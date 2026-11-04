"""Tubería de audio en un hilo propio:

  cola del micrófono → ganancia → supresión de ruido → remuestreo a 16 kHz
  → VAD (Silero) → segmentador → eventos UTTERANCE_*

Consumo en silencio: solo RNNoise (si está activo) + Silero sobre bloques de
32 ms, ambos del orden de 1 ms de CPU por bloque.

El VAD decide sobre el audio LIMPIO; la transcripción recibe por defecto el
audio CRUDO alineado (prioridad: no perder palabras).
"""
from __future__ import annotations

import logging
import queue
import threading
import time

import numpy as np

from cronista.audio import dsp
from cronista.audio.capture import MicCapture, resolve_device
from cronista.audio.denoise import build_denoiser
from cronista.audio.segmenter import SegmenterConfig, Utterance, UtteranceSegmenter
from cronista.audio.vad import SENSITIVITY, build_vad
from cronista.core.events import EventBus, Topics
from cronista.core.settings import Settings

log = logging.getLogger(__name__)

STT_RATE = 16000


class AudioPipeline:
    def __init__(self, bus: EventBus, settings: Settings) -> None:
        self.bus = bus
        self.settings = settings
        self.capture: MicCapture | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.partials_enabled = True
        self.listening = False
        self.noise_floor_db = -60.0
        self.last_level_db = -90.0
        self.last_prob = 0.0
        self._force_requested: bool | None = None
        self._raw_utt: dict[str, list[np.ndarray]] = {}
        self.device_label = ""
        self.auto_detect = True   # False (modo manual): solo se abre una frase con el hotkey

    # ------------------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            dev = resolve_device(self.settings.audio.device)
            if dev is None:
                self.bus.publish(Topics.AUDIO_ERROR, message="No se encontró ningún micrófono")
                return
            self.capture = MicCapture(dev)
            try:
                rate = self.capture.start()
            except Exception as e:
                self.capture = None
                self.bus.publish(Topics.AUDIO_ERROR, message=str(e))
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, args=(rate,), name="audio-pipeline", daemon=True)
            self._thread.start()
            self.listening = True
            self.device_label = dev.label()
            self.bus.publish(Topics.AUDIO_STARTED, device=dev.label(), samplerate=rate)

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            if self.capture:
                self.capture.stop()  # corta el acceso al dispositivo de inmediato
                self.capture = None
            t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=3)
        self._thread = None
        if self.listening:
            self.listening = False
            self.bus.publish(Topics.AUDIO_STOPPED)

    def restart(self) -> None:
        self.stop()
        self.start()

    def set_forced(self, forced: bool) -> None:
        """Llamado desde el hilo del hotkey; se aplica en el hilo de audio."""
        self._force_requested = forced

    # ------------------------------------------------------------------
    def _build_segmenter(self) -> UtteranceSegmenter:
        a = self.settings.audio
        on_t, off_t = SENSITIVITY.get(a.vad_sensitivity, SENSITIVITY["medium"])
        cfg = SegmenterConfig(sample_rate=STT_RATE, chunk=512, on_threshold=on_t, off_threshold=off_t,
                              pre_roll_ms=a.pre_roll_ms, post_roll_ms=a.post_roll_ms,
                              end_silence_ms=a.end_silence_ms, min_speech_ms=a.min_speech_ms,
                              max_utterance_s=a.max_utterance_s, partial_every_s=a.partial_every_s,
                              enable_partials=True)
        return UtteranceSegmenter(cfg, self._on_start, self._on_partial, self._on_end)

    def _run(self, rate: int) -> None:
        try:
            self._loop(rate)
        except Exception as e:
            log.exception("Fallo en la tubería de audio")
            self.bus.publish(Topics.AUDIO_ERROR, message=f"Tubería de audio detenida: {e}")

    def _loop(self, rate: int) -> None:
        a = self.settings.audio
        denoiser, stationary = build_denoiser(a.noise_suppression, rate)
        res_clean = dsp.StreamResampler(rate, STT_RATE)
        res_raw = dsp.StreamResampler(rate, STT_RATE)
        vad = build_vad(STT_RATE)
        seg = self._build_segmenter()
        chunk = vad.chunk
        raw_pre = dsp.RingBuffer(int(STT_RATE * a.pre_roll_ms / 1000))
        self._raw_pre = raw_pre
        clean_acc = np.zeros(0, dtype=np.float32)
        raw_acc = np.zeros(0, dtype=np.float32)
        level_t = 0.0
        noise_learn_t = 0.0
        silence_run_ms = 0.0
        cap = self.capture
        gain = a.gain_db
        stt_from_raw = not a.denoise_for_stt

        while not self._stop.is_set() and cap is not None:
            try:
                blk = cap.queue.get(timeout=0.25)
            except queue.Empty:
                continue
            x = dsp.apply_gain(blk.samples, gain)
            clean = denoiser.process(x)
            raw16 = res_raw.process(x)
            clean16 = res_clean.process(clean)
            clean_acc = np.concatenate([clean_acc, clean16])
            raw_acc = np.concatenate([raw_acc, raw16])
            n = min(len(clean_acc), len(raw_acc))
            n_chunks = n // chunk
            if n_chunks == 0:
                continue
            now = time.monotonic()
            block_t0 = now - n_chunks * chunk / STT_RATE
            for i in range(n_chunks):
                c = clean_acc[i * chunk:(i + 1) * chunk]
                r = raw_acc[i * chunk:(i + 1) * chunk]
                stt_chunk = r if stt_from_raw else c
                t_chunk = block_t0 + (i + 1) * chunk / STT_RATE
                fr = self._force_requested
                if fr is not None:
                    self._force_requested = None
                    seg.set_forced(fr, t_chunk)   # puede abrir → _on_start lee raw_pre
                if seg.current is not None:
                    self._raw_utt.setdefault(seg.current.id, []).append(stt_chunk)
                else:
                    raw_pre.push(stt_chunk)
                p = vad.prob(c)
                self.last_prob = p
                # modo manual: la detección automática no abre frases (solo el hotkey)
                seg.process(c, p if (self.auto_detect or seg.current is not None) else 0.0, t_chunk)
                # perfil de ruido estacionario: aprender en silencios largos
                if p < 0.15 and seg.current is None:
                    silence_run_ms += chunk * 1000 / STT_RATE
                    if stationary is not None and silence_run_ms > 800 and now - noise_learn_t > 2.0:
                        noise_learn_t = now
                        stationary.observe_noise(x)
                else:
                    silence_run_ms = 0.0
            clean_acc = clean_acc[n_chunks * chunk:]
            raw_acc = raw_acc[n_chunks * chunk:]

            if now - level_t >= 0.1:  # nivel para la UI (10 Hz)
                level_t = now
                lvl = dsp.rms_db(x)
                self.last_level_db = lvl
                if self.last_prob < 0.2:
                    self.noise_floor_db = 0.97 * self.noise_floor_db + 0.03 * lvl
                self.bus.publish(Topics.AUDIO_LEVEL, rms_db=lvl, peak_db=dsp.peak_db(x),
                                 speech_prob=self.last_prob, noise_db=self.noise_floor_db,
                                 speaking=seg.current is not None, dropped=cap.dropped)

    # ---- callbacks del segmentador (hilo de audio) --------------------
    _pending_pre: np.ndarray = np.zeros(0, dtype=np.float32)

    def _on_start(self, utt: Utterance) -> None:
        self._raw_utt[utt.id] = [self._pending_pre]
        self.bus.publish(Topics.UTTERANCE_START, utterance_id=utt.id, start_mono=utt.start_mono, forced=utt.forced)

    def _stt_audio(self, utt: Utterance) -> np.ndarray:
        parts = self._raw_utt.get(utt.id)
        if not parts:
            return utt.audio()
        audio = np.concatenate(parts)
        target = sum(len(c) for c in utt.chunks)
        return audio[:target] if len(audio) > target else audio

    def _on_partial(self, utt: Utterance) -> None:
        if not self.partials_enabled:
            return
        self.bus.publish(Topics.UTTERANCE_PARTIAL, utterance_id=utt.id, audio=self._stt_audio(utt),
                         samplerate=STT_RATE, start_mono=utt.start_mono)

    def _on_end(self, utt: Utterance, reason: str) -> None:
        audio = self._stt_audio(utt)
        self._raw_utt.pop(utt.id, None)
        if reason == "discard":
            self.bus.publish(Topics.NOTE_DISCARDED, reason="too_short", text="", utterance_id=utt.id)
            return
        self.bus.publish(Topics.UTTERANCE_END, utterance_id=utt.id, audio=audio, samplerate=STT_RATE,
                         start_mono=utt.start_mono, duration_ms=int(len(audio) * 1000 / STT_RATE),
                         forced=utt.forced, reason=reason)

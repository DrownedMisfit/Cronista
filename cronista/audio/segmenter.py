"""Segmentador de frases: convierte probabilidades de voz por bloque en
"utterances" (comentarios completos) con pre-roll, tolerancia a pausas y
corte suave para comentarios largos. También soporta captura forzada (hotkey).

Es una máquina de estados pura (sin hilos ni audio real) para poder probarla
con audio sintético.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from cronista.audio.dsp import RingBuffer


@dataclass
class SegmenterConfig:
    sample_rate: int = 16000
    chunk: int = 512
    on_threshold: float = 0.5
    off_threshold: float = 0.3
    pre_roll_ms: int = 700
    post_roll_ms: int = 400
    end_silence_ms: int = 1500
    min_speech_ms: int = 300
    max_utterance_s: int = 75
    hard_max_s: int = 110          # corte duro si no aparece una pausa tras max_utterance_s
    partial_every_s: float = 2.5
    enable_partials: bool = True
    speech_onset_chunks: int = 2   # bloques consecutivos por encima del umbral para abrir


@dataclass
class Utterance:
    id: str
    start_mono: float              # reloj monotónico del primer sample (aprox.)
    forced: bool = False
    chunks: list[np.ndarray] = field(default_factory=list)
    speech_ms: float = 0.0
    silence_ms: float = 0.0
    last_partial_s: float = 0.0
    speech_seen: bool = False

    def audio(self) -> np.ndarray:
        return np.concatenate(self.chunks) if self.chunks else np.zeros(0, dtype=np.float32)

    def duration_s(self, sr: int) -> float:
        return sum(len(c) for c in self.chunks) / sr


class UtteranceSegmenter:
    """Callbacks:
    on_start(utt), on_partial(utt), on_end(utt, reason) — reason: 'silence' | 'max' | 'forced' | 'discard'
    """

    def __init__(self, cfg: SegmenterConfig,
                 on_start: Callable[[Utterance], None] | None = None,
                 on_partial: Callable[[Utterance], None] | None = None,
                 on_end: Callable[[Utterance, str], None] | None = None) -> None:
        self.cfg = cfg
        self.on_start = on_start or (lambda u: None)
        self.on_partial = on_partial or (lambda u: None)
        self.on_end = on_end or (lambda u, r: None)
        sr = cfg.sample_rate
        self.chunk_ms = cfg.chunk * 1000.0 / sr
        self.pre = RingBuffer(int(sr * cfg.pre_roll_ms / 1000))
        self.current: Utterance | None = None
        self.forced = False
        self._onset = 0
        self._post_roll_left_ms = 0.0
        self._closing = False
        self.last_prob = 0.0

    # ------------------------------------------------------------------
    def set_forced(self, forced: bool, now: float | None = None) -> None:
        """Hotkey mantenido: abre (o mantiene) una captura hasta soltar."""
        now = time.monotonic() if now is None else now
        if forced and not self.forced:
            self.forced = True
            if self.current is None:
                self._open(now, forced=True)
            else:
                self.current.forced = True
        elif not forced and self.forced:
            self.forced = False
            if self.current is not None:
                # al soltar: cerrar tras el post-roll
                self._closing = True
                self._post_roll_left_ms = self.cfg.post_roll_ms

    def process(self, chunk: np.ndarray, prob: float, now: float | None = None) -> None:
        """Alimentar un bloque (len == cfg.chunk) con su probabilidad de voz."""
        now = time.monotonic() if now is None else now
        self.last_prob = prob
        cfg = self.cfg
        if self.current is None:
            self.pre.push(chunk)
            if prob >= cfg.on_threshold:
                self._onset += 1
                if self._onset >= cfg.speech_onset_chunks:
                    self._open(now, forced=False)
            else:
                self._onset = 0
            return

        utt = self.current
        utt.chunks.append(chunk)
        is_speech = prob >= (cfg.off_threshold if utt.speech_seen else cfg.on_threshold)
        if is_speech:
            utt.speech_seen = True
            utt.speech_ms += self.chunk_ms
            utt.silence_ms = 0.0
        else:
            utt.silence_ms += self.chunk_ms

        dur = utt.duration_s(cfg.sample_rate)

        # parciales mientras hablo
        if cfg.enable_partials and utt.speech_seen and dur - utt.last_partial_s >= cfg.partial_every_s:
            utt.last_partial_s = dur
            self.on_partial(utt)

        if self._closing:  # hotkey soltado: agotar post-roll y cerrar
            self._post_roll_left_ms -= self.chunk_ms
            if self._post_roll_left_ms <= 0:
                self._close("forced")
            return

        if self.forced:
            if dur >= cfg.hard_max_s:
                self._close("max")
            return

        # cierre por silencio (con post-roll ya incluido en end_silence)
        if utt.silence_ms >= cfg.end_silence_ms:
            self._close("silence")
            return
        # corte suave en una pausa breve para comentarios muy largos
        if dur >= cfg.max_utterance_s and utt.silence_ms >= 350:
            self._close("max")
            return
        if dur >= cfg.hard_max_s:
            self._close("max")

    # ------------------------------------------------------------------
    def _open(self, now: float, forced: bool) -> None:
        pre = self.pre.get()
        sr = self.cfg.sample_rate
        start_mono = now - len(pre) / sr
        utt = Utterance(id=uuid.uuid4().hex[:12], start_mono=start_mono, forced=forced)
        if len(pre):
            utt.chunks.append(pre)
        self.current = utt
        self._onset = 0
        self._closing = False
        self.pre.clear()
        self.on_start(utt)

    def _close(self, reason: str) -> None:
        utt = self.current
        if utt is None:
            return
        self.current = None
        self._closing = False
        # recortar el exceso de silencio final, dejando el post-roll
        keep_tail_ms = self.cfg.post_roll_ms
        if reason == "silence" and utt.silence_ms > keep_tail_ms:
            drop = int((utt.silence_ms - keep_tail_ms) / 1000 * self.cfg.sample_rate)
            audio = utt.audio()
            if drop < len(audio):
                utt.chunks = [audio[:len(audio) - drop]]
        if not utt.forced and utt.speech_ms < self.cfg.min_speech_ms:
            self.on_end(utt, "discard")
            return
        if utt.forced and not utt.speech_seen and utt.speech_ms < 150:
            # hotkey pulsado sin hablar: descartar sin ruido
            self.on_end(utt, "discard")
            return
        self.on_end(utt, reason)
        # el reloj de inicio sigue siendo válido; el pre-roll se regenera solo

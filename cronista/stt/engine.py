"""Transcripción local con faster-whisper (CTranslate2), en un hilo propio.

Diseño orientado a bajo consumo:
  - El modelo solo trabaja cuando llega una frase (nunca en silencio).
  - Parciales (mientras hablo) con un modelo pequeño y búsqueda greedy; se descartan si
    el motor está ocupado (solo importa el último).
  - Final con el modelo principal y beam search; tiene prioridad sobre los parciales.
  - Descarga de modelos tras N minutos sin uso (libera RAM) y recarga bajo demanda.
  - Modo "eco" (juego en primer plano): menos beam, sin parciales si así se configura.

El backend es intercambiable: cualquier objeto con `transcribe(audio16k) -> (text, info)`.
"""
from __future__ import annotations

import gc
import logging
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from cronista.core.events import EventBus, Topics
from cronista.core.hardware import HardwareInfo
from cronista.core.paths import models_dir
from cronista.core.settings import Settings

log = logging.getLogger(__name__)

MODEL_CHOICES = ["auto", "tiny", "base", "small", "medium", "large-v3-turbo", "large-v3"]
MODEL_SIZES_MB = {"tiny": 75, "base": 140, "small": 480, "medium": 1500, "large-v3-turbo": 1600, "large-v3": 3000}

# Frases típicas que Whisper "alucina" en silencio o ruido (se descartan si son todo el texto)
_HALLUCINATIONS = [
    r"subt[ií]tulos? (realizados|hechos|creados).*", r"gracias por ver.*", r"suscr[ií]b(e|a)te.*",
    r"^\s*(música|aplausos|risas|silencio)\s*$", r"www\.\w+\.\w+", r"amara\.org", r"^\s*\.+\s*$",
    r"^\s*¡?gracias!?\.?\s*$", r"^\s*(adiós|chao|hasta luego)[.!]?\s*$",
]
_HALLU_RE = re.compile("|".join(f"(?:{p})" for p in _HALLUCINATIONS), re.IGNORECASE)


@dataclass(order=True)
class _Task:
    priority: int
    seq: int
    kind: str = field(compare=False)          # partial | final
    utterance_id: str = field(compare=False)
    audio: np.ndarray = field(compare=False)
    meta: dict = field(compare=False, default_factory=dict)


def resolve_model_name(settings: Settings, hw: HardwareInfo) -> str:
    m = (settings.stt.model or "auto").lower()
    return hw.recommended_model() if m == "auto" else m


class FasterWhisperBackend:
    """Envoltorio de faster_whisper.WhisperModel con opciones conservadoras contra alucinaciones."""

    def __init__(self, model_name: str, device: str, compute_type: str, cpu_threads: int,
                 language: str, on_progress=None) -> None:
        from faster_whisper import WhisperModel

        self.name = model_name
        self.language = language
        root = str(models_dir() / "whisper")
        if device == "auto":
            device = "cpu"
            try:
                import ctranslate2
                if ctranslate2.get_cuda_device_count() > 0:
                    device = "cuda"
            except Exception:
                pass
        if compute_type == "auto":
            compute_type = "float16" if device == "cuda" else "int8"
        self.device, self.compute_type = device, compute_type
        t0 = time.monotonic()
        self.model = WhisperModel(model_name, device=device, compute_type=compute_type,
                                  cpu_threads=cpu_threads, num_workers=1, download_root=root)
        log.info("Modelo %s cargado en %.1fs (%s/%s, %d hilos)", model_name, time.monotonic() - t0,
                 device, compute_type, cpu_threads)

    def transcribe(self, audio: np.ndarray, beam_size: int = 5, initial_prompt: str | None = None,
                   hotwords: str | None = None, fast: bool = False) -> tuple[str, dict]:
        segments, info = self.model.transcribe(
            audio, language=self.language, beam_size=1 if fast else beam_size, best_of=1 if fast else 5,
            temperature=0.0 if fast else [0.0, 0.2, 0.4], condition_on_previous_text=False,
            initial_prompt=initial_prompt, hotwords=hotwords, vad_filter=False, without_timestamps=True,
            no_speech_threshold=0.6, log_prob_threshold=-1.0, compression_ratio_threshold=2.4,
            suppress_blank=True)
        parts: list[str] = []
        logprobs: list[float] = []
        nospeech: list[float] = []
        for seg in segments:
            t = seg.text.strip()
            if not t:
                continue
            if seg.no_speech_prob > 0.85 and seg.avg_logprob < -1.2:
                continue   # casi seguro que es ruido
            parts.append(t)
            logprobs.append(seg.avg_logprob)
            nospeech.append(seg.no_speech_prob)
        text = " ".join(parts).strip()
        return text, {"model": self.name, "language": info.language, "language_prob": float(info.language_probability),
                      "avg_logprob": float(np.mean(logprobs)) if logprobs else 0.0,
                      "no_speech_prob": float(np.mean(nospeech)) if nospeech else 1.0,
                      "device": self.device, "compute_type": self.compute_type}


def looks_like_hallucination(text: str) -> bool:
    t = text.strip()
    if not t:
        return True
    if _HALLU_RE.search(t) and len(t) < 80:
        return True
    words = re.findall(r"\w+", t.lower())
    if len(words) >= 6 and len(set(words)) <= 2:   # "no no no no no no"
        return True
    return False


class STTEngine:
    def __init__(self, bus: EventBus, settings: Settings, hw: HardwareInfo) -> None:
        self.bus = bus
        self.settings = settings
        self.hw = hw
        self._q: queue.PriorityQueue[_Task] = queue.PriorityQueue()
        self._seq = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._main: FasterWhisperBackend | None = None
        self._partial: FasterWhisperBackend | None = None
        self._lock = threading.Lock()
        self._last_use = time.monotonic()
        self.eco = False
        self.state = "idle"
        self.model_name = resolve_model_name(settings, hw)      # modelo Whisper (en vivo o segunda pasada)
        self.extra_vocab: Callable[[], list[str]] | None = None   # vocabulario personal (lo asigna App)
        self.voice_gate: Callable[[np.ndarray, bool], dict] | None = None   # perfil de voz (lo asigna App)
        self.context_game: Callable[[], str] | None = None        # juego de la sesión actual (lo asigna App)
        self.context_terms: Callable[[], list[str]] | None = None  # juegos y referencias conocidos (lo asigna App)
        self._pending_partial: dict[str, int] = {}   # utterance → seq del último parcial
        self._finalized: set[str] = set()
        bus.subscribe(Topics.UTTERANCE_PARTIAL, self._on_partial)
        bus.subscribe(Topics.UTTERANCE_END, self._on_end)
        bus.subscribe(Topics.PERF_GAME_DETECTED, lambda e: self.set_eco(bool(e.get("detected"))))

    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="stt-engine", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._q.put(_Task(-1, -1, "quit", "", np.zeros(0)))
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=5)
        self.unload()

    def set_eco(self, eco: bool) -> None:
        if eco != self.eco:
            self.eco = eco
            log.info("Modo eco %s", "activado" if eco else "desactivado")

    def reload_models(self) -> None:
        """Tras cambiar la configuración: descarga y deja que se recarguen bajo demanda."""
        self.model_name = resolve_model_name(self.settings, self.hw)
        self.unload()

    def unload(self) -> None:
        with self._lock:
            had = self._main is not None or self._partial is not None
            self._main = None
            self._partial = None
        gate = self.voice_gate
        if gate is not None and hasattr(gate, "unload"):
            gate.unload()      # el modelo de voz también se libera en reposo (se recarga al instante)
        if had:
            gc.collect()
            log.info("Modelos de transcripción descargados de memoria")
            self._status("idle")

    def preload(self) -> None:
        self._submit("preload", "", np.zeros(0), {}, priority=5)

    # ---- entrada de audio --------------------------------------------------
    def _partials_wanted(self) -> bool:
        p = (self.settings.stt.partials or "auto").lower()
        if p == "off":
            return False
        if p == "on":
            return True
        return not (self.eco and self.settings.performance.prioritize_game)

    def _on_partial(self, ev) -> None:
        if not self._partials_wanted() or ev.utterance_id in self._finalized:
            return
        self._submit("partial", ev.utterance_id, ev.audio, {"start_mono": ev.get("start_mono")}, priority=5)

    def _on_end(self, ev) -> None:
        self._finalized.add(ev.utterance_id)
        if len(self._finalized) > 500:
            self._finalized = set(list(self._finalized)[-200:])
        self._submit("final", ev.utterance_id, ev.audio,
                     {"start_mono": ev.get("start_mono"), "duration_ms": ev.get("duration_ms"),
                      "forced": ev.get("forced", False), "reason": ev.get("reason", "")}, priority=1)

    def _submit(self, kind: str, utt: str, audio: np.ndarray, meta: dict, priority: int) -> None:
        self._seq += 1
        if kind == "partial":
            self._pending_partial[utt] = self._seq
        self._q.put(_Task(priority, self._seq, kind, utt, audio, meta))

    # ---- hilo de trabajo ---------------------------------------------------
    @property
    def live_model_name(self) -> str:
        """Nombre del modelo que transcribe en vivo (para mostrar en la interfaz)."""
        if self.live_backend() == "parakeet":
            from cronista.stt.parakeet import MODEL_NAME
            return MODEL_NAME
        return self.model_name

    def _status(self, state: str, **extra: Any) -> None:
        self.state = state
        self.bus.publish(Topics.STT_STATUS, state=state, model=self.live_model_name, **extra)

    def _threads(self) -> int:
        n = int(self.settings.stt.cpu_threads or 0) or self.hw.recommended_threads()
        return max(1, n)

    def live_backend(self) -> str:
        """Motor que transcribe en vivo: "parakeet" o "faster-whisper"."""
        b = (self.settings.stt.backend or "auto").lower()
        if b in ("auto", "parakeet"):
            from cronista.stt import parakeet
            if parakeet.available():
                return "parakeet"
        return "faster-whisper"

    def _ensure_main(self):
        if self.live_backend() == "parakeet":
            from cronista.stt.parakeet import MODEL_NAME, ParakeetBackend
            with self._lock:
                if self._main is None or self._main.name != MODEL_NAME:
                    self._status("loading", detail="Cargando Parakeet…")
                    self._main = ParakeetBackend(cpu_threads=self._threads(), language=self.settings.stt.language)
                    self._partial = None
                return self._main
        with self._lock:
            if self._main is None or self._main.name != self.model_name:
                self._status("loading", detail=f"Cargando {self.model_name}…")
                self._main = FasterWhisperBackend(self.model_name, self.settings.stt.device,
                                                  self.settings.stt.compute_type, self._threads(),
                                                  self.settings.stt.language)
                if self._partial is not None and self._partial.name == self.model_name:
                    self._partial = None
            return self._main

    def _ensure_partial(self):
        if self.live_backend() == "parakeet":
            return self._ensure_main()          # Parakeet es tan rápido que hace también los parciales
        name = self.settings.stt.partial_model or "small"
        if name in ("same", self.model_name) or MODEL_SIZES_MB.get(self.model_name, 0) <= MODEL_SIZES_MB.get(name, 0):
            return self._ensure_main()
        with self._lock:
            if self._partial is None or self._partial.name != name:
                self._status("loading", detail=f"Cargando {name} (parciales)…")
                self._partial = FasterWhisperBackend(name, self.settings.stt.device, self.settings.stt.compute_type,
                                                     max(1, self._threads() // 2), self.settings.stt.language)
            return self._partial

    @staticmethod
    def _cache_audio(utterance_id: str, audio: np.ndarray) -> str | None:
        """Guarda el audio de la frase (FLAC, local) hasta la segunda pasada; se borra al repasarla."""
        try:
            import soundfile as sf
            from cronista.core.paths import data_dir
            d = data_dir() / "audio_pendiente"
            d.mkdir(parents=True, exist_ok=True)
            path = d / f"{utterance_id}.flac"
            sf.write(str(path), np.clip(audio, -1, 1), 16000, format="FLAC", subtype="PCM_16")
            return str(path)
        except Exception:
            log.warning("No se pudo guardar el audio para la segunda pasada", exc_info=True)
            return None

    def glossary(self):
        """Glosario bilingüe actual: tus términos + juego de la sesión + juegos conocidos + vocabulario
        aprendido + términos de videojuegos en inglés (ver stt/glossary.py)."""
        from cronista.stt.glossary import GAMING_TERMS, Glossary
        extra: list[str] = []
        for fn in (self.context_terms, self.extra_vocab):
            if fn is not None:
                try:
                    extra += [t for t in fn() if t]
                except Exception:
                    log.debug("Términos de contexto no disponibles", exc_info=True)
        return Glossary.from_lines(list(self.settings.stt.vocabulary), extra_terms=extra + GAMING_TERMS)

    def _current_game(self) -> str:
        try:
            return (self.context_game() if self.context_game else "") or ""
        except Exception:
            return ""

    def _prompt(self) -> tuple[str, str | None]:
        # Pistas para que Whisper escriba en inglés los títulos y términos que dices en inglés
        return self.glossary().prompt(self._current_game())

    def _fix_terms(self, text: str, info: dict | None = None) -> str:
        """Corrige títulos y términos en inglés mal entendidos («Rise of Iron Di-Day» → «Rise of Iron: D-Day»)."""
        if not text or not self.settings.stt.fix_terms:
            return text
        try:
            fixed, changes = self.glossary().fix(text, game=self._current_game())
        except Exception:
            log.debug("Glosario: corrección omitida", exc_info=True)
            return text
        if changes and info is not None:
            info["glossary"] = changes
            info["text_before_glossary"] = text
        return fixed

    def _run(self) -> None:
        idle_unload_s = max(0, int(self.settings.stt.idle_unload_min)) * 60
        while not self._stop.is_set():
            try:
                task = self._q.get(timeout=5.0)
            except queue.Empty:
                if idle_unload_s and (self._main or self._partial) and time.monotonic() - self._last_use > idle_unload_s:
                    self.unload()
                continue
            if task.kind == "quit":
                break
            try:
                self._handle(task)
            except Exception as e:
                log.exception("Error en transcripción")
                self._status("idle")
                self.bus.publish(Topics.STT_ERROR, utterance_id=task.utterance_id, message=str(e))
            finally:
                self._last_use = time.monotonic()
                idle_unload_s = max(0, int(self.settings.stt.idle_unload_min)) * 60

    def _handle(self, task: _Task) -> None:
        if task.kind == "preload":
            self._ensure_main()
            self._status("idle")
            return
        if task.kind == "partial":
            if self._pending_partial.get(task.utterance_id) != task.seq or task.utterance_id in self._finalized:
                return  # hay uno más reciente o la frase ya terminó
            if task.audio.size < 16000 * 0.8:
                return
            backend = self._ensure_partial()
            self._status("busy", kind="partial")
            prompt, hot = self._prompt()
            text, info = backend.transcribe(task.audio, fast=True, initial_prompt=prompt, hotwords=hot)
            self._status("idle")
            if text and not looks_like_hallucination(text):
                self.bus.publish(Topics.STT_PARTIAL, utterance_id=task.utterance_id, text=self._fix_terms(text))
            return
        # final
        if task.audio.size < 16000 * 0.3:
            self.bus.publish(Topics.STT_FINAL, utterance_id=task.utterance_id, text="", info={"empty": True},
                             **task.meta)
            return
        voice = None
        if self.voice_gate is not None:
            # identificación de voz ANTES de transcribir: si se descarta otra voz, Whisper ni se ejecuta
            voice = self.voice_gate(task.audio, bool(task.meta.get("forced")))
            if voice.get("action") == "skip":
                log.info("Frase descartada: no parece tu voz (similitud %s < %s)", voice.get("score"),
                         voice.get("threshold"))
                self.bus.publish(Topics.NOTE_DISCARDED, reason="other_voice", text="",
                                 utterance_id=task.utterance_id, voice=voice)
                return
        backend = self._ensure_main()
        self._status("busy", kind="final")
        prompt, hot = self._prompt()
        beam = int(self.settings.stt.beam_size or 5)
        if self.eco and self.settings.performance.prioritize_game:
            beam = min(beam, 2)
        t0 = time.monotonic()
        text, info = backend.transcribe(task.audio, beam_size=beam, initial_prompt=prompt, hotwords=hot)
        info["seconds"] = round(time.monotonic() - t0, 2)
        info["audio_s"] = round(task.audio.size / 16000, 2)
        if voice is not None and voice.get("score") is not None:
            info["voice"] = voice          # la nota guarda la similitud y si coincidió con tu perfil
        self._status("idle")
        if looks_like_hallucination(text):
            info["hallucination"] = True
            text = ""
        log.info("Transcrito %.1fs de audio en %.1fs: %s", info["audio_s"], info["seconds"], text[:80])
        text = self._fix_terms(text, info)
        if text and info.get("backend") == "parakeet" and self.settings.stt.second_pass:
            path = self._cache_audio(task.utterance_id, task.audio)
            if path:
                info["audio_cache"] = path      # la segunda pasada (Whisper) lo repasará al dejar de jugar
        self.bus.publish(Topics.STT_FINAL, utterance_id=task.utterance_id, text=text, info=info, **task.meta)


def model_is_downloaded(name: str) -> bool:
    try:
        from faster_whisper.utils import _MODELS
        repo = _MODELS.get(name, name)
        folder = models_dir() / "whisper" / ("models--" + repo.replace("/", "--"))
        return folder.exists() and any(folder.rglob("model.bin"))
    except Exception:
        return False


def download_model(name: str, progress=None) -> str:
    """Descarga un modelo al directorio local de modelos (requiere internet; se indica en la UI)."""
    from faster_whisper.utils import download_model as _dl
    return _dl(name, cache_dir=str(models_dir() / "whisper"))

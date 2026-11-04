"""Segunda pasada: Whisper + glosario repasan las frases transcritas en vivo con Parakeet.

Por qué: Parakeet es ~15 veces más rápido (ideal mientras juegas) pero no admite pistas de vocabulario;
Whisper sí (prompt con tus títulos), aunque en CPU tarda ~1,3-2 s por segundo hablado. Así se combinan:
en vivo, rápido y ligero; después, preciso con los títulos en inglés.

Reglas (prioridad: no perder ni empeorar lo que dijiste):
- Solo trabaja sin un juego en primer plano, de una en una, con prioridad baja.
- Acepta el texto de Whisper solo si se parece razonablemente al de Parakeet (mismo audio): si Whisper
  alucina o se va por otro lado, se queda el texto en vivo.
- Si editaste la nota a mano, no la toca (guarda la propuesta aparte).
- El audio de la frase se borra al repasarla. Si desactivas la segunda pasada, se borra el pendiente.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from difflib import SequenceMatcher
from pathlib import Path

from cronista.core.events import EventBus, Topics

log = logging.getLogger(__name__)

MIN_SIMILARITY = 0.55        # parecido mínimo entre el texto en vivo y el de Whisper para aceptarlo
IDLE_UNLOAD_S = 120          # descargar Whisper tras 2 min sin trabajo


def _norm(t: str) -> str:
    return re.sub(r"[^\w]+", " ", (t or "").lower()).strip()


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


class SecondPass:
    def __init__(self, bus: EventBus, settings, store, stt, hw) -> None:
        self.bus = bus
        self.settings = settings
        self.store = store
        self.stt = stt
        self.hw = hw
        self.game_active = False
        self._whisper = None
        self._last_use = 0.0
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.done_count = 0
        bus.subscribe(Topics.PERF_GAME_DETECTED, self._on_game)
        bus.subscribe(Topics.NOTE_CREATED, lambda e: self._wake.set())
        bus.subscribe(Topics.SESSION_STOPPED, lambda e: self._wake.set())

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="second-pass", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=10)
        self._whisper = None

    def _on_game(self, ev) -> None:
        self.game_active = bool(ev.get("detected"))
        if self.game_active:
            self._whisper = None          # liberar RAM mientras juegas; se recarga después
        else:
            self._wake.set()

    # ---- bucle ----------------------------------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(5.0)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                pending = self.store.notes_pending_refine(limit=1)
                if not pending:
                    if self._whisper is not None and time.monotonic() - self._last_use > IDLE_UNLOAD_S:
                        self._whisper = None
                        log.info("Segunda pasada: Whisper descargado (sin trabajo)")
                    continue
                if not self.settings.stt.second_pass:
                    self._discard(pending[0], "segunda pasada desactivada")
                    self._wake.set()
                    continue
                if self.game_active:
                    continue
                self._refine(pending[0])
                self._wake.set()           # seguir con la siguiente sin esperar
            except Exception:
                log.exception("Segunda pasada: error")

    # ---- trabajo ----------------------------------------------------------------------------------
    def _backend(self):
        if self._whisper is None:
            from cronista.stt.engine import FasterWhisperBackend
            s = self.settings.stt
            threads = max(1, int(s.cpu_threads or 0) or self.hw.recommended_threads())
            self._whisper = FasterWhisperBackend(self.stt.model_name, s.device, s.compute_type, threads, s.language)
        self._last_use = time.monotonic()
        return self._whisper

    def _discard(self, note: dict, reason: str) -> None:
        info = dict(note.get("stt_info") or {})
        path = info.pop("audio_cache", None)
        info["second_pass"] = {"done": True, "applied": False, "reason": reason}
        self.store.update_note(note["id"], stt_info=info)
        _remove(path)

    def _refine(self, note: dict) -> None:
        import soundfile as sf
        info = dict(note.get("stt_info") or {})
        path = info.get("audio_cache")
        if not path or not os.path.isfile(path):
            self._discard(note, "audio no encontrado")
            return
        sess = self.store.get_session(note["session_id"]) or {}
        game = sess.get("game") or ""
        audio, _sr = sf.read(path, dtype="float32")
        glossary = self.stt.glossary()
        prompt, _ = glossary.prompt(game)
        t0 = time.monotonic()
        text, winfo = self._backend().transcribe(audio, beam_size=int(self.settings.stt.beam_size or 5),
                                                 initial_prompt=prompt)
        from cronista.stt.engine import looks_like_hallucination
        if looks_like_hallucination(text):
            text = ""
        text, changes = glossary.fix(text, game=game)
        live = note.get("text_raw") or note.get("text") or ""
        sim = similarity(live, text) if text else 0.0
        info.pop("audio_cache", None)
        result = {"done": True, "model": self._whisper.name if self._whisper else "", "similarity": round(sim, 2),
                  "seconds": round(time.monotonic() - t0, 1), "text": text}
        fields: dict = {}
        if not text or sim < MIN_SIMILARITY:
            result.update(applied=False, reason="demasiado distinto del texto en vivo" if text else "sin texto")
        elif note.get("user_edited"):
            result.update(applied=False, reason="editada por ti")
        elif _norm(text) == _norm(live):
            result.update(applied=False, reason="sin cambios")
        else:
            result.update(applied=True)
            info["live_text"] = live
            fields.update(text_raw=text, text=text)
        info["second_pass"] = result
        if changes:
            info["glossary_second_pass"] = changes
        self.store.update_note(note["id"], stt_info=info, **fields)
        _remove(path)
        self.done_count += 1
        if fields:
            log.info("Segunda pasada: nota %d mejorada (%.2f): %s", note["id"], sim, text[:80])
            self.bus.publish(Topics.NOTE_UPDATED, note=self.store.get_note(note["id"]), source="refine")


def _remove(path: str | None) -> None:
    if path:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass


def cleanup_orphans(store, max_age_s: float = 86400.0) -> int:
    """Borra audios pendientes que ninguna nota referencia (p. ej. frases descartadas) y tengan más de un día."""
    from cronista.core.paths import data_dir
    d = data_dir() / "audio_pendiente"
    if not d.exists():
        return 0
    referenced = {os.path.normcase(str(Path((n.get("stt_info") or {}).get("audio_cache", ""))))
                  for n in store.notes_pending_refine(limit=100000)}
    removed = 0
    for f in d.glob("*.flac"):
        if os.path.normcase(str(f)) not in referenced and time.time() - f.stat().st_mtime > max_age_s:
            _remove(str(f))
            removed += 1
    return removed

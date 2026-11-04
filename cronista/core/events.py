"""Bus de eventos simple y seguro entre hilos.

Todos los módulos se comunican publicando eventos por tema ("obs.record.started",
"audio.utterance.end", ...). Ningún módulo importa a otro módulo de alto nivel:
solo conocen el bus. Esto permite sustituir cualquier módulo sin tocar el resto.
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)

Handler = Callable[["Event"], None]


@dataclass(slots=True)
class Event:
    topic: str
    data: dict[str, Any] = field(default_factory=dict)

    def __getattr__(self, item: str) -> Any:  # acceso cómodo: ev.path
        try:
            return self.data[item]
        except KeyError as e:  # pragma: no cover
            raise AttributeError(item) from e

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


class EventBus:
    """Publicación síncrona en el hilo del emisor. Los suscriptores que necesiten
    ejecutar en otro hilo (p. ej. la UI de Qt) deben hacer su propio puente."""

    WILDCARD = "*"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subs: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, topic: str, handler: Handler) -> Callable[[], None]:
        with self._lock:
            self._subs[topic].append(handler)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._subs[topic].remove(handler)
                except ValueError:
                    pass

        return unsubscribe

    def publish(self, topic: str, **data: Any) -> None:
        ev = Event(topic, data)
        with self._lock:
            handlers = list(self._subs.get(topic, ())) + list(self._subs.get(self.WILDCARD, ()))
        for h in handlers:
            try:
                h(ev)
            except Exception:  # un suscriptor roto no debe tumbar al emisor
                log.exception("Error en suscriptor de %s", topic)


# Catálogo de temas (documentación viva; los strings se usan tal cual)
class Topics:
    # OBS
    OBS_CONNECTED = "obs.connected"
    OBS_DISCONNECTED = "obs.disconnected"
    OBS_RECORD_STARTED = "obs.record.started"      # path (puede ser None)
    OBS_RECORD_STOPPED = "obs.record.stopped"      # path
    OBS_RECORD_PAUSED = "obs.record.paused"
    OBS_RECORD_RESUMED = "obs.record.resumed"
    OBS_RECORD_STATUS = "obs.record.status"        # active, paused, duration_ms, mono
    OBS_GAME_HINT = "obs.game.hint"                # name
    # Sesión
    SESSION_STARTED = "session.started"            # session (dict)
    SESSION_STOPPED = "session.stopped"            # session (dict)
    SESSION_UPDATED = "session.updated"            # session (dict)
    SESSION_TICK = "session.tick"                  # position_ms, paused
    # Audio
    AUDIO_STARTED = "audio.started"                # device, samplerate
    AUDIO_STOPPED = "audio.stopped"
    AUDIO_LEVEL = "audio.level"                    # rms_db, peak_db, speech_prob, noise_db
    AUDIO_ERROR = "audio.error"                    # message
    UTTERANCE_START = "audio.utterance.start"      # utterance_id, position_ms, forced
    UTTERANCE_PARTIAL = "audio.utterance.partial"  # utterance_id, audio (np.ndarray), samplerate
    UTTERANCE_END = "audio.utterance.end"          # utterance_id, audio, samplerate, position_ms, duration_ms, forced
    # STT
    STT_STATUS = "stt.status"                      # state: idle|loading|busy, model
    STT_PARTIAL = "stt.partial"                    # utterance_id, text
    STT_FINAL = "stt.final"                        # utterance_id, text, info
    STT_ERROR = "stt.error"
    # Notas
    NOTE_CREATED = "note.created"                  # note (dict)
    NOTE_UPDATED = "note.updated"                  # note (dict)
    NOTE_DELETED = "note.deleted"                  # note_id
    NOTE_DISCARDED = "note.discarded"              # reason, text
    # Contexto
    CONTEXT_UPDATED = "context.updated"            # context (dict)
    # Hotkeys
    HOTKEY_DOWN = "hotkey.down"
    HOTKEY_UP = "hotkey.up"
    # Sistema
    STATUS = "app.status"                          # level: info|warn|error, message
    SETTINGS_CHANGED = "settings.changed"          # settings
    LISTENING_CHANGED = "listening.changed"        # enabled
    MODE_CHANGED = "mode.changed"                  # mode
    PERF_GAME_DETECTED = "perf.game"               # detected (bool), title
    # IA local (fase 2)
    AI_STATUS = "ai.status"                        # state: off|unavailable|idle|loading|busy|deferred, detail, queue
    AI_RESULT = "ai.result"                        # kind: classify|transform|summary, note_id?, session_id?, data
    AI_ERROR = "ai.error"                          # kind, message, note_id?, session_id?
    RELATIONS_UPDATED = "relations.updated"        # note_id, related: list[{id, kind, weight}]
    # Fase 3: editor, clips, guiones, búsqueda, momentos
    JOB_PROGRESS = "job.progress"                  # job: clips|reclaimer|moments|index|script, fraction 0-1, text
    JOB_DONE = "job.done"                          # job, ok (bool), message, result (dict)

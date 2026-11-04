"""Gestor de notas: convierte frases transcritas en notas con timestamp de grabación.

Filtra lo que claramente no es un comentario (vacío, muletillas sueltas, alucinaciones
del modelo) y delega la clasificación en el motor de contexto (fase 2, sustituible).
Las correcciones del usuario se registran solo si la personalización está activada.
"""
from __future__ import annotations

import logging
import re
import threading
from typing import Any

from cronista.core.events import EventBus, Topics
from cronista.core.settings import Settings
from cronista.notes.store import Store
from cronista.session.manager import SessionManager

log = logging.getLogger(__name__)

MARKERS = {
    "": "Sin marcador",
    "important": "⭐ Importante",
    "video_idea": "🔥 Idea para video",
    "use_in_video": "🎬 Usar en video",
    "develop": "📝 Desarrollar después",
    "problem": "❌ Problema",
    "liked": "👍 Algo que me gustó",
}

USER_LOCKABLE = ("text", "scope", "functions", "topics", "refs")

_FILLER_ONLY = re.compile(
    r"^[\s,.;:!?¡¿\-–—…]*((eh+|em+|mm+|hm+|ah+|oh+|uh+|ok(ay)?|sí|si|no|bueno|vale|ya|a ver|este|pues|o sea)"
    r"[\s,.;:!?¡¿\-–—…]*){1,4}$", re.IGNORECASE)


def is_relevant(text: str, forced: bool = False) -> tuple[bool, str]:
    t = (text or "").strip()
    if not t:
        return False, "empty"
    if forced:
        return True, ""
    words = re.findall(r"[\wáéíóúñü]+", t, flags=re.IGNORECASE)
    if len(words) < 2:
        return False, "too_short"
    if _FILLER_ONLY.match(t):
        return False, "filler"
    return True, ""


class NoteManager:
    def __init__(self, bus: EventBus, settings: Settings, store: Store, sessions: SessionManager,
                 context_engine=None) -> None:
        self.bus = bus
        self.settings = settings
        self.store = store
        self.sessions = sessions
        self.context = context_engine
        self._positions: dict[str, int | None] = {}
        self._lock = threading.Lock()
        self.last_note: dict | None = None
        bus.subscribe(Topics.UTTERANCE_START, self._on_utt_start)
        bus.subscribe(Topics.STT_FINAL, self._on_final)

    # ------------------------------------------------------------------
    def _on_utt_start(self, ev) -> None:
        if not self.sessions.active and ev.get("forced") and self.settings.general.auto_manual_session:
            self.sessions.start(mode="manual")
        pos = self.sessions.position_at(float(ev.start_mono)) if self.sessions.active else None
        with self._lock:
            self._positions[ev.utterance_id] = pos
            if len(self._positions) > 200:
                for k in list(self._positions)[:-100]:
                    self._positions.pop(k, None)

    def _on_final(self, ev) -> None:
        text = (ev.get("text") or "").strip()
        forced = bool(ev.get("forced"))
        ok, reason = is_relevant(text, forced)
        if not ok:
            self.bus.publish(Topics.NOTE_DISCARDED, reason=reason, text=text, utterance_id=ev.utterance_id)
            return
        with self._lock:
            pos = self._positions.pop(ev.utterance_id, None)
        if pos is None and ev.get("start_mono") is not None and self.sessions.active:
            pos = self.sessions.position_at(float(ev.start_mono))
        sess = self.sessions.current
        if sess is None:
            if not self.settings.general.auto_manual_session:
                self.bus.publish(Topics.NOTE_DISCARDED, reason="no_session", text=text, utterance_id=ev.utterance_id)
                return
            sess = self.sessions.start(mode="manual")
            pos = self.sessions.position_now()
        note: dict[str, Any] = {
            "session_id": sess["id"], "position_ms": pos, "duration_ms": int(ev.get("duration_ms") or 0),
            "text_raw": text, "text": text, "source": "hotkey" if forced else "auto", "forced": forced,
            "stt_info": ev.get("info") or {},
        }
        self._classify(note, sess)
        note["id"] = self.store.create_note(note)
        self.last_note = note
        log.info("Nota #%d [%s] %s", note["id"], pos, text[:60])
        self.bus.publish(Topics.NOTE_CREATED, note=dict(note))

    def _classify(self, note: dict, sess: dict) -> None:
        if self.context is None:
            return
        try:
            recent = self.store.list_notes(sess["id"])[-5:]
            result = self.context.classify(note["text"], game=sess.get("game", ""), recent=recent)
            note.update({k: result[k] for k in ("scope", "functions", "topics", "refs", "confidence", "ambiguous")
                         if k in result})
            note["context"] = dict(result.get("context", {}), source="heuristic")
            self.bus.publish(Topics.CONTEXT_UPDATED, context=self.context.state())
        except Exception:
            log.exception("Fallo en el motor de contexto")

    # ---- API para la UI ------------------------------------------------------
    def create_manual(self, text: str, position_ms: int | None = None) -> dict | None:
        sess = self.sessions.current
        if sess is None:
            return None
        pos = position_ms if position_ms is not None else self.sessions.position_now()
        note = {"session_id": sess["id"], "position_ms": pos, "text_raw": text, "text": text, "source": "manual"}
        self._classify(note, sess)
        note["id"] = self.store.create_note(note)
        self.bus.publish(Topics.NOTE_CREATED, note=dict(note))
        return note

    def create_from_transcript(self, sess: dict, position_ms: int, duration_ms: int, text: str,
                               stt_info: dict | None = None) -> dict:
        """Nota de una grabación transcrita (posición = segundo exacto del archivo)."""
        note = {"session_id": sess["id"], "position_ms": int(position_ms), "duration_ms": int(duration_ms),
                "text_raw": text, "text": text, "source": "file", "stt_info": stt_info or {}}
        self._classify(note, sess)
        note["id"] = self.store.create_note(note)
        self.bus.publish(Topics.NOTE_CREATED, note=dict(note))
        return note

    def update(self, note_id: int, learn_field: str | None = None, **fields: Any) -> dict | None:
        old = self.store.get_note(note_id)
        if old is None:
            return None
        if learn_field and self.settings.learning.learn_corrections and learn_field in fields:
            self.store.add_correction(note_id, learn_field, old.get(learn_field), fields[learn_field], old["text"])
        if "text" in fields and fields["text"] != old["text"]:
            fields["user_edited"] = 1
        # Lo que el usuario corrige o confirma queda bloqueado: la IA nunca lo sobrescribe después.
        # (Confirmar sin cambiar, p. ej. "✓ Actual" sobre una nota ya marcada como actual, también cuenta.)
        locked = [f for f in USER_LOCKABLE if f in fields and (f != "text" or fields[f] != old.get(f))]
        if locked:
            ctx = dict(old.get("context") or {})
            ctx["user_locked"] = sorted(set(ctx.get("user_locked", [])) | set(locked))
            fields["context"] = ctx
        self.store.update_note(note_id, **fields)
        new = self.store.get_note(note_id)
        self.bus.publish(Topics.NOTE_UPDATED, note=new)
        return new

    def delete(self, note_id: int) -> None:
        self.store.delete_note(note_id)
        self.bus.publish(Topics.NOTE_DELETED, note_id=note_id)

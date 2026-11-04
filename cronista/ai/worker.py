"""Trabajador de análisis en segundo plano (fase 2).

Un único hilo procesa una cola con prioridades:
  0 = pedido explícito del usuario (corregir/mejorar una nota, analizar sesión)
  1 = correlación entre comentarios (barata, sin IA)
  2 = clasificación automática con IA local de cada nota nueva

Política de rendimiento (Configuración → IA local → "Mientras juegas"):
  - defer (por defecto): con un juego en primer plano, los trabajos automáticos de IA esperan y el servidor
    de IA se cierra para devolver la VRAM al juego. La clasificación heurística instantánea sigue funcionando.
  - cpu: la IA trabaja durante el juego pero sin tocar la GPU.
  - gpu: sin restricciones.
Los pedidos explícitos del usuario no se aplazan (si los pide, es que no está jugando en ese momento).

Nunca sobrescribe lo que el usuario corrigió: respeta context["user_locked"].
"""
from __future__ import annotations

import itertools
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from cronista.core.events import EventBus, Topics
from cronista.core.settings import Settings

log = logging.getLogger(__name__)

PRIO_USER, PRIO_RELATE, PRIO_CLASSIFY = 0, 1, 2
TRANSFORM_FIELDS = {"correct": "text_corrected", "improve": "text_improved", "script": "text_script"}
_LOCKABLE = ("scope", "functions", "topics", "refs")


@dataclass(order=True)
class Job:
    priority: int
    seq: int
    kind: str = field(compare=False)                 # relate | classify | transform | summary | quit
    payload: dict = field(compare=False, default_factory=dict)

    @property
    def needs_llm(self) -> bool:
        return self.kind in ("classify", "transform", "summary")

    @property
    def by_user(self) -> bool:
        return self.priority == PRIO_USER


class AnalysisWorker:
    def __init__(self, bus: EventBus, settings: Settings, store, sessions, context, personalizer,
                 llm_factory=None) -> None:
        self.bus = bus
        self.settings = settings
        self.store = store
        self.sessions = sessions
        self.context = context
        self.personalizer = personalizer
        self._llm_factory = llm_factory
        self._llm = None
        self._q: queue.PriorityQueue[Job] = queue.PriorityQueue()
        self._seq = itertools.count()
        self._deferred: list[Job] = []
        self._pending_classify: set[int] = set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.game_active = False
        self.state = "off"
        self.detail = ""
        self._unavailable_reason = ""
        bus.subscribe(Topics.NOTE_CREATED, self._on_note_created)
        bus.subscribe(Topics.NOTE_UPDATED, self._on_note_updated)
        bus.subscribe(Topics.PERF_GAME_DETECTED, self._on_game)
        bus.subscribe(Topics.SETTINGS_CHANGED, lambda e: self.reset_llm())

    # ---- ciclo de vida -----------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ai-worker", daemon=True)
        self._thread.start()
        self._publish_status("idle" if self.settings.llm.enabled else "off")

    def stop(self) -> None:
        self._stop.set()
        self._q.put(Job(-1, -1, "quit"))
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=10)
        self._unload()

    def reset_llm(self) -> None:
        """Tras cambiar la configuración: el cliente se recrea en el próximo uso."""
        with self._lock:
            old, self._llm = self._llm, None
            self._unavailable_reason = ""
        if old is not None:
            try:
                old.unload()
            except Exception:
                log.debug("Error al descargar la IA", exc_info=True)
        if self.state == "unavailable":
            self._publish_status("idle" if self.settings.llm.enabled else "off")

    # ---- API pública (la usa la UI) -----------------------------------------------
    def request_transform(self, note_id: int, mode: str) -> None:
        self._put(PRIO_USER, "transform", note_id=note_id, mode=mode)

    def request_summary(self, session_id: int, which: str = "local") -> None:
        self._put(PRIO_USER, "summary", session_id=session_id, which=which)

    def request_classify(self, note_id: int) -> None:
        """Reclasificar una nota a petición del usuario (no se aplaza)."""
        self._put(PRIO_USER, "classify", note_id=note_id, force=True)

    def reclassify_session(self, session_id: int) -> int:
        notes = self.store.list_notes(session_id)
        for n in notes:
            self._put(PRIO_CLASSIFY, "classify", note_id=n["id"], force=True)
        return len(notes)

    def availability(self) -> tuple[bool, str]:
        if not self.settings.llm.enabled:
            return False, "La IA local está desactivada en Configuración."
        llm = self._get_llm()
        if llm is None:
            return False, self._unavailable_reason or "IA local no disponible."
        return llm.available()

    def queue_size(self) -> int:
        return self._q.qsize() + len(self._deferred)

    # ---- eventos -----------------------------------------------------------------------
    def _on_note_created(self, ev) -> None:
        note = ev.note
        self._put(PRIO_RELATE, "relate", note_id=note["id"])
        if self.settings.llm.enabled and self.settings.llm.auto_classify and note.get("text"):
            self._put(PRIO_CLASSIFY, "classify", note_id=note["id"])

    def _on_note_updated(self, ev) -> None:
        if ev.get("source") == "ai":
            return   # lo publicamos nosotros: evitar bucles
        note = ev.get("note") or {}
        if note.get("id"):
            self._put(PRIO_RELATE, "relate", note_id=note["id"])
            if ev.get("source") == "refine" and self.settings.llm.enabled and self.settings.llm.auto_classify:
                self._put(PRIO_CLASSIFY, "classify", note_id=note["id"], force=True)   # texto mejorado

    def _on_game(self, ev) -> None:
        self.game_active = bool(ev.get("detected"))
        policy = self.settings.llm.during_game
        if self.game_active and policy == "defer":
            # devolver la VRAM al juego; se recargará al terminar
            self._unload()
        elif self.game_active and policy == "cpu":
            llm = self._llm
            if llm is not None:
                llm.set_device("cpu")
        if not self.game_active:
            with self._lock:
                pending, self._deferred = self._deferred, []
            for job in pending:
                self._q.put(job)
            if pending:
                log.info("Juego fuera de primer plano: reanudando %d análisis aplazados", len(pending))
        self._publish_status(self.state)

    # ---- cola ------------------------------------------------------------------------
    def _put(self, priority: int, kind: str, **payload: Any) -> None:
        if kind == "classify" and not payload.get("force"):
            nid = payload["note_id"]
            with self._lock:
                if nid in self._pending_classify:
                    return
                self._pending_classify.add(nid)
        self._q.put(Job(priority, next(self._seq), kind, payload))

    def _should_defer(self, job: Job) -> bool:
        return (job.needs_llm and not job.by_user and self.game_active
                and self.settings.llm.during_game == "defer")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._q.get(timeout=5.0)
            except queue.Empty:
                self._idle_tick()
                continue
            if job.kind == "quit":
                break
            if self._should_defer(job):
                with self._lock:
                    self._deferred.append(job)
                self._publish_status("deferred", detail="Esperando a que el juego no esté en primer plano")
                continue
            try:
                getattr(self, f"_do_{job.kind}")(job)
            except Exception as e:
                self._handle_error(job, e)
            finally:
                if job.kind == "classify":
                    with self._lock:
                        self._pending_classify.discard(job.payload.get("note_id"))
                # Si el juego pasó a primer plano durante esta tarea, liberar la VRAM ahora
                # (unload no interrumpe una llamada en curso, así que lo hacemos al terminarla).
                if self.game_active and self.settings.llm.during_game == "defer":
                    self._unload()
            if self._q.empty() and not self._deferred and self.state not in ("unavailable", "idle", "off"):
                # "no disponible" se mantiene visible hasta que cambie la configuración (reset_llm)
                self._publish_status("idle" if self.settings.llm.enabled else "off", detail=self.detail)

    def _idle_tick(self) -> None:
        llm = self._llm
        if llm is not None:
            try:
                before = self._llm_running()
                llm.idle_check()
                if before and not self._llm_running():
                    log.info("IA local descargada por inactividad")
                    self._publish_status("idle", detail="Modelo descargado (inactivo)")
            except Exception:
                log.debug("idle_check falló", exc_info=True)

    # ---- IA ---------------------------------------------------------------------------
    def _get_llm(self):
        with self._lock:
            if self._llm is not None:
                return self._llm
            if not self.settings.llm.enabled:
                return None
            try:
                if self._llm_factory is not None:
                    llm = self._llm_factory(self.settings)
                else:
                    from cronista.ai.local_llm import build_chat_llm
                    llm = build_chat_llm(self.settings, "local")
            except Exception as e:
                self._unavailable_reason = f"No se pudo preparar la IA local: {e}"
                return None
            if llm is None:
                return None
            ok, why = llm.available()
            if not ok:
                self._unavailable_reason = why
            self._llm = llm
            return llm

    def _llm_running(self) -> bool:
        llm = self._llm
        if llm is None:
            return False
        server = getattr(llm, "server", None)
        if server is not None:
            return bool(getattr(server, "running", False))
        return getattr(llm, "state", "off") not in ("off", "")

    def _prepare_llm(self, which: str = "local"):
        """Devuelve un cliente listo o lanza RuntimeError con un motivo legible."""
        if which == "api":
            from cronista.ai.local_llm import build_chat_llm
            llm = build_chat_llm(self.settings, "api")
            if llm is None:
                raise RuntimeError("No hay un proveedor de API configurado (Configuración → IA / Chatbot).")
            return llm
        llm = self._get_llm()
        if llm is None:
            raise RuntimeError(self._unavailable_reason or "La IA local está desactivada.")
        ok, why = llm.available()
        if not ok:
            raise RuntimeError(why)
        device = self.settings.llm.device
        if self.game_active and self.settings.llm.during_game == "cpu":
            device = "cpu"
        llm.set_device(device)
        if not self._llm_running():
            self._publish_status("loading", detail="Cargando modelo de IA local…")
        return llm

    def _unload(self) -> None:
        llm = self._llm
        if llm is not None and self._llm_running():
            try:
                llm.unload()
                log.info("IA local descargada (libera VRAM/RAM)")
            except Exception:
                log.debug("unload falló", exc_info=True)

    # ---- trabajos ----------------------------------------------------------------------
    def _do_relate(self, job: Job) -> None:
        from cronista.notes.relations import compute_relations
        note = self.store.get_note(job.payload["note_id"])
        if not note or note.get("deleted"):
            return
        others = [n for n in self.store.list_notes(note["session_id"]) if n["id"] != note["id"]]
        rels = compute_relations(note, others)
        self.store.replace_relations(note["id"], rels)
        self.bus.publish(Topics.RELATIONS_UPDATED, note_id=note["id"],
                         related=[{"id": i, "kind": k, "weight": w} for i, k, w in rels])

    def _do_classify(self, job: Job) -> None:
        from cronista.ai import tasks
        note = self.store.get_note(job.payload["note_id"])
        if not note or note.get("deleted") or not note.get("text"):
            return
        ctx = dict(note.get("context") or {})
        if ctx.get("source") == "llm" and not job.payload.get("force"):
            return
        try:
            llm = self._prepare_llm()
        except RuntimeError as e:
            # Sin IA disponible: la nota conserva la clasificación heurística. Solo avisamos una vez.
            if job.by_user:
                raise
            if self.state != "unavailable":
                self._publish_status("unavailable", detail=str(e))
            return
        sess = self.store.get_session(note["session_id"]) or {}
        siblings = self.store.list_notes(note["session_id"])
        earlier = [n for n in siblings if (n.get("position_ms") or 0) <= (note.get("position_ms") or 0)
                   and n["id"] != note["id"]][-3:]
        is_live = bool(self.sessions.current and self.sessions.current.get("id") == note["session_id"])
        prev_state = self.context.state() if is_live else {}
        examples = self.personalizer.examples_for(note["text"]) if self.personalizer else []
        self._publish_status("busy", detail="Clasificando nota…")
        res = tasks.classify_note(llm, note["text"], game=sess.get("game", ""), recent=earlier,
                                  examples=examples, prev_state=prev_state)
        locked = set(ctx.get("user_locked", []))
        fields: dict[str, Any] = {f: res[f] for f in _LOCKABLE if f not in locked and f in res}
        fields["confidence"] = res.get("confidence", 0.0)
        fields["ambiguous"] = int(bool(res.get("ambiguous")))
        if ctx.get("source") != "llm":
            ctx["heuristic"] = {k: note.get(k) for k in ("scope", "functions", "topics", "confidence")}
        ctx.update({"source": "llm", "subtopic": res.get("subtopic", ""), "rationale": res.get("rationale", ""),
                    "llm_at": datetime.now().isoformat(timespec="seconds")})
        for key in ("chapter", "section"):
            # la detección por reglas ("misión cinco" → "Misión 5") es exacta: solo se completa si faltaba
            if res.get(key) and not ctx.get(key):
                ctx[key] = res[key]
            elif ctx.get(key):
                res[key] = ctx[key]
        fields["context"] = ctx
        self.store.update_note(note["id"], **fields)
        new = self.store.get_note(note["id"])
        self.bus.publish(Topics.NOTE_UPDATED, note=new, source="ai")
        self.bus.publish(Topics.AI_RESULT, kind="classify", note_id=note["id"], data=res)
        if self.personalizer:
            for r in res.get("refs") or []:
                self.personalizer.remember_game(r.get("name", ""))
        # el estado vivo de la sesión solo se actualiza con la nota más reciente
        if is_live and siblings and siblings[-1]["id"] == note["id"]:
            self.context.apply_llm(res)
            self.bus.publish(Topics.CONTEXT_UPDATED, context=self.context.state())
        self._put(PRIO_RELATE, "relate", note_id=note["id"])   # tema/subtema nuevos → relaciones nuevas

    def _do_transform(self, job: Job) -> None:
        from cronista.ai import tasks
        mode = job.payload["mode"]
        if mode not in TRANSFORM_FIELDS:
            raise ValueError(f"Modo desconocido: {mode}")
        note = self.store.get_note(job.payload["note_id"])
        if not note:
            return
        llm = self._prepare_llm()
        sess = self.store.get_session(note["session_id"]) or {}
        self._publish_status("busy", detail=f"{tasks.TRANSFORM_MODES[mode]['title']}…")
        text = tasks.transform_note(llm, note["text"], mode, game=sess.get("game", ""))
        self.store.update_note(note["id"], **{TRANSFORM_FIELDS[mode]: text})
        new = self.store.get_note(note["id"])
        self.bus.publish(Topics.NOTE_UPDATED, note=new, source="ai")
        self.bus.publish(Topics.AI_RESULT, kind="transform", note_id=note["id"], data={"mode": mode, "text": text})

    def _do_summary(self, job: Job) -> None:
        from cronista.ai import tasks
        sid = job.payload["session_id"]
        which = job.payload.get("which", "local")
        sess = self.store.get_session(sid)
        if not sess:
            return
        notes = self.store.list_notes(sid)
        llm = self._prepare_llm(which)
        self._publish_status("busy", detail=f"Analizando sesión ({len(notes)} notas)…")
        t0 = time.monotonic()
        summary = tasks.summarize_session(llm, sess, notes)
        md = tasks.summary_to_markdown(summary, sess)
        stored = dict(sess.get("summary") or {})
        stored[which] = dict(summary, markdown=md, created_at=datetime.now().isoformat(timespec="seconds"),
                             seconds=round(time.monotonic() - t0, 1))
        self.store.update_session(sid, summary=stored)
        self.bus.publish(Topics.AI_RESULT, kind="summary", session_id=sid,
                         data={"summary": summary, "markdown": md, "which": which})

    # ---- estado y errores ----------------------------------------------------------------
    def _handle_error(self, job: Job, e: Exception) -> None:
        log.warning("Trabajo de IA '%s' falló: %s", job.kind, e)
        msg = str(e) or type(e).__name__
        if job.by_user or job.kind == "relate":
            self.bus.publish(Topics.AI_ERROR, kind=job.kind, message=msg, note_id=job.payload.get("note_id"),
                             session_id=job.payload.get("session_id"))
        self._publish_status("idle", detail=msg[:200])

    def _publish_status(self, state: str, detail: str = "") -> None:
        self.state = state
        self.detail = detail
        self.bus.publish(Topics.AI_STATUS, state=state, detail=detail, queue=self.queue_size())

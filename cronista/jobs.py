"""Ejecutor de tareas largas pedidas por el usuario (exportar clips, crear proyecto de Reclaimer,
analizar momentos, indexar búsqueda, generar guion). Un solo hilo: nunca dos tareas pesadas a la vez.

Cada tarea es fn(progress, cancelled) -> dict, donde progress(fraccion 0-1, texto) informa y
cancelled() indica si el usuario la canceló. Publica Topics.JOB_PROGRESS y Topics.JOB_DONE.
"""
from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from cronista.core.events import EventBus, Topics

log = logging.getLogger(__name__)

Progress = Callable[[float, str], None]
JobFn = Callable[[Progress, Callable[[], bool]], dict]


@dataclass
class _Job:
    name: str
    fn: JobFn
    label: str = ""
    cancel: threading.Event = field(default_factory=threading.Event)


class JobRunner:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self._q: queue.Queue[_Job | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self.current: _Job | None = None
        self._pending: list[_Job] = []
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="jobs", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.cancel_all()
        self._q.put(None)
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=10)

    def submit(self, name: str, fn: JobFn, label: str = "") -> None:
        job = _Job(name, fn, label or name)
        with self._lock:
            self._pending.append(job)
        self.start()
        self._q.put(job)
        self.bus.publish(Topics.JOB_PROGRESS, job=name, fraction=0.0, text=f"En cola: {job.label}")

    def busy(self, name: str | None = None) -> bool:
        with self._lock:
            jobs = ([self.current] if self.current else []) + self._pending
        return any(j and (name is None or j.name == name) for j in jobs)

    def pending(self) -> int:
        """Tareas en cola que aún no han empezado (sin contar la que está en marcha)."""
        with self._lock:
            return len(self._pending)

    def cancel(self, name: str) -> None:
        with self._lock:
            for j in ([self.current] if self.current else []) + self._pending:
                if j and j.name == name:
                    j.cancel.set()

    def cancel_all(self) -> None:
        with self._lock:
            for j in ([self.current] if self.current else []) + self._pending:
                if j:
                    j.cancel.set()

    def _run(self) -> None:
        while True:
            job = self._q.get()
            if job is None:
                break
            with self._lock:
                if job in self._pending:
                    self._pending.remove(job)
                self.current = job
            if job.cancel.is_set():
                self._done(job, False, "Cancelado", {})
                continue

            def progress(fraction: float, text: str = "", _job=job) -> None:
                self.bus.publish(Topics.JOB_PROGRESS, job=_job.name, fraction=max(0.0, min(1.0, float(fraction))),
                                 text=text)

            try:
                result = job.fn(progress, job.cancel.is_set) or {}
                if job.cancel.is_set():
                    self._done(job, False, "Cancelado", result)
                else:
                    self._done(job, True, result.get("message", "Listo"), result)
            except Exception as e:
                log.exception("Tarea '%s' falló", job.name)
                self._done(job, False, str(e) or type(e).__name__, {})
            finally:
                with self._lock:
                    self.current = None

    def _done(self, job: _Job, ok: bool, message: str, result: dict[str, Any]) -> None:
        self.bus.publish(Topics.JOB_DONE, job=job.name, ok=ok, message=message, result=result)

"""Gestor de sesiones: una sesión = una grabación de OBS (o una sesión manual).

Escucha los eventos de OBS para iniciar/detener/pausar, mantiene el reloj de
grabación, asigna a cada frase detectada su posición exacta y persiste la sesión.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from cronista.core.events import EventBus, Topics
from cronista.core.settings import Settings
from cronista.notes.store import Store
from cronista.session.clock import RecordingClock

log = logging.getLogger(__name__)


class SessionManager:
    def __init__(self, bus: EventBus, settings: Settings, store: Store) -> None:
        self.bus = bus
        self.settings = settings
        self.store = store
        self.clock = RecordingClock()
        self.current: dict | None = None
        self.game_hint = ""
        self._lock = threading.RLock()
        self._tick_thread: threading.Thread | None = None
        self._stop = threading.Event()
        bus.subscribe(Topics.OBS_RECORD_STARTED, self._on_obs_started)
        bus.subscribe(Topics.OBS_RECORD_STOPPED, self._on_obs_stopped)
        bus.subscribe(Topics.OBS_RECORD_PAUSED, lambda e: self.pause(e.get("mono")))
        bus.subscribe(Topics.OBS_RECORD_RESUMED, lambda e: self.resume(e.get("mono")))
        bus.subscribe(Topics.OBS_RECORD_STATUS, self._on_obs_status)
        bus.subscribe(Topics.OBS_DISCONNECTED, self._on_obs_disconnected)
        bus.subscribe(Topics.OBS_GAME_HINT, self._on_game_hint)

    # ------------------------------------------------------------------
    @property
    def active(self) -> bool:
        return self.current is not None

    @property
    def paused(self) -> bool:
        return self.current is not None and not self.clock.running

    def start(self, mode: str = "manual", game: str = "", obs_path: str | None = None,
              mono: float | None = None, pos_ms: float = 0.0) -> dict:
        with self._lock:
            if self.current is not None:
                if self.current["mode"] == "manual" and mode == "obs":
                    # Una grabación de OBS empieza durante una sesión manual: la sesión pasa a
                    # seguir el reloj de OBS desde este momento.
                    self.current["mode"] = "obs"
                    self.current["obs_path"] = obs_path
                    self.clock.start(mono, pos_ms)
                    self.store.update_session(self.current["id"], mode="obs", obs_path=obs_path)
                    self.bus.publish(Topics.SESSION_UPDATED, session=dict(self.current))
                return self.current
            game = game or self.game_hint or self.settings.general.default_game or "Sin título"
            now = datetime.now()
            sess = {
                "id": None, "game": game, "started_at": now.isoformat(timespec="seconds"),
                "ended_at": None, "duration_ms": 0, "obs_path": obs_path, "mode": mode,
                "title": f"{game} — {now.strftime('%d/%m/%Y %H:%M')}",
            }
            sess["id"] = self.store.create_session(sess)
            self.current = sess
            self.clock.start(mono, pos_ms)
            self._start_ticker()
            log.info("Sesión iniciada (%s): %s", mode, sess["title"])
            self.bus.publish(Topics.SESSION_STARTED, session=dict(sess))
            return sess

    def stop(self, obs_path: str | None = None, duration_ms: float | None = None) -> dict | None:
        with self._lock:
            sess = self.current
            if sess is None:
                return None
            self.current = None
            self._stop_ticker()
            dur = duration_ms if duration_ms else self.clock.position_now()
            self.clock.stop()
            sess["ended_at"] = datetime.now().isoformat(timespec="seconds")
            sess["duration_ms"] = int(dur)
            if obs_path:
                sess["obs_path"] = obs_path
            self.store.update_session(sess["id"], ended_at=sess["ended_at"], duration_ms=sess["duration_ms"],
                                      obs_path=sess["obs_path"])
            log.info("Sesión terminada: %s (%d ms)", sess["title"], sess["duration_ms"])
            self.bus.publish(Topics.SESSION_STOPPED, session=dict(sess))
            return sess

    def pause(self, mono: float | None = None) -> None:
        with self._lock:
            if self.current:
                self.clock.pause(mono)
                self.bus.publish(Topics.SESSION_UPDATED, session=dict(self.current), paused=True)

    def resume(self, mono: float | None = None) -> None:
        with self._lock:
            if self.current:
                self.clock.resume(mono)
                self.bus.publish(Topics.SESSION_UPDATED, session=dict(self.current), paused=False)

    def set_game(self, name: str) -> None:
        with self._lock:
            if self.current and name:
                self.current["game"] = name
                self.store.update_session(self.current["id"], game=name)
                self.bus.publish(Topics.SESSION_UPDATED, session=dict(self.current))

    def position_at(self, mono: float) -> int | None:
        if self.current is None:
            return None
        return int(self.clock.position_at(mono))

    def position_now(self) -> int | None:
        return self.position_at(time.monotonic())

    # ---- eventos OBS ------------------------------------------------------
    def _on_obs_started(self, ev) -> None:
        if not self.settings.obs.auto_session:
            return
        self.start(mode="obs", obs_path=ev.get("path"), mono=ev.get("mono"),
                   pos_ms=float(ev.get("duration_ms", 0) or 0))

    def _on_obs_stopped(self, ev) -> None:
        if self.current and self.current["mode"] == "obs":
            self.stop(obs_path=ev.get("path"), duration_ms=ev.get("duration_ms") or None)

    def _on_obs_status(self, ev) -> None:
        if self.current and self.current["mode"] == "obs" and ev.get("active"):
            drift = self.clock.sync(float(ev.duration_ms), ev.get("mono"), paused=bool(ev.get("paused")))
            if abs(drift) > 1000:
                log.info("Reloj corregido %+.0f ms con OBS", drift)

    def _on_obs_disconnected(self, ev) -> None:
        # No cerramos la sesión: OBS puede seguir grabando aunque perdamos el websocket.
        if self.current and self.current["mode"] == "obs":
            self.bus.publish(Topics.STATUS, level="warn",
                             message="Conexión con OBS perdida; la sesión sigue con reloj local.")

    def _on_game_hint(self, ev) -> None:
        name = ev.get("name", "")
        if not name:
            return
        self.game_hint = name
        with self._lock:
            if self.current and self.current["game"] in ("Sin título", ""):
                self.set_game(name)

    # ---- tick para la UI --------------------------------------------------
    def _start_ticker(self) -> None:
        self._stop.clear()
        if self._tick_thread and self._tick_thread.is_alive():
            return
        self._tick_thread = threading.Thread(target=self._tick, name="session-tick", daemon=True)
        self._tick_thread.start()

    def _stop_ticker(self) -> None:
        self._stop.set()

    def _tick(self) -> None:
        while not self._stop.wait(1.0):
            if self.current is None:
                break
            self.bus.publish(Topics.SESSION_TICK, position_ms=int(self.clock.position_now()),
                             paused=not self.clock.running)

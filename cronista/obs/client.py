"""Integración con OBS Studio mediante obs-websocket (protocolo 5.x, integrado en OBS >= 28).

Responsabilidades:
  - Conectar y reconectar automáticamente (hilo propio, bajo consumo: un sondeo ligero cada N s).
  - Descubrir host/puerto/contraseña desde la configuración local de OBS si el usuario lo permite.
  - Publicar en el bus: conectado/desconectado, grabación iniciada/detenida/pausada/reanudada,
    y un estado periódico con la duración real de la grabación (para sincronizar el reloj).
  - Sugerir el nombre del juego a partir de la fuente "Captura de juego" o de la escena activa.

Nunca modifica el audio de OBS ni sus fuentes; solo lee estado y recibe eventos.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from cronista.core.events import EventBus, Topics
from cronista.core.settings import Settings

log = logging.getLogger(__name__)

OUTPUT_STARTED = "OBS_WEBSOCKET_OUTPUT_STARTED"
OUTPUT_STOPPED = "OBS_WEBSOCKET_OUTPUT_STOPPED"
OUTPUT_PAUSED = "OBS_WEBSOCKET_OUTPUT_PAUSED"
OUTPUT_RESUMED = "OBS_WEBSOCKET_OUTPUT_RESUMED"


def obs_config_dir() -> Path:
    return Path(os.environ.get("APPDATA", Path.home())) / "obs-studio"


def obs_websocket_config_path() -> Path:
    return obs_config_dir() / "plugin_config" / "obs-websocket" / "config.json"


@dataclass
class ObsWsConfig:
    enabled: bool
    port: int
    password: str
    auth_required: bool


def read_obs_websocket_config() -> ObsWsConfig | None:
    """Lee la configuración local del servidor WebSocket de OBS (solo lectura)."""
    p = obs_websocket_config_path()
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return ObsWsConfig(bool(raw.get("server_enabled", False)), int(raw.get("server_port", 4455)),
                           str(raw.get("server_password", "")), bool(raw.get("auth_required", True)))
    except Exception:
        log.exception("No se pudo leer la configuración de obs-websocket")
        return None


def obs_running() -> bool:
    try:
        import psutil
        for p in psutil.process_iter(["name"]):
            if (p.info.get("name") or "").lower() in ("obs64.exe", "obs32.exe", "obs.exe"):
                return True
    except Exception:
        pass
    return False


def enable_obs_websocket_server(port: int = 4455) -> tuple[bool, str]:
    """Activa el servidor WebSocket en la configuración de OBS. Solo con OBS cerrado
    (OBS sobrescribe su configuración al salir). Conserva la contraseña existente."""
    if obs_running():
        return False, "Cierra OBS antes de activar el servidor WebSocket."
    p = obs_websocket_config_path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        raw["server_enabled"] = True
        raw.setdefault("server_port", port)
        raw.setdefault("auth_required", True)
        raw.setdefault("first_load", False)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        return True, "Servidor WebSocket activado. Inicia OBS."
    except Exception as e:
        return False, f"No se pudo modificar la configuración de OBS: {e}"


_WINDOW_RE = re.compile(r"^(?P<title>.*?):(?P<cls>[^:]*):(?P<exe>[^:]*)$")


def _clean_game_title(title: str) -> str:
    title = title.replace("#3A", ":").strip()
    for sep in (" - ", " | ", " — "):
        if sep in title:
            parts = [x.strip() for x in title.split(sep) if x.strip()]
            if parts:
                title = parts[0] if len(parts[0]) >= 4 else max(parts, key=len)
    return title[:80]


class OBSClient:
    """Mantiene la conexión en un hilo; expone métodos seguros para llamar desde la UI."""

    def __init__(self, bus: EventBus, settings: Settings) -> None:
        self.bus = bus
        self.settings = settings
        self._req = None
        self._ev = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self.connected = False
        self.recording = False
        self.paused = False
        self.last_status: dict = {}
        self.last_error = ""
        self.version = ""
        self.record_dir = ""
        self._last_path: str | None = None
        self._wake = threading.Event()

    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="obs-client", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._disconnect()
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=3)

    def reconnect_now(self) -> None:
        self._disconnect()
        self.last_error = ""
        self._wake.set()

    # ------------------------------------------------------------------
    def _conn_params(self) -> tuple[str, int, str]:
        o = self.settings.obs
        host, port, password = o.host or "localhost", int(o.port or 4455), o.password or ""
        if o.auto_discover and host in ("localhost", "127.0.0.1"):
            cfg = read_obs_websocket_config()
            if cfg is not None:
                port = cfg.port or port
                if not password and cfg.auth_required:
                    password = cfg.password
                if not cfg.enabled:
                    self.last_error = ("El servidor WebSocket de OBS está desactivado. "
                                       "Actívalo en OBS → Herramientas → Ajustes del servidor WebSocket.")
        return host, port, password

    def _run(self) -> None:
        import obsws_python as obs

        while not self._stop.is_set():
            if not self.connected:
                host, port, password = self._conn_params()
                try:
                    req = obs.ReqClient(host=host, port=port, password=password, timeout=4)
                    ev = obs.EventClient(host=host, port=port, password=password, timeout=4)
                    ev.callback.register([self.on_record_state_changed, self.on_exit_started,
                                          self.on_current_program_scene_changed])
                    with self._lock:
                        self._req, self._ev = req, ev
                        self.connected = True
                    self.last_error = ""
                    self._after_connect()
                except Exception as e:
                    msg = str(e) or type(e).__name__
                    if isinstance(e, (ConnectionRefusedError, OSError)) or "10061" in msg:
                        msg = ("OBS no está abierto o su servidor WebSocket está desactivado "
                               "(Herramientas → Ajustes del servidor WebSocket).")
                    elif "auth" in msg.lower() or "identify" in msg.lower():
                        msg = "OBS rechazó la contraseña del WebSocket. Revísala en Configuración → OBS."
                    if "desactivado" not in self.last_error:
                        self.last_error = msg
                    self._sleep(self.settings.obs.reconnect_interval_s)
                    continue
            self._poll_status()
            self._sleep(self.settings.obs.sync_interval_s)

    def _sleep(self, s: float) -> None:
        self._wake.wait(max(0.5, s))
        self._wake.clear()

    def _after_connect(self) -> None:
        try:
            v = self._req.get_version()
            self.version = f"OBS {v.obs_version} / WS {v.obs_web_socket_version}"
        except Exception:
            self.version = ""
        try:
            self.record_dir = self._req.get_record_directory().record_directory
        except Exception:
            self.record_dir = ""
        log.info("Conectado a %s", self.version or "OBS")
        self.bus.publish(Topics.OBS_CONNECTED, version=self.version, record_dir=self.record_dir)
        self._publish_game_hint()
        self._poll_status(initial=True)

    def _disconnect(self) -> None:
        with self._lock:
            req, ev = self._req, self._ev
            self._req = self._ev = None
            was = self.connected
            self.connected = False
        for c in (ev, req):
            try:
                if c is not None:
                    c.disconnect()
            except Exception:
                pass
        if was:
            self.recording = False
            self.paused = False
            self.bus.publish(Topics.OBS_DISCONNECTED)

    def _alive(self) -> bool:
        ev = self._ev
        return self.connected and ev is not None and ev.worker.is_alive()

    def _poll_status(self, initial: bool = False) -> None:
        if not self._alive():
            if self.connected:
                log.warning("Se perdió la conexión con OBS")
                self._disconnect()
            return
        t0 = time.monotonic()
        try:
            st = self._req.get_record_status()
        except Exception as e:
            log.warning("Error consultando OBS: %s", e)
            self._disconnect()
            return
        t1 = time.monotonic()
        mid = (t0 + t1) / 2
        active = bool(st.output_active)
        paused = bool(getattr(st, "output_paused", False))
        duration_ms = int(getattr(st, "output_duration", 0) or 0)
        self.last_status = {"active": active, "paused": paused, "duration_ms": duration_ms,
                            "timecode": getattr(st, "output_timecode", ""), "mono": mid}
        if active != self.recording:
            self.recording, self.paused = active, paused
            if active:
                self.bus.publish(Topics.OBS_RECORD_STARTED, path=None, duration_ms=duration_ms,
                                 mono=mid, already_running=True)
            else:
                self.bus.publish(Topics.OBS_RECORD_STOPPED, path=self._last_path, duration_ms=duration_ms, mono=mid)
        if active:
            self.bus.publish(Topics.OBS_RECORD_STATUS, active=active, paused=paused,
                             duration_ms=duration_ms, mono=mid)

    # ---- callbacks de eventos (hilo de obsws) ---------------------------
    def on_record_state_changed(self, data) -> None:
        state = getattr(data, "output_state", "")
        path = getattr(data, "output_path", None)
        now = time.monotonic()
        log.info("OBS grabación: %s %s", state, path or "")
        if state == OUTPUT_STARTED:
            self.recording, self.paused = True, False
            self._last_path = path
            self.bus.publish(Topics.OBS_RECORD_STARTED, path=path, duration_ms=0, mono=now, already_running=False)
            self._publish_game_hint()
        elif state == OUTPUT_STOPPED:
            self.recording, self.paused = False, False
            dur = int(self.last_status.get("duration_ms", 0))
            self.bus.publish(Topics.OBS_RECORD_STOPPED, path=path or self._last_path, duration_ms=dur, mono=now)
            self._last_path = None
        elif state == OUTPUT_PAUSED:
            self.paused = True
            self.bus.publish(Topics.OBS_RECORD_PAUSED, mono=now)
        elif state == OUTPUT_RESUMED:
            self.paused = False
            self.bus.publish(Topics.OBS_RECORD_RESUMED, mono=now)

    def on_exit_started(self, data) -> None:
        log.info("OBS se está cerrando")
        threading.Thread(target=self._disconnect, daemon=True).start()

    def on_current_program_scene_changed(self, data) -> None:
        self._publish_game_hint()

    # ---- pista del juego ------------------------------------------------
    def _publish_game_hint(self) -> None:
        name = self.guess_game_name()
        if name:
            self.bus.publish(Topics.OBS_GAME_HINT, name=name)

    def guess_game_name(self) -> str:
        req = self._req
        if req is None:
            return ""
        try:
            inputs = req.get_input_list().inputs
            for inp in inputs:
                kind = inp.get("inputKind", "")
                if kind in ("game_capture", "window_capture"):
                    st = req.get_input_settings(inp["inputName"]).input_settings
                    win = st.get("window", "")
                    m = _WINDOW_RE.match(win)
                    if m and m.group("title"):
                        return _clean_game_title(m.group("title"))
        except Exception as e:
            log.debug("Sin pista de juego: %s", e)
        try:
            scene = req.get_current_program_scene().scene_name
            if scene and scene.lower() not in ("escena", "scene", "escena 1", "scene 1"):
                return scene
        except Exception:
            pass
        return ""

    # ---- control manual (respaldo) ---------------------------------------
    def start_record(self) -> bool:
        return self._call("start_record")

    def stop_record(self) -> bool:
        return self._call("stop_record")

    def _call(self, name: str) -> bool:
        req = self._req
        if req is None:
            return False
        try:
            getattr(req, name)()
            return True
        except Exception as e:
            log.warning("OBS %s falló: %s", name, e)
            return False

    def status_text(self) -> str:
        if self.connected:
            return "Conectado" + (f" ({self.version})" if self.version else "")
        return self.last_error or "Desconectado"

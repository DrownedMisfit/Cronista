"""IA local: llama-server (llama.cpp, build Vulkan) como proceso hijo, arrancado solo cuando hace falta.

El servidor escucha solo en 127.0.0.1. Se lanza en la primera llamada, baja de prioridad y se liga a un
Job Object de Windows para que muera si Cronista se cierra o se cae. Tras `idle_unload_min` minutos sin uso
se cierra para liberar la memoria de vídeo y de RAM.

Contrato común con los proveedores por API: `ChatLLM` (ver `cronista/ai/providers.py`).
"""
from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import psutil
import requests

from cronista.ai.providers import strip_think
from cronista.core.paths import llm_models_dir, llm_runtime_dir, logs_dir
from cronista.core.settings import LLMSettings

log = logging.getLogger(__name__)

RUNTIME_EXE = "llama-server.exe"
# Argumentos extra para llama-server (vacío por defecto; útil para depurar).
EXTRA_ARGS: list[str] = []

HEALTH_TIMEOUT_S = 120.0
HEALTH_INTERVAL_S = 0.5
STOP_TIMEOUT_S = 5.0
LOG_TAIL_LINES = 15
LOG_NAME = "llama-server.log"

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001

# Sesión sin proxies del sistema: el servidor es siempre local.
_LOCAL_HTTP = requests.Session()
_LOCAL_HTTP.trust_env = False


class LLMUnavailable(RuntimeError):
    """La IA no está disponible (falta el runtime o el modelo, error de red, servidor caído...)."""


class ChatLLM(Protocol):
    name: str
    online: bool

    def chat(self, messages: list[dict], *, json_schema: dict | None = None, max_tokens: int = 512,
             temperature: float = 0.2, timeout: float = 180.0) -> str: ...


# ---- localizar binario y modelo ------------------------------------------------
def find_runtime(llm_settings: LLMSettings) -> Path | None:
    """llama-server.exe: la ruta configurada si existe; si no, tools/llama/llama-server.exe."""
    if llm_settings.runtime_path:
        p = Path(llm_settings.runtime_path)
        if p.is_file():
            return p
    p = llm_runtime_dir() / RUNTIME_EXE
    return p if p.is_file() else None


def find_model(llm_settings: LLMSettings) -> Path | None:
    """Modelo GGUF: la ruta configurada si existe; si no, el primer *.gguf (alfabético) que no sea mmproj."""
    if llm_settings.model_path:
        p = Path(llm_settings.model_path)
        if p.is_file():
            return p
    for p in sorted(llm_models_dir().glob("*.gguf"), key=lambda x: x.name.lower()):
        if p.is_file() and "mmproj" not in p.name.lower():
            return p
    return None


def free_port() -> int:
    """Puerto libre en 127.0.0.1 (libre en el momento de la consulta)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _default_threads() -> int:
    cores = psutil.cpu_count(logical=False) or os.cpu_count() or 4
    return max(2, cores // 2)


# ---- Windows: Job Object y prioridad -------------------------------------------
_k32: Any = None


def _kernel32() -> Any:
    """kernel32 con tipos de 64 bits para los handles (solo Windows)."""
    global _k32
    if _k32 is None:
        import ctypes
        from ctypes import wintypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k.SetInformationJobObject.restype = wintypes.BOOL
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k.AssignProcessToJobObject.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL
        _k32 = k
    return _k32


def _assign_to_job(pid: int) -> int:
    """Liga el proceso a un Job Object con KILL_ON_JOB_CLOSE. Devuelve el handle del job (no cerrarlo mientras viva)."""
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BASIC_LIMIT(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC_LIMIT),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    k = _kernel32()
    job = k.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = _JOB_LIMIT_KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                         ctypes.addressof(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        h = k.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not h:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not k.AssignProcessToJobObject(job, h):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            k.CloseHandle(h)
    except BaseException:
        k.CloseHandle(job)
        raise
    return int(job)


def _close_job(job: int) -> None:
    try:
        _kernel32().CloseHandle(job)
    except Exception:  # pragma: no cover - solo informativo
        log.debug("No se pudo cerrar el handle del Job Object", exc_info=True)


def _lower_priority(pid: int) -> None:
    psutil.Process(pid).nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)


def _log_tail(path: Path, offset: int, n: int = LOG_TAIL_LINES) -> str:
    """Últimas n líneas del log escritas desde `offset` (solo la sesión de arranque actual)."""
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return "(sin log)"
    lines = text.splitlines()[-n:]
    return "\n".join(lines) or "(log vacío)"


# ---- proceso llama-server -------------------------------------------------------
class LlamaServer:
    """Gestiona un proceso llama-server. No es seguro entre hilos: lo protege quien lo usa (LocalLLM)."""

    def __init__(self, llm_settings: LLMSettings) -> None:
        self.cfg = llm_settings
        self._proc: subprocess.Popen | None = None
        self._job: int | None = None
        self._port = 0
        self._device = ""

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def device(self) -> str:
        """Dispositivo con el que se arrancó el servidor actual ("gpu" | "cpu" | "")."""
        return self._device

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._port}" if self._port else ""

    def start(self, device: str) -> None:
        """Arranca el servidor en `device` ("gpu" o "cpu") y espera a que esté sano. Lanza LLMUnavailable."""
        if device not in ("gpu", "cpu"):
            raise ValueError(f"Dispositivo no válido: {device!r}")
        if self.running:
            if device == self._device:
                return
            self.stop()

        exe = find_runtime(self.cfg)
        if exe is None:
            raise LLMUnavailable(f"No encuentro {RUNTIME_EXE} (esperado en {llm_runtime_dir()}). "
                                 "Ejecuta tools\\download_llm.py.")
        model = find_model(self.cfg)
        if model is None:
            raise LLMUnavailable(f"No hay ningún modelo .gguf en {llm_models_dir()}. "
                                 "Ejecuta tools\\download_llm.py.")

        port = self.cfg.port or free_port()
        threads = self.cfg.threads or _default_threads()
        ngl = self.cfg.gpu_layers if device == "gpu" else 0
        cmd = [str(exe), "-m", str(model), "--host", "127.0.0.1", "--port", str(port),
               "-c", str(self.cfg.context_size), "-ngl", str(ngl), "-t", str(threads),
               "-np", "1", "--jinja", *EXTRA_ARGS]

        log_path = logs_dir() / LOG_NAME
        if log_path.exists() and log_path.stat().st_size > 5_000_000:   # rotación simple: un archivo previo
            try:
                log_path.replace(log_path.with_suffix(".log.1"))
            except OSError:
                pass
        offset = log_path.stat().st_size if log_path.exists() else 0
        log.info("Arrancando IA local (%s, %s, puerto %d)", device, model.name, port)
        with open(log_path, "a", encoding="utf-8") as logf:
            logf.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} arranque ({device}, puerto {port}) ===\n")
            logf.flush()
            try:
                proc = subprocess.Popen(cmd, cwd=str(exe.parent), stdin=subprocess.DEVNULL,
                                        stdout=logf, stderr=subprocess.STDOUT,
                                        creationflags=_CREATE_NO_WINDOW)
            except OSError as e:
                raise LLMUnavailable(f"No se pudo lanzar llama-server: {e}") from e

        self._proc = proc
        self._port = port
        self._device = device

        if sys.platform == "win32":
            try:
                self._job = _assign_to_job(proc.pid)
            except Exception as e:
                log.warning("Sin Job Object: si Cronista se cae, llama-server podría quedar vivo (%s)", e)
            try:
                _lower_priority(proc.pid)
            except Exception as e:
                log.warning("No se pudo bajar la prioridad de llama-server: %s", e)

        if not self._wait_healthy(proc, port):
            reason = "el proceso terminó" if proc.poll() is not None else "se agotó el tiempo de espera"
            tail = _log_tail(log_path, offset)
            self.stop()
            raise LLMUnavailable(f"El servidor de IA local no arrancó ({reason}). Últimas líneas del log:\n{tail}")
        log.info("IA local lista en %s", self.base_url)

    def _wait_healthy(self, proc: subprocess.Popen, port: int) -> bool:
        url = f"http://127.0.0.1:{port}/health"
        deadline = time.monotonic() + HEALTH_TIMEOUT_S
        while True:
            if proc.poll() is not None:
                return False
            try:
                if _LOCAL_HTTP.get(url, timeout=2).status_code == 200:
                    return True
            except requests.RequestException:
                pass
            if time.monotonic() >= deadline:
                return False
            time.sleep(HEALTH_INTERVAL_S)

    def stop(self) -> None:
        """Termina el proceso (terminate, 5 s, kill). Idempotente."""
        proc, self._proc = self._proc, None
        job, self._job = self._job, None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=STOP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                log.warning("llama-server no terminó en %.0f s; se fuerza", STOP_TIMEOUT_S)
                proc.kill()
                try:
                    proc.wait(timeout=STOP_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    log.error("llama-server no responde incluso tras kill()")
            except OSError as e:
                log.warning("Error al detener llama-server: %s", e)
            log.info("IA local detenida")
        if job:
            _close_job(job)
        self._device = ""


# ---- cliente de chat --------------------------------------------------------------
class LocalLLM:
    """IA local (implementa ChatLLM). Con `base_url` usa un servidor externo y no gestiona procesos."""

    name = "local"
    online = False

    def __init__(self, llm_settings: LLMSettings, base_url: str | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.cfg = llm_settings
        self.device = llm_settings.device if llm_settings.device in ("gpu", "cpu") else "gpu"
        self._external = base_url is not None
        self._base = base_url.rstrip("/") if base_url else ""
        self._clock = clock
        self._server = LlamaServer(llm_settings)
        self._lock = threading.Lock()
        self._busy = 0
        self.last_used = clock()
        self.state = "idle" if self._external else "off"

    def available(self) -> tuple[bool, str]:
        """(disponible, motivo en español si no lo está)."""
        if self._external:
            return True, ""
        if find_runtime(self.cfg) is None:
            return False, (f"Falta {RUNTIME_EXE} en {llm_runtime_dir()}. "
                           "Descárgalo con tools\\download_llm.py o desde Configuración → IA local.")
        if find_model(self.cfg) is None:
            return False, (f"Falta el modelo .gguf en {llm_models_dir()}. "
                           "Descárgalo con tools\\download_llm.py o desde Configuración → IA local.")
        return True, ""

    def set_device(self, device: str) -> None:
        """Cambia gpu/cpu. Si el servidor está corriendo, se reiniciará en la próxima llamada."""
        if device not in ("gpu", "cpu"):
            raise ValueError(f"Dispositivo no válido: {device!r}")
        self.device = device

    def chat(self, messages: list[dict], *, json_schema: dict | None = None, max_tokens: int = 512,
             temperature: float = 0.2, timeout: float = 180.0) -> str:
        base = self._acquire()
        try:
            body: dict[str, Any] = {
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "top_p": 0.8,
                "top_k": 20,
                "stream": False,
                "chat_template_kwargs": {"enable_thinking": False},
            }
            if json_schema:
                body["response_format"] = {"type": "json_schema",
                                           "json_schema": {"name": "result", "schema": json_schema}}
            try:
                r = _LOCAL_HTTP.post(f"{base}/v1/chat/completions", json=body, timeout=timeout)
            except requests.RequestException as e:
                raise LLMUnavailable(f"No se pudo conectar con la IA local ({type(e).__name__}).") from e
            if r.status_code != 200:
                raise LLMUnavailable(f"La IA local respondió con error HTTP {r.status_code}: {r.text[:200]}")
            try:
                content = r.json()["choices"][0]["message"]["content"]
            except (ValueError, KeyError, IndexError, TypeError) as e:
                raise LLMUnavailable("La IA local devolvió una respuesta que no se puede interpretar.") from e
            return strip_think(content or "")
        finally:
            self._release()

    def idle_check(self) -> bool:
        """Cierra el servidor si lleva más de `idle_unload_min` minutos sin uso. Devuelve True si lo cerró."""
        if self._external or self.cfg.idle_unload_min <= 0:
            return False
        with self._lock:
            if self._busy or not self._server.running:
                return False
            if self._clock() - self.last_used < self.cfg.idle_unload_min * 60:
                return False
        self.unload()
        return True

    def unload(self) -> None:
        """Cierra el servidor. No interrumpe una llamada en curso (en ese caso no hace nada)."""
        if self._external:
            return
        with self._lock:
            if self._busy:
                return
            self._server.stop()
            self.state = "off"

    # ---- internos -----------------------------------------------------------------
    def _acquire(self) -> str:
        """Asegura el servidor y registra una llamada en curso. Devuelve la URL base."""
        with self._lock:
            if self._external:
                self._busy += 1
                self.state = "busy"
                return self._base
            srv = self._server
            if srv.running and srv.device != self.device and self._busy == 0:
                log.info("Cambio de dispositivo de IA local (%s → %s): reiniciando", srv.device, self.device)
                srv.stop()
            if not srv.running:
                self.state = "loading"
                try:
                    srv.start(self.device)
                except LLMUnavailable:
                    self.state = "off"
                    raise
                except Exception as e:
                    self.state = "off"
                    raise LLMUnavailable(f"No se pudo arrancar la IA local: {e}") from e
            self._busy += 1
            self.state = "busy"
            return srv.base_url

    def _release(self) -> None:
        with self._lock:
            self._busy = max(0, self._busy - 1)
            self.last_used = self._clock()
            if self._busy:
                self.state = "busy"
            else:
                self.state = "idle" if (self._external or self._server.running) else "off"


def build_chat_llm(settings, which: str = "local") -> ChatLLM | None:
    """'local' → LocalLLM si está habilitada; 'api' → proveedor por API configurado (o None)."""
    if which == "local":
        return LocalLLM(settings.llm) if settings.llm.enabled else None
    if which == "api":
        from cronista.ai.providers import build_provider  # import diferido
        return build_provider(settings.ai)
    raise ValueError(f"Origen de IA desconocido: {which!r}")

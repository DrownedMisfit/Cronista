"""Monitor de rendimiento: detecta si hay un juego a pantalla completa / sin bordes en
primer plano y, si "Priorizar rendimiento del juego" está activo, baja la prioridad
de nuestro proceso (y avisa al motor de transcripción para que entre en modo eco).

Coste: una consulta a la API de Windows cada pocos segundos (microsegundos de CPU).
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import os
import threading

import psutil

from cronista.core.events import EventBus, Topics
from cronista.core.settings import Settings

log = logging.getLogger(__name__)

_IGNORED_EXE = {"explorer.exe", "obs64.exe", "obs32.exe", "python.exe", "pythonw.exe", "cronista.exe",
                "dwm.exe", "searchhost.exe", "startmenuexperiencehost.exe", "shellexperiencehost.exe",
                "lockapp.exe", "applicationframehost.exe", "msedge.exe", "chrome.exe", "firefox.exe",
                "discord.exe", "code.exe", "devenv.exe", "parsecd.exe", "steamwebhelper.exe"}


def foreground_window_info() -> tuple[str, str, bool]:
    """(título, exe, pantalla completa) de la ventana en primer plano."""
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return "", "", False
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    exe = ""
    try:
        exe = psutil.Process(pid.value).name().lower()
    except Exception:
        pass
    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    # monitor donde está la ventana
    MONITOR_DEFAULTTONEAREST = 2
    hmon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]

    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    full = False
    if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
        m = mi.rcMonitor
        full = (rect.left <= m.left and rect.top <= m.top and rect.right >= m.right and rect.bottom >= m.bottom)
    return buf.value, exe, full


class PerfMonitor:
    def __init__(self, bus: EventBus, settings: Settings, interval_s: float = 3.0) -> None:
        self.bus = bus
        self.settings = settings
        self.interval = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.game_detected = False
        self.game_title = ""
        self._proc = psutil.Process(os.getpid())
        self._normal_nice = self._proc.nice()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="perf-monitor", daemon=True)
        self._thread.start()
        if self.settings.performance.low_priority_always:
            self._set_priority(low=True)

    def stop(self) -> None:
        self._stop.set()
        self._set_priority(low=False)

    def _set_priority(self, low: bool) -> None:
        try:
            if low:
                self._proc.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
            elif not self.settings.performance.low_priority_always:
                self._proc.nice(psutil.NORMAL_PRIORITY_CLASS)
        except Exception as e:
            log.debug("No se pudo cambiar la prioridad: %s", e)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                title, exe, full = foreground_window_info()
            except Exception:
                continue
            detected = bool(full and exe and exe not in _IGNORED_EXE)
            if detected != self.game_detected or (detected and title != self.game_title):
                self.game_detected, self.game_title = detected, title if detected else ""
                if self.settings.performance.prioritize_game:
                    self._set_priority(low=detected)
                log.info("Juego en primer plano: %s (%s)", title if detected else "ninguno", exe)
                self.bus.publish(Topics.PERF_GAME_DETECTED, detected=detected, title=self.game_title, exe=exe)

    def usage(self) -> dict:
        try:
            with self._proc.oneshot():
                return {"cpu": self._proc.cpu_percent(interval=None), "rss_mb": self._proc.memory_info().rss / 2**20,
                        "threads": self._proc.num_threads()}
        except Exception:
            return {}

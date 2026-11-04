"""Hotkey global (funciona con juegos en pantalla completa, sin robar el foco).

Usa un hook de teclado de bajo nivel (librería `keyboard`). Mantener pulsada la
tecla = captura forzada; soltar = fin. Opcionalmente modo "alternar" (pulsar para
empezar, pulsar para terminar). Publica HOTKEY_DOWN / HOTKEY_UP en el bus.
"""
from __future__ import annotations

import logging
import threading
import time

from cronista.core.events import EventBus, Topics
from cronista.core.settings import Settings

log = logging.getLogger(__name__)


class HotkeyManager:
    def __init__(self, bus: EventBus, settings: Settings) -> None:
        self.bus = bus
        self.settings = settings
        self._handles: list = []
        self._down = False
        self._lock = threading.Lock()
        self._last_down = 0.0
        self.active = False
        self.error = ""

    def start(self) -> None:
        self.stop()
        key = (self.settings.hotkey.note_key or "f8").strip().lower()
        try:
            import keyboard
            self._handles.append(keyboard.on_press_key(key, self._on_press, suppress=False))
            self._handles.append(keyboard.on_release_key(key, self._on_release, suppress=False))
            self.active = True
            self.error = ""
            log.info("Hotkey global registrado: %s", key.upper())
        except Exception as e:
            self.active = False
            self.error = str(e)
            log.exception("No se pudo registrar el hotkey")
            self.bus.publish(Topics.STATUS, level="error", message=f"Hotkey no disponible: {e}")

    def stop(self) -> None:
        if not self._handles:
            return
        try:
            import keyboard
            for h in self._handles:
                try:
                    keyboard.unhook(h)
                except Exception:
                    pass
        finally:
            self._handles.clear()
            self.active = False
            if self._down:
                self._down = False
                self.bus.publish(Topics.HOTKEY_UP)

    def rebind(self, key: str) -> None:
        self.settings.hotkey.note_key = key
        self.start()

    # ---- callbacks (hilo del hook) --------------------------------------------
    def _on_press(self, ev) -> None:
        now = time.monotonic()
        with self._lock:
            if self.settings.hotkey.toggle_mode:
                if now - self._last_down < 0.35:   # autorepetición del teclado
                    return
                self._last_down = now
                self._down = not self._down
                self.bus.publish(Topics.HOTKEY_DOWN if self._down else Topics.HOTKEY_UP)
                return
            self._last_down = now
            if self._down:
                return   # autorepetición
            self._down = True
        self.bus.publish(Topics.HOTKEY_DOWN)

    def _on_release(self, ev) -> None:
        with self._lock:
            if self.settings.hotkey.toggle_mode or not self._down:
                return
            self._down = False
        self.bus.publish(Topics.HOTKEY_UP)

    @staticmethod
    def read_key(timeout: float = 6.0) -> str:
        """Espera a que el usuario pulse una tecla/combinación y devuelve su nombre."""
        import keyboard
        result: list[str] = []
        done = threading.Event()

        def worker() -> None:
            try:
                result.append(keyboard.read_hotkey(suppress=False))
            except Exception:
                pass
            done.set()

        threading.Thread(target=worker, daemon=True).start()
        done.wait(timeout)
        return result[0] if result else ""

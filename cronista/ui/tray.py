"""Icono de bandeja con estados: inactivo, escuchando, procesando, nota guardada, problema."""
from __future__ import annotations

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap, QPen
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from cronista.ui.theme import COLORS

STATE_COLORS = {"idle": COLORS["off"], "listening": COLORS["ok"], "processing": COLORS["busy"],
                "saved": "#a371f7", "warn": COLORS["warn"], "error": COLORS["err"], "recording": COLORS["rec"]}
STATE_TEXT = {"idle": "Inactivo", "listening": "Escuchando", "processing": "Procesando", "saved": "Nota guardada",
              "warn": "Atención", "error": "Problema", "recording": "Grabando"}


def make_icon(color: str, ring: str | None = None, size: int = 64) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#1b1d22"))
    p.drawEllipse(2, 2, size - 4, size - 4)
    p.setBrush(QColor(color))
    m = size // 5
    p.drawEllipse(m, m, size - 2 * m, size - 2 * m)
    if ring:
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(ring), max(2, size // 12)))
        p.drawEllipse(4, 4, size - 8, size - 8)
    p.end()
    return QIcon(pm)


class TrayIcon(QSystemTrayIcon):
    def __init__(self, window, parent=None) -> None:
        super().__init__(parent)
        self.window = window
        self.state = "idle"
        self.recording = False
        self._icons: dict[tuple[str, bool], QIcon] = {}
        self._revert = QTimer(self)
        self._revert.setSingleShot(True)
        self._revert.timeout.connect(lambda: self.set_state(self._base_state))
        self._base_state = "idle"
        menu = QMenu()
        self.act_open = QAction("Abrir Cronista", menu)
        self.act_session = QAction("Iniciar sesión manual", menu)
        self.act_listen = QAction("Micrófono activado", menu)
        self.act_listen.setCheckable(True)
        self.act_mic = QMenu("Cambiar micrófono", menu)
        self.act_settings = QAction("Configuración…", menu)
        self.act_quit = QAction("Cerrar Cronista", menu)
        menu.addAction(self.act_open)
        menu.addSeparator()
        menu.addAction(self.act_session)
        menu.addAction(self.act_listen)
        menu.addMenu(self.act_mic)
        menu.addSeparator()
        menu.addAction(self.act_settings)
        menu.addSeparator()
        menu.addAction(self.act_quit)
        self.setContextMenu(menu)
        self.activated.connect(self._activated)
        self.set_state("idle")

    def _activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.window.show_from_tray()

    def set_state(self, state: str, transient_ms: int = 0) -> None:
        if state not in STATE_COLORS:
            state = "idle"
        if transient_ms:
            self._revert.start(transient_ms)
        else:
            self._base_state = state
            self._revert.stop()
        self.state = state
        key = (state, self.recording)
        if key not in self._icons:
            self._icons[key] = make_icon(STATE_COLORS[state], COLORS["rec"] if self.recording else None)
        self.setIcon(self._icons[key])
        rec = " · Grabando" if self.recording else ""
        self.setToolTip(f"Cronista — {STATE_TEXT[state]}{rec}")

    def set_recording(self, recording: bool) -> None:
        if recording != self.recording:
            self.recording = recording
            self.set_state(self.state if self._revert.isActive() else self._base_state)

    def set_session_active(self, active: bool) -> None:
        self.act_session.setText("Detener sesión" if active else "Iniciar sesión manual")

    def notify(self, title: str, text: str, ms: int = 3000) -> None:
        if self.supportsMessages():
            self.showMessage(title, text, QSystemTrayIcon.Information, ms)

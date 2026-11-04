"""Arranque del teleprompter: crea la aplicación Qt, aplica el tema y muestra la ventana principal."""
from __future__ import annotations

import logging
import sys

from PySide6.QtWidgets import QApplication

from cronista import APP_NAME, __version__
from cronista.core.settings import Settings
from cronista.teleprompter.window import TeleprompterWindow
from cronista.ui.theme import apply_theme

log = logging.getLogger(__name__)


def run_teleprompter(settings: Settings) -> int:
    """Abre el teleprompter y devuelve el código de salida de Qt al cerrarlo."""
    qapp = QApplication.instance() or QApplication(sys.argv)
    qapp.setApplicationName(APP_NAME)
    qapp.setQuitOnLastWindowClosed(True)
    apply_theme(qapp, settings.ui.theme)
    log.info("Arrancando el teleprompter de %s %s", APP_NAME, __version__)
    window = TeleprompterWindow(settings)
    window.show()
    return qapp.exec()

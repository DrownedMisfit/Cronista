"""Arranque del teleprompter: crea la aplicación Qt, aplica el tema y muestra la ventana principal."""
from __future__ import annotations

import logging
import sys

from PySide6.QtWidgets import QApplication

from cronista import APP_NAME, __version__
from cronista.core.settings import Settings
from cronista.teleprompter import arranque
from cronista.ui.theme import apply_theme

log = logging.getLogger(__name__)


def run_teleprompter(settings: Settings) -> int:
    """Abre el teleprompter y devuelve el código de salida de Qt al cerrarlo."""
    arranque.set_windows_app_id()                # antes de crear la aplicación Qt (si no, sale el icono de Python)
    qapp = QApplication.instance() or QApplication(sys.argv)
    qapp.setApplicationName(APP_NAME)
    qapp.setQuitOnLastWindowClosed(True)
    apply_theme(qapp, settings.ui.theme)
    arranque.set_app_identity(qapp)
    log.info("Arrancando el teleprompter de %s %s", APP_NAME, __version__)
    # La ventana de arranque sale en cuanto hay Qt (antes de montar la principal); al terminar de montarla pasa la onda
    # de luz una vez y se funde sobre ella. Un clic o una tecla la saltan.
    inicio = arranque.mostrar_arranque(qapp) if settings.teleprompter.intro_animation else None
    from cronista.teleprompter.window import TeleprompterWindow
    window = TeleprompterWindow(settings)

    def abrir() -> None:
        window.show()
        window.raise_()
        window.activateWindow()

    if inicio is not None:
        inicio.play(abrir)
    else:
        abrir()
    return qapp.exec()

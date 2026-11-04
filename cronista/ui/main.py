"""Arranque de la interfaz Qt."""
from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)


def run_gui(settings, minimized: bool = False) -> int:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QMessageBox

    from cronista import APP_NAME
    from cronista.app import App
    from cronista.ui.main_window import MainWindow
    from cronista.ui.theme import apply_theme

    qapp = QApplication.instance() or QApplication(sys.argv)
    qapp.setApplicationName(APP_NAME)
    qapp.setQuitOnLastWindowClosed(False)
    apply_theme(qapp, settings.ui.theme)

    try:
        app = App(settings)
    except Exception as e:
        log.exception("No se pudo iniciar la aplicación")
        QMessageBox.critical(None, APP_NAME, f"No se pudo iniciar:\n{e}")
        return 1

    win = MainWindow(app, qapp)
    app.start()
    if minimized:
        win.hide()
        win.tray.notify(APP_NAME, "Cronista está en segundo plano. Doble clic en el icono para abrir.")
    else:
        win.show()
    code = qapp.exec()
    return code

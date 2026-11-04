"""Tema visual (oscuro por defecto) para toda la aplicación."""
from __future__ import annotations

from PySide6.QtGui import QPalette, QColor
from PySide6.QtWidgets import QApplication

DARK_QSS = """
QWidget { font-family: 'Segoe UI', sans-serif; font-size: 10pt; }
QMainWindow, QDialog { background: #1b1d22; }
QDockWidget::title { background: #23262d; padding: 6px; font-weight: 600; }
QFrame#statusStrip { background: #23262d; border-radius: 8px; }
QLabel#statusKey { color: #8b93a3; font-size: 8.5pt; letter-spacing: 1px; }
QLabel#statusVal { color: #e8eaf0; font-weight: 600; }
QLabel#liveText { color: #e8eaf0; font-size: 13pt; padding: 10px; background: #23262d; border-radius: 8px; }
QLabel#liveHint { color: #8b93a3; font-style: italic; }
QLabel#heading { color: #8b93a3; font-size: 8.5pt; letter-spacing: 1px; font-weight: 600; margin-top: 6px; }
QLabel#ctxBig { color: #e8eaf0; font-size: 11pt; }
QListWidget, QTextEdit, QPlainTextEdit, QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background: #15171b; color: #e8eaf0; border: 1px solid #2f333c; border-radius: 6px; padding: 4px; }
QListWidget::item { padding: 6px; border-bottom: 1px solid #22252c; }
QListWidget::item:selected { background: #2d3a55; }
QPushButton { background: #2b2f38; color: #e8eaf0; border: 1px solid #3a3f4b; border-radius: 6px; padding: 5px 10px; }
QPushButton:hover { background: #363b47; }
QPushButton:checked { background: #3b5bdb; border-color: #3b5bdb; }
QPushButton#primary { background: #3b5bdb; border-color: #3b5bdb; }
QPushButton#danger { background: #7a2d2d; border-color: #7a2d2d; }
QMenuBar { background: #1b1d22; color: #e8eaf0; }
QMenuBar::item:selected { background: #2d3a55; }
QMenu { background: #23262d; color: #e8eaf0; border: 1px solid #3a3f4b; }
QMenu::item:selected { background: #2d3a55; }
QStatusBar { color: #8b93a3; }
QTabWidget::pane { border: 1px solid #2f333c; }
QTabBar::tab { background: #23262d; color: #8b93a3; padding: 6px 12px; }
QTabBar::tab:selected { color: #e8eaf0; background: #2b2f38; }
QGroupBox { border: 1px solid #2f333c; border-radius: 6px; margin-top: 10px; padding-top: 8px; color: #8b93a3; }
QGroupBox::title { subcontrol-origin: margin; left: 8px; }
QCheckBox, QRadioButton { color: #e8eaf0; }
QToolTip { background: #23262d; color: #e8eaf0; border: 1px solid #3a3f4b; }
QScrollBar:vertical { background: #1b1d22; width: 10px; } QScrollBar::handle:vertical { background: #3a3f4b; border-radius: 4px; }
"""

COLORS = {"ok": "#3fb950", "warn": "#d29922", "err": "#f85149", "off": "#6e7681", "busy": "#58a6ff", "rec": "#f85149"}


def apply_theme(app: QApplication, theme: str = "dark") -> None:
    if theme == "dark":
        app.setStyle("Fusion")
        pal = QPalette()
        pal.setColor(QPalette.Window, QColor("#1b1d22"))
        pal.setColor(QPalette.WindowText, QColor("#e8eaf0"))
        pal.setColor(QPalette.Base, QColor("#15171b"))
        pal.setColor(QPalette.Text, QColor("#e8eaf0"))
        pal.setColor(QPalette.Button, QColor("#2b2f38"))
        pal.setColor(QPalette.ButtonText, QColor("#e8eaf0"))
        pal.setColor(QPalette.Highlight, QColor("#3b5bdb"))
        pal.setColor(QPalette.HighlightedText, QColor("#ffffff"))
        app.setPalette(pal)
        app.setStyleSheet(DARK_QSS)
    else:
        app.setStyle("Fusion")
        app.setStyleSheet("")

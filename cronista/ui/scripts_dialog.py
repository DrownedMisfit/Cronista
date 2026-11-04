"""Diálogo de guiones: guiones guardados (ver, copiar, exportar, eliminar) y creación de uno nuevo
a partir de las notas de las sesiones de un juego. La generación corre en JobRunner; aquí solo
se muestra el progreso escuchando Topics.JOB_PROGRESS / JOB_DONE del bus."""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
                               QProgressBar, QPushButton, QSplitter, QTabWidget, QTextBrowser, QVBoxLayout, QWidget)

from cronista import APP_NAME
from cronista.ai.providers import build_provider
from cronista.ai.scripts import APPROACHES, LENGTHS
from cronista.core.events import Topics
from cronista.core.paths import exports_dir
from cronista.ui.bridge import QtBridge
from cronista.ui.theme import COLORS

log = logging.getLogger(__name__)

NOTA_GUION = ("El guion se construye solo con tus notas; cada punto cita los momentos de la grabación en que lo "
              "dijiste. Las preguntas abiertas señalan lo que te falta.")
AVISO_API = "Aviso: el texto de tus notas se enviará a internet (proveedor de API configurado)."
MOTORES = {"local": "IA local", "api": "API (online)"}


def _fmt_fecha(iso: str | None) -> str:
    try:
        return datetime.fromisoformat(iso or "").strftime("%d/%m/%Y %H:%M")
    except (TypeError, ValueError):
        return iso or "—"


class ScriptsDialog(QDialog):
    def __init__(self, parent, app, game: str | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self.game = game or None
        self._busy = False                 # True mientras esta ventana ha pedido un guion nuevo
        self._current: dict | None = None  # guion que se muestra en "Ver guion"
        self.setWindowTitle("Guiones")
        self.resize(1000, 700)

        root = QVBoxLayout(self)
        split = QSplitter(Qt.Horizontal)

        # izquierda: guiones guardados
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        title = "Guiones guardados" + (f" · {self.game}" if self.game else "")
        ll.addWidget(QLabel(title))
        self.list = QListWidget()
        self.list.currentItemChanged.connect(self._on_current_changed)
        self.list.itemClicked.connect(lambda _item: self.tabs.setCurrentIndex(0))
        ll.addWidget(self.list, 1)
        split.addWidget(left)

        # derecha: ver guion / nuevo guion
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_view(), "Ver guion")
        self.tabs.addTab(self._build_new(), "Nuevo guion")
        split.addWidget(self.tabs)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 3)
        split.setSizes([320, 680])
        root.addWidget(split, 1)

        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

        self._refresh_games()
        self._refresh_list()
        if self.list.count() == 0:
            self.tabs.setCurrentIndex(1)

        self._bridge = QtBridge(app.bus, self, topics=(Topics.JOB_PROGRESS, Topics.JOB_DONE))
        self._bridge.event.connect(self._on_event)

    # ---- panel "Ver guion" ----------------------------------------------------------
    def _build_view(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(False)
        lay.addWidget(self.view, 1)
        self.view_status = QLabel("")
        self.view_status.setWordWrap(True)
        lay.addWidget(self.view_status)
        row = QHBoxLayout()
        self.b_copy = QPushButton("Copiar")
        self.b_copy.clicked.connect(self._copy)
        self.b_export = QPushButton("Guardar como documento…")
        self.b_export.clicked.connect(self._export)
        self.b_delete = QPushButton("Eliminar")
        self.b_delete.clicked.connect(self._delete)
        row.addWidget(self.b_copy)
        row.addWidget(self.b_export)
        row.addStretch(1)
        row.addWidget(self.b_delete)
        lay.addLayout(row)
        return w

    # ---- panel "Nuevo guion" --------------------------------------------------------
    def _build_new(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        nota = QLabel(NOTA_GUION)
        nota.setWordWrap(True)
        nota.setObjectName("liveHint")
        lay.addWidget(nota)

        form = QFormLayout()
        self.new_game = QComboBox()
        self.new_game.currentIndexChanged.connect(lambda _i: self._refresh_sessions())
        form.addRow("Juego", self.new_game)

        self.sessions = QListWidget()
        self.sessions.setMinimumHeight(110)
        form.addRow("Sesiones", self.sessions)

        self.length = QComboBox()
        for key, (label, _n) in LENGTHS.items():
            self.length.addItem(f"{key[:1].upper()}{key[1:]} ({label})", key)
        self.length.setCurrentIndex(max(0, self.length.findData("medio")))
        form.addRow("Duración", self.length)

        self.approach = QComboBox()
        for key in APPROACHES:
            self.approach.addItem(f"{key[:1].upper()}{key[1:]}", key)
        self.approach_desc = QLabel("")
        self.approach_desc.setWordWrap(True)
        self.approach_desc.setObjectName("liveHint")
        self.approach.currentIndexChanged.connect(lambda _i: self._on_approach())
        form.addRow("Enfoque", self.approach)
        form.addRow("", self.approach_desc)

        self.only_marked = QCheckBox("Solo notas con marcador")
        form.addRow("", self.only_marked)

        self.extra = QLineEdit()
        self.extra.setPlaceholderText("Indicaciones adicionales (opcional): p. ej. 'céntrate en el capítulo 3'")
        form.addRow("Indicaciones", self.extra)

        self.engine = QComboBox()
        for key, label in MOTORES.items():
            self.engine.addItem(label, key)
        self.engine.currentIndexChanged.connect(lambda _i: self._on_engine())
        if build_provider(self.app.settings.ai) is None:
            self.engine.model().item(self.engine.findData("api")).setEnabled(False)
            self.engine.setToolTip("Configura un proveedor en Configuración → IA / Chatbot")
        form.addRow("Motor", self.engine)
        lay.addLayout(form)

        self.engine_warn = QLabel(AVISO_API)
        self.engine_warn.setWordWrap(True)
        self.engine_warn.setStyleSheet(f"color: {COLORS['warn']};")
        self.engine_warn.setVisible(False)
        lay.addWidget(self.engine_warn)

        row = QHBoxLayout()
        self.btn_generate = QPushButton("Generar guion")
        self.btn_generate.setObjectName("primary")
        self.btn_generate.clicked.connect(self._generate)
        row.addWidget(self.btn_generate)
        row.addStretch(1)
        lay.addLayout(row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        lay.addWidget(self.progress)
        self.gen_status = QLabel("")
        self.gen_status.setWordWrap(True)
        lay.addWidget(self.gen_status)
        lay.addStretch(1)

        self._on_approach()
        self._on_engine()
        return w

    def _refresh_games(self) -> None:
        games = self.app.store.list_games()
        pre = self.game or self.app.settings.general.default_game or ""
        if pre and pre not in games:
            games.insert(0, pre)
        self.new_game.blockSignals(True)
        self.new_game.clear()
        for g in games:
            self.new_game.addItem(g)
        if pre:
            self.new_game.setCurrentIndex(max(0, self.new_game.findText(pre)))
        self.new_game.blockSignals(False)
        self._refresh_sessions()
        self.btn_generate.setEnabled(bool(games) and not self._busy)

    def _refresh_sessions(self) -> None:
        self.sessions.clear()
        game = self.new_game.currentText()
        if not game:
            return
        for s in self.app.store.list_sessions(game=game):
            label = (f"{_fmt_fecha(s.get('started_at'))} · {s.get('title') or 'Sesión'} · "
                     f"{s.get('notes_count', 0)} notas")
            it = QListWidgetItem(label)
            it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked)
            it.setData(Qt.UserRole, s["id"])
            self.sessions.addItem(it)

    def _checked_session_ids(self) -> list[int]:
        out = []
        for i in range(self.sessions.count()):
            it = self.sessions.item(i)
            if it.checkState() == Qt.Checked:
                out.append(int(it.data(Qt.UserRole)))
        return out

    def _on_approach(self) -> None:
        self.approach_desc.setText(APPROACHES.get(self.approach.currentData(), ""))

    def _on_engine(self) -> None:
        self.engine_warn.setVisible(self.engine.currentData() == "api")

    # ---- lista de guiones -----------------------------------------------------------
    def _selected_id(self) -> int | None:
        it = self.list.currentItem()
        return int(it.data(Qt.UserRole)) if it is not None else None

    def _refresh_list(self, select_id: int | None = None) -> None:
        keep = select_id if select_id is not None else self._selected_id()
        self.list.clear()
        for s in self.app.store.list_scripts(self.game):   # más recientes primero
            text = f"{s.get('title') or 'Guion'}\n{s.get('game') or ''} · {_fmt_fecha(s.get('created_at'))}"
            it = QListWidgetItem(text)
            it.setData(Qt.UserRole, s["id"])
            self.list.addItem(it)
        row = 0 if self.list.count() else -1    # por defecto, el más reciente
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.UserRole) == keep:
                row = i
                break
        if row >= 0:
            self.list.setCurrentRow(row)
        self._show(self._selected_id())

    def _on_current_changed(self, cur, _prev) -> None:
        self._show(int(cur.data(Qt.UserRole)) if cur is not None else None)

    def _show(self, script_id: int | None) -> None:
        rec = self.app.store.get_script(script_id) if script_id else None
        self._current = rec
        has = rec is not None
        self.b_copy.setEnabled(has)
        self.b_export.setEnabled(has)
        self.b_delete.setEnabled(has)
        if rec is None:
            self.view.setPlainText("Aún no hay guiones. Crea uno en la pestaña «Nuevo guion»."
                                   if self.list.count() == 0 else "Selecciona un guion de la lista.")
            self.view_status.setText("")
            return
        self.view.setMarkdown(rec.get("markdown") or "")
        motor = MOTORES.get(rec.get("provider") or "", rec.get("provider") or "")
        self.view_status.setText(f"Creado el {_fmt_fecha(rec.get('created_at'))} · {motor} · "
                                 f"{len(rec.get('session_ids') or [])} sesiones")

    def _copy(self) -> None:
        if self._current:
            QGuiApplication.clipboard().setText(self._current.get("markdown") or "")
            self.view_status.setText("Copiado al portapapeles.")

    def _export(self) -> None:
        if not self._current:
            return
        base = Path(self.app.settings.general.export_dir) if self.app.settings.general.export_dir else exports_dir()
        base.mkdir(parents=True, exist_ok=True)
        from cronista.ui.doc_export import save_markdown_as
        title = self._current.get("title") or "Guion"
        out = save_markdown_as(self, self._current.get("markdown") or "", title, base, title=title)
        if out:
            self.view_status.setText(f"Guardado en {out}")

    def _delete(self) -> None:
        if not self._current:
            return
        titulo = self._current.get("title") or "Guion"
        if QMessageBox.question(self, APP_NAME, f"¿Eliminar el guion «{titulo}»?\nNo se puede deshacer.",
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self.app.store.delete_script(self._current["id"])
        self._refresh_list()

    # ---- generación -------------------------------------------------------------------
    def _generate(self) -> None:
        game = self.new_game.currentText()
        ids = self._checked_session_ids()
        if not game:
            QMessageBox.information(self, APP_NAME, "Elige un juego.")
            return
        if not ids:
            QMessageBox.information(self, APP_NAME, "Marca al menos una sesión.")
            return
        which = self.engine.currentData()
        if which == "local":
            ok, motivo = self.app.ai.availability()
            if not ok:
                QMessageBox.warning(self, APP_NAME, f"{motivo}\n\nPuedes configurarla en Configuración → IA local.")
                return
        else:
            if QMessageBox.question(self, APP_NAME, "Se enviará el texto de las notas seleccionadas al proveedor de "
                                    "API configurado (conexión a internet). ¿Continuar?",
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return
        self._busy = True
        self.btn_generate.setEnabled(False)
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.gen_status.setText("Preparando el guion…")
        self.app.generate_script(game, ids, length=self.length.currentData(), approach=self.approach.currentData(),
                                 extra=self.extra.text().strip(), only_marked=self.only_marked.isChecked(),
                                 which=which)

    def _on_event(self, ev) -> None:
        try:
            if ev.get("job") != "script":
                return
            if ev.topic == Topics.JOB_PROGRESS:
                if self._busy:
                    self.progress.setValue(int(max(0.0, min(1.0, float(ev.get("fraction") or 0))) * 100))
                    self.gen_status.setText(str(ev.get("text") or ""))
                return
            if ev.topic != Topics.JOB_DONE:
                return
            was_busy = self._busy
            self._busy = False
            self.progress.setVisible(False)
            self.btn_generate.setEnabled(self.new_game.count() > 0)
            if not was_busy:
                self._refresh_list()        # lo generó otra ventana: solo actualizar la lista
                return
            if ev.get("ok"):
                sid = (ev.get("result") or {}).get("script_id")
                self.gen_status.setText("Guion generado.")
                self._refresh_list(select_id=sid)
                self.tabs.setCurrentIndex(0)
            else:
                self.gen_status.setText("")
                QMessageBox.warning(self, APP_NAME, f"No se pudo generar el guion:\n{ev.get('message')}")
        except RuntimeError:               # el diálogo ya se cerró y se destruyó
            pass

    # ---- cierre -----------------------------------------------------------------------
    def done(self, r: int) -> None:
        self._bridge.close()
        super().done(r)

    def closeEvent(self, e) -> None:
        self._bridge.close()
        super().closeEvent(e)

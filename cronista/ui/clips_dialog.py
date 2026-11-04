"""Diálogo para exportar clips (recortes de la grabación de OBS) de las notas de una sesión.
El corte lo hace JobRunner (app.export_clips); aquí solo se eligen las opciones."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QGroupBox, QHBoxLayout, QLabel, QPushButton, QRadioButton, QVBoxLayout)

from cronista.clips.exporter import default_clip_dir
from cronista.ui.theme import COLORS


def _fmt_fecha(iso: str | None) -> str:
    try:
        return datetime.fromisoformat(iso or "").strftime("%d/%m/%Y %H:%M")
    except (TypeError, ValueError):
        return iso or "—"


class ClipsDialog(QDialog):
    def __init__(self, parent, app, session_id: int, note_ids: list[int] | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self.session_id = session_id
        self.note_ids = list(note_ids) if note_ids is not None else None
        self.session: dict = app.store.get_session(session_id) or {}
        self.notes: list[dict] = app.store.list_notes(session_id) if self.session else []
        ed = app.settings.editor
        self.setWindowTitle("Exportar clips")
        self.setMinimumWidth(600)

        lay = QVBoxLayout(self)
        head = QLabel(f"{self.session.get('game') or 'Sesión sin juego'} — "
                      f"{_fmt_fecha(self.session.get('started_at'))}")
        head.setStyleSheet("font-size: 13pt; font-weight: 600;")
        lay.addWidget(head)
        self.rec_label = QLabel("")
        self.rec_label.setWordWrap(True)
        lay.addWidget(self.rec_label)

        # qué exportar
        g_what = QGroupBox("Qué exportar")
        wl = QVBoxLayout(g_what)
        self.rb_all = QRadioButton(f"Todas las notas ({len(self.notes)})")
        marked = [n for n in self.notes if n.get("marker")]
        self.rb_marked = QRadioButton(f"Solo notas con marcador ({len(marked)})")
        self.rb_marked.setEnabled(bool(marked))
        self.rb_all.setChecked(True)
        group = QButtonGroup(self)
        group.addButton(self.rb_all)
        group.addButton(self.rb_marked)
        if self.note_ids is not None:
            n = len(self.note_ids)
            wl.addWidget(QLabel(f"{n} notas seleccionadas" if n != 1 else "1 nota seleccionada"))
        else:
            wl.addWidget(self.rb_all)
            wl.addWidget(self.rb_marked)
            self.rb_all.toggled.connect(lambda _on: self._refresh_state())
        lay.addWidget(g_what)

        # opciones de corte
        g_opts = QGroupBox("Opciones de corte")
        form = QFormLayout(g_opts)
        self.merge = QCheckBox("Unir notas cercanas en un solo clip")
        form.addRow("", self.merge)
        self.pre = QDoubleSpinBox()
        self.post = QDoubleSpinBox()
        for spin, value in ((self.pre, ed.clip_pre_s), (self.post, ed.clip_post_s)):
            spin.setRange(0.0, 60.0)
            spin.setDecimals(1)
            spin.setSingleStep(0.5)
            spin.setSuffix(" s")
            spin.setValue(float(value))
        self.pre.valueChanged.connect(lambda v: self._save_editor("clip_pre_s", float(v)))
        self.post.valueChanged.connect(lambda v: self._save_editor("clip_post_s", float(v)))
        form.addRow("Segundos antes de la nota", self.pre)
        form.addRow("Segundos después de lo dicho", self.post)
        self.mode = QComboBox()
        self.mode.addItem("Rápido: sin recodificar (puede empezar unos segundos antes)", "copy")
        self.mode.addItem("Exacto: recodifica con la GPU", "exact")
        self.mode.setCurrentIndex(max(0, self.mode.findData(ed.clip_mode)))
        self.mode.currentIndexChanged.connect(lambda _i: self._save_editor("clip_mode", self.mode.currentData()))
        form.addRow("Modo", self.mode)
        lay.addWidget(g_opts)

        # carpeta de destino
        g_dir = QGroupBox("Carpeta de destino")
        dl = QHBoxLayout(g_dir)
        self.dir_label = QLabel("")
        self.dir_label.setWordWrap(True)
        self.b_dir = QPushButton("Cambiar…")
        self.b_dir.clicked.connect(self._change_dir)
        dl.addWidget(self.dir_label, 1)
        dl.addWidget(self.b_dir)
        lay.addWidget(g_dir)
        self._update_dir_label()

        self.state_label = QLabel("")
        self.state_label.setWordWrap(True)
        lay.addWidget(self.state_label)

        row = QHBoxLayout()
        b_cancel = QPushButton("Cancelar")
        b_cancel.clicked.connect(self.reject)
        self.b_export = QPushButton("Exportar")
        self.b_export.setObjectName("primary")
        self.b_export.clicked.connect(self._export)
        row.addStretch(1)
        row.addWidget(b_cancel)
        row.addWidget(self.b_export)
        lay.addLayout(row)

        self._refresh_state()

    # ---- estado -----------------------------------------------------------------------
    def _recording_problem(self) -> str:
        """Motivo por el que no se pueden cortar clips, o "" si la grabación está disponible."""
        if not self.session:
            return "La sesión no existe."
        obs = self.session.get("obs_path")
        if not obs:
            return "Esta sesión no tiene grabación de OBS asociada, así que no se pueden cortar clips."
        if not Path(obs).is_file():
            return f"No se encuentra la grabación de OBS: {obs}. Si se movió, no se pueden cortar clips desde aquí."
        return ""

    def _target(self) -> tuple[list[int] | None, bool] | None:
        """(note_ids, only_marked) para app.export_clips, o None si no hay nada que exportar."""
        if self.note_ids is not None:
            return (self.note_ids, False) if self.note_ids else None
        if self.rb_marked.isChecked():
            return (None, True)
        return (None, False)

    def _refresh_state(self) -> None:
        problem = self._recording_problem()
        if problem:
            self.rec_label.setText(problem)
            self.rec_label.setStyleSheet(f"color: {COLORS['err']};")
            self.state_label.setText("Para exportar clips, la grabación de la sesión debe estar disponible.")
            self.b_export.setEnabled(False)
            return
        obs = self.session.get("obs_path")
        self.rec_label.setText(f"Grabación: {obs}")
        self.rec_label.setStyleSheet("")
        target = self._target()
        self.b_export.setEnabled(target is not None)
        if target is None:
            self.state_label.setText("No hay notas que exportar con esta selección.")
        else:
            self.state_label.setText("Los clips se cortan en segundo plano; el progreso aparece en la ventana principal.")

    def _update_dir_label(self) -> None:
        self.dir_label.setText(str(default_clip_dir(self.session, self.app.settings)))

    # ---- acciones ---------------------------------------------------------------------
    def _save_editor(self, field: str, value) -> None:
        setattr(self.app.settings.editor, field, value)
        self.app.settings.save()

    def _change_dir(self) -> None:
        actual = str(default_clip_dir(self.session, self.app.settings))
        p = QFileDialog.getExistingDirectory(self, "Carpeta de clips", actual)
        if p:
            self._save_editor("clip_dir", p)
            self._update_dir_label()

    def _export(self) -> None:
        target = self._target()
        if target is None or self._recording_problem():
            return
        note_ids, only_marked = target
        self.app.export_clips(self.session_id, note_ids, only_marked, self.merge.isChecked())
        self.accept()

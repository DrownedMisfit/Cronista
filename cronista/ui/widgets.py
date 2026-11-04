"""Widgets reutilizables: indicador de estado, medidor de nivel, detalle de nota."""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal, QRectF
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QButtonGroup, QComboBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QPushButton, QSizePolicy, QTabWidget, QTextEdit, QVBoxLayout, QWidget)

from cronista.ai.tasks import TRANSFORM_MODES
from cronista.notes.manager import MARKERS
from cronista.session.clock import fmt_timestamp
from cronista.ui.theme import COLORS

SCOPE_BUTTONS = [("current", "Actual"), ("other", "Otro juego"), ("general", "General"), ("personal", "Personal")]
# Versiones de texto de una nota: (clave, etiqueta, campo en la nota). "stt_info.X" = dato dentro de stt_info.
VERSIONS = (("raw", "Original", "text_raw"), ("live", "En vivo", "stt_info.live_text"),
            ("corrected", "Corregida", "text_corrected"), ("improved", "Mejorada", "text_improved"),
            ("script", "Guion", "text_script"))
TRANSFORM_ORDER = ("correct", "improve", "script")
RELATION_LABELS = {"same_topic": "mismo tema", "similar_text": "texto parecido", "same_reference": "misma referencia"}
LOCK_LABELS = {"scope": "ámbito", "functions": "función", "topics": "tema", "refs": "referencias", "text": "texto"}


def is_other_voice(note: dict | None) -> bool:
    """La nota se verificó con el perfil de voz, no coincidió y el usuario no la ha confirmado como suya."""
    voice = ((note or {}).get("stt_info") or {}).get("voice") or {}
    return voice.get("match") is False and not voice.get("confirmed")


def fmt_score(x: float | None) -> str:
    """Similitud de voz con coma decimal ('0,21'); '—' si no hay valor."""
    return "—" if x is None else f"{float(x):.2f}".replace(".", ",")


def _version_value(note: dict | None, field: str) -> str:
    """Texto de una versión: campo de la nota o, si empieza por «stt_info.», dato de su información STT."""
    if not note:
        return ""
    if field.startswith("stt_info."):
        return str((note.get("stt_info") or {}).get(field[len("stt_info."):]) or "")
    return str(note.get(field) or "")


def second_pass_status(stt_info: dict | None) -> str:
    """Texto del estado de la segunda pasada (Whisper) de una nota; '' si no hay nada que mostrar."""
    stt = stt_info or {}
    sp = stt.get("second_pass") or {}
    if sp.get("applied"):
        return f"✓ Repasada con Whisper (similitud {fmt_score(sp.get('similarity'))})"
    if sp.get("done"):
        motivo = f" ({sp['reason']})" if sp.get("reason") else ""
        return f"Repasada con Whisper: se conservó el texto en vivo{motivo}"
    if stt.get("audio_cache"):
        return "Pendiente de repaso con Whisper (al dejar de jugar)"
    return ""


class StatusLabel(QWidget):
    def __init__(self, key: str, parent=None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(0)
        self.key = QLabel(key.upper())
        self.key.setObjectName("statusKey")
        self.val = QLabel("—")
        self.val.setObjectName("statusVal")
        lay.addWidget(self.key)
        lay.addWidget(self.val)

    def set(self, text: str, color: str | None = None) -> None:
        dot = f"<span style='color:{color}'>●</span> " if color else ""
        self.val.setText(f"{dot}{text}")


class LevelMeter(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.level_db = -90.0
        self.noise_db = -60.0
        self.prob = 0.0
        self.speaking = False
        self.setFixedHeight(14)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_level(self, level_db: float, prob: float, speaking: bool, noise_db: float = -60.0) -> None:
        self.level_db, self.prob, self.speaking, self.noise_db = level_db, prob, speaking, noise_db
        self.update()

    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#15171b"))
        p.drawRoundedRect(QRectF(0, 0, w, h), 4, 4)
        frac = max(0.0, min(1.0, (self.level_db + 70) / 70))
        color = QColor(COLORS["ok"]) if self.speaking else QColor("#4b5563") if self.prob < 0.3 else QColor(COLORS["busy"])
        p.setBrush(color)
        p.drawRoundedRect(QRectF(0, 0, w * frac, h), 4, 4)
        nf = max(0.0, min(1.0, (self.noise_db + 70) / 70))
        p.setPen(QPen(QColor(COLORS["warn"]), 2))
        p.drawLine(int(w * nf), 0, int(w * nf), h)
        p.end()


class NoteDetail(QWidget):
    """Detalle de una nota en tres pestañas: «Nota» (edición), «Versiones e IA» y «Relacionados»."""

    changed = Signal(int, dict, str)      # note_id, campos, campo "aprendible" (o "")
    delete_requested = Signal(int)
    open_requested = Signal(dict)
    copy_requested = Signal(dict)
    transform_requested = Signal(int, str)   # note_id, modo: correct | improve | script
    reclassify_requested = Signal(int)       # note_id
    related_selected = Signal(int)           # note_id de un comentario relacionado (doble clic)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.note: dict | None = None
        self._pending: dict[str, int] = {}   # modo -> id de la nota cuya transformación está en curso
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        self.ts = QLabel("--:--:--")
        self.ts.setObjectName("ctxBig")
        self.ts.setStyleSheet("font-family: Consolas, monospace; font-size: 14pt; font-weight: 700;")
        top.addWidget(self.ts)
        self.btn_copy = QPushButton("Copiar timestamp")
        self.btn_open = QPushButton("Abrir en grabación")
        self.btn_copy.clicked.connect(lambda: self.note and self.copy_requested.emit(self.note))
        self.btn_open.clicked.connect(lambda: self.note and self.open_requested.emit(self.note))
        top.addStretch(1)
        top.addWidget(self.btn_copy)
        top.addWidget(self.btn_open)
        lay.addLayout(top)

        self.tabs = QTabWidget()
        lay.addWidget(self.tabs)
        self._build_note_tab()
        self._build_ai_tab()
        self._build_related_tab()

        self.btn_save.clicked.connect(self._save_text_and_tags)
        self.btn_delete.clicked.connect(lambda: self.note and self.delete_requested.emit(self.note["id"]))
        self.marker.currentIndexChanged.connect(self._marker_changed)
        self.scope_group.buttonClicked.connect(self._scope_changed)
        self._loading = False
        self.set_note(None)

    # ---- construcción de pestañas ----------------------------------------------------
    def _build_note_tab(self) -> None:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(6, 6, 6, 6)
        self.text = QTextEdit()
        self.text.setAcceptRichText(False)
        self.text.setPlaceholderText("Texto de la nota")
        self.text.setMinimumHeight(70)
        self.text.setMaximumHeight(140)
        lay.addWidget(self.text)
        self.raw = QLabel("")
        self.raw.setObjectName("liveHint")
        self.raw.setWordWrap(True)
        lay.addWidget(self.raw)

        self.voice_row = QWidget()
        vr = QHBoxLayout(self.voice_row)
        vr.setContentsMargins(0, 0, 0, 0)
        self.voice_lbl = QLabel("")
        self.voice_lbl.setStyleSheet(f"color: {COLORS['warn']};")
        self.btn_my_voice = QPushButton("Es mi voz")
        self.btn_my_voice.setToolTip("Confirma que la frase es tuya: se quita la marca de otra voz")
        self.btn_my_voice.clicked.connect(self._confirm_my_voice)
        vr.addWidget(self.voice_lbl, 1)
        vr.addWidget(self.btn_my_voice)
        self.voice_row.setVisible(False)
        lay.addWidget(self.voice_row)

        row = QHBoxLayout()
        row.addWidget(QLabel("Marcador"))
        self.marker = QComboBox()
        for k, v in MARKERS.items():
            self.marker.addItem(v, k)
        row.addWidget(self.marker, 1)
        row.addWidget(QLabel("Ámbito"))
        self.scope_group = QButtonGroup(self)
        self.scope_group.setExclusive(True)
        for key, label in SCOPE_BUTTONS:
            b = QPushButton(label)
            b.setCheckable(True)
            b.setProperty("scope", key)
            self.scope_group.addButton(b)
            row.addWidget(b)
        lay.addLayout(row)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Función"))
        self.functions = QLineEdit()
        self.functions.setPlaceholderText("descripción, opinión…")
        row2.addWidget(self.functions, 1)
        row2.addWidget(QLabel("Tema"))
        self.topics = QLineEdit()
        self.topics.setPlaceholderText("gameplay, combate…")
        row2.addWidget(self.topics, 1)
        self.conf = QLabel("")
        self.conf.setObjectName("liveHint")
        row2.addWidget(self.conf)
        lay.addLayout(row2)

        row3 = QHBoxLayout()
        self.btn_save = QPushButton("Guardar cambios")
        self.btn_save.setObjectName("primary")
        self.btn_delete = QPushButton("Eliminar")
        self.btn_delete.setObjectName("danger")
        row3.addWidget(self.btn_save)
        row3.addStretch(1)
        row3.addWidget(self.btn_delete)
        lay.addLayout(row3)
        self.tabs.addTab(page, "Nota")

    def _build_ai_tab(self) -> None:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(6, 6, 6, 6)

        row = QHBoxLayout()
        row.addWidget(QLabel("Versión"))
        self.version_group = QButtonGroup(self)
        self.version_group.setExclusive(True)
        self.version_btns: dict[str, QPushButton] = {}
        for key, label, _field in VERSIONS:
            b = QPushButton(label)
            b.setCheckable(True)
            b.setProperty("version", key)
            self.version_group.addButton(b)
            self.version_btns[key] = b
            row.addWidget(b)
        row.addStretch(1)
        self.btn_use_version = QPushButton("Usar esta versión")
        self.btn_use_version.clicked.connect(self._use_version)
        row.addWidget(self.btn_use_version)
        lay.addLayout(row)
        self.version_group.buttonClicked.connect(lambda _b: self._show_version(self._version_key()))
        self.version_view = QTextEdit()
        self.version_view.setReadOnly(True)
        self.version_view.setPlaceholderText("Esta nota no tiene otras versiones todavía")
        self.version_view.setMinimumHeight(48)
        self.version_view.setMaximumHeight(80)
        lay.addWidget(self.version_view)

        row2 = QHBoxLayout()
        self.ai_btns: dict[str, QPushButton] = {}
        for mode in TRANSFORM_ORDER:
            b = QPushButton(TRANSFORM_MODES[mode]["title"])
            b.clicked.connect(lambda _=False, m=mode: self._request_transform(m))
            self.ai_btns[mode] = b
            row2.addWidget(b)
        row2.addStretch(1)
        self.btn_reclassify = QPushButton("Reclasificar con IA")
        self.btn_reclassify.clicked.connect(lambda: self.note and self.reclassify_requested.emit(self.note["id"]))
        row2.addWidget(self.btn_reclassify)
        lay.addLayout(row2)

        row3 = QHBoxLayout()
        self.origin = QLabel("")
        self.origin.setObjectName("liveHint")
        self.subtopic = QLabel("")
        self.subtopic.setObjectName("liveHint")
        row3.addWidget(self.origin)
        row3.addWidget(self.subtopic, 1)
        lay.addLayout(row3)
        self.second_pass = QLabel("")
        self.second_pass.setObjectName("liveHint")
        self.second_pass.setWordWrap(True)
        self.second_pass.setVisible(False)
        lay.addWidget(self.second_pass)
        self.locked = QLabel("")
        self.locked.setWordWrap(True)
        lay.addWidget(self.locked)
        lay.addStretch(1)
        self.tabs.addTab(page, "Versiones e IA")

    def _build_related_tab(self) -> None:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(6, 6, 6, 6)
        title = QLabel("Comentarios relacionados")
        title.setObjectName("heading")
        lay.addWidget(title)
        self.related = QListWidget()
        self.related.setWordWrap(True)
        self.related.setMaximumHeight(110)
        self.related.itemDoubleClicked.connect(self._related_double_clicked)
        lay.addWidget(self.related)
        self.related_empty = QLabel("Todavía no hay comentarios relacionados con esta nota.")
        self.related_empty.setObjectName("liveHint")
        lay.addWidget(self.related_empty)
        # clips exportados de esta nota (fase 3)
        clips_title = QLabel("Clips de esta nota")
        clips_title.setObjectName("heading")
        lay.addWidget(clips_title)
        self.clips_list = QListWidget()
        self.clips_list.setMaximumHeight(90)
        self.clips_list.itemDoubleClicked.connect(self._clip_double_clicked)
        lay.addWidget(self.clips_list)
        self.clips_empty = QLabel("Sin clips exportados para esta nota. Clic derecho en la nota → Exportar clip.")
        self.clips_empty.setObjectName("liveHint")
        self.clips_empty.setWordWrap(True)
        lay.addWidget(self.clips_empty)
        lay.addStretch(1)
        self.tabs.addTab(page, "Relacionados")

    # ---- estado ---------------------------------------------------------------------------
    def set_note(self, note: dict | None) -> None:
        self._loading = True
        self.note = note
        enabled = note is not None
        for w in (self.text, self.marker, self.functions, self.topics, self.btn_save, self.btn_delete, self.btn_copy,
                  self.btn_open, self.related, self.clips_list):
            w.setEnabled(enabled)
        for b in self.scope_group.buttons():
            b.setEnabled(enabled)
        if note is None:
            self.ts.setText("--:--:--")
            self.text.setPlainText("")
            self.raw.setText("")
            self.functions.setText("")
            self.topics.setText("")
            self.conf.setText("")
            self.set_related([])
            self.set_clips([])
        else:
            self.ts.setText(fmt_timestamp(note.get("position_ms")))
            self.text.setPlainText(note.get("text") or "")
            raw = note.get("text_raw") or ""
            self.raw.setText(f"Original: {raw}" if raw and raw != note.get("text") else "")
            idx = self.marker.findData(note.get("marker") or "")
            self.marker.setCurrentIndex(max(0, idx))
            for b in self.scope_group.buttons():
                b.setChecked(b.property("scope") == (note.get("scope") or ""))
            self.functions.setText(", ".join(note.get("functions") or []))
            self.topics.setText(", ".join(note.get("topics") or []))
            c = float(note.get("confidence") or 0)
            amb = " · contexto ambiguo" if note.get("ambiguous") else ""
            self.conf.setText(f"Confianza {int(c * 100)}%{amb}" if c else "")
        self._refresh_versions()
        self._refresh_ai_buttons()
        self._refresh_origin()
        self._refresh_second_pass()
        self._refresh_voice()
        self._loading = False

    def _refresh_voice(self) -> None:
        show = is_other_voice(self.note)
        self.voice_row.setVisible(show)
        if show:
            voice = self.note["stt_info"]["voice"]
            self.voice_lbl.setText(f"🗣️ Posiblemente otra voz (similitud {fmt_score(voice.get('score'))})")

    def _confirm_my_voice(self) -> None:
        if not self.note or not is_other_voice(self.note):
            return
        stt = dict(self.note.get("stt_info") or {})
        voice = dict(stt.get("voice") or {})
        voice.update({"match": True, "confirmed": True})
        stt["voice"] = voice
        self.changed.emit(self.note["id"], {"stt_info": stt}, "")

    def set_related(self, notes: list[dict]) -> None:
        """Comentarios relacionados de la nota: dicts de Store.related_notes (con relation_kind)."""
        self.related.clear()
        for n in notes or []:
            kind = n.get("relation_kind") or ""
            text = " ".join(str(n.get("text") or "").split())
            if len(text) > 120:
                text = text[:117] + "…"
            it = QListWidgetItem(f"[{fmt_timestamp(n.get('position_ms'))}] {text} "
                                 f"({RELATION_LABELS.get(kind, kind)})")
            it.setData(Qt.UserRole, n["id"])
            it.setToolTip(str(n.get("text") or ""))
            self.related.addItem(it)
        self.related_empty.setVisible(not notes)

    def set_clips(self, clips: list[dict]) -> None:
        """Clips exportados de la nota actual (dicts de Store.clips_for_note). Lo llama la ventana principal."""
        self.clips_list.clear()
        for c in clips or []:
            path = str(c.get("path") or "")
            name = os.path.basename(path) or "(sin archivo)"
            text = f"{fmt_timestamp(c.get('start_ms'))}–{fmt_timestamp(c.get('end_ms'))}  ·  {name}"
            it = QListWidgetItem(text)
            it.setData(Qt.UserRole, path)
            it.setToolTip(path)
            self.clips_list.addItem(it)
        self.clips_empty.setVisible(not clips)

    def _clip_double_clicked(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.UserRole) or ""
        if path and os.path.isfile(path):
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            QMessageBox.information(self, "Cronista", f"Archivo no encontrado:\n{path or '(sin ruta)'}")

    def transform_finished(self, note_id: int | None, mode: str, ok: bool) -> None:
        """Fin de una transformación de IA. `mode` vacío = modo desconocido (error): libera la nota.
        `ok` se recibe por simetría con el evento; el texto nuevo ya llega por NOTE_UPDATED."""
        if mode:
            if self._pending.get(mode) == note_id:
                del self._pending[mode]
        else:
            for m in [m for m, nid in self._pending.items() if nid == note_id]:
                del self._pending[m]
        self._refresh_ai_buttons()

    # ---- refrescos internos ---------------------------------------------------------------
    def _version_key(self) -> str | None:
        for key, b in self.version_btns.items():
            if b.isChecked():
                return key
        return None

    def _version_text(self, key: str) -> str:
        field = next(f for k, _label, f in VERSIONS if k == key)
        return _version_value(self.note, field)

    def _refresh_versions(self) -> None:
        note = self.note
        avail = {key: bool(_version_value(note, field)) for key, _label, field in VERSIONS}
        for key, b in self.version_btns.items():
            b.setEnabled(avail[key])
        current = self._version_key()
        if not (current and avail.get(current)):
            current = next((key for key, _label, _field in VERSIONS if avail[key]), None)
        if current is None:
            self.version_group.setExclusive(False)   # un grupo exclusivo no permite desmarcar el activo
            for b in self.version_btns.values():
                b.setChecked(False)
            self.version_group.setExclusive(True)
        else:
            self.version_btns[current].setChecked(True)
        self._show_version(current)

    def _show_version(self, key: str | None) -> None:
        value = self._version_text(key) if (key and self.note) else ""
        self.version_view.setPlainText(value)
        self.btn_use_version.setEnabled(bool(value) and value != (self.note or {}).get("text"))

    def _refresh_ai_buttons(self) -> None:
        nid = self.note["id"] if self.note else None
        for mode, b in self.ai_btns.items():
            busy = nid is not None and self._pending.get(mode) == nid
            b.setText("Procesando…" if busy else TRANSFORM_MODES[mode]["title"])
            b.setEnabled(nid is not None and not busy)
        self.btn_reclassify.setEnabled(nid is not None)

    def _refresh_second_pass(self) -> None:
        text = second_pass_status((self.note or {}).get("stt_info")) if self.note else ""
        self.second_pass.setText(text)
        self.second_pass.setVisible(bool(text))
        sp = ((self.note or {}).get("stt_info") or {}).get("second_pass") or {}
        self.second_pass.setToolTip(f"Modelo: {sp['model']}" if sp.get("model") else "")

    def _refresh_origin(self) -> None:
        if not self.note:
            self.origin.setText("")
            self.origin.setToolTip("")
            self.subtopic.setText("")
            self.locked.setText("")
            return
        ctx = self.note.get("context") or {}
        self.origin.setText("Clasificado por IA local" if ctx.get("source") == "llm"
                            else "Clasificación rápida (heurística)")
        self.origin.setToolTip(str(ctx.get("rationale") or ""))
        sub = ctx.get("subtopic")
        self.subtopic.setText(f"Subtema: {sub}" if sub else "")
        names = [LOCK_LABELS.get(k, k) for k in (ctx.get("user_locked") or [])]
        self.locked.setText(("🔒 Corregido por ti: " + ", ".join(names)) if names else "")

    # ---- acciones -------------------------------------------------------------------------
    def _request_transform(self, mode: str) -> None:
        if not self.note:
            return
        nid = self.note["id"]
        self._pending[mode] = nid
        self._refresh_ai_buttons()
        self.transform_requested.emit(nid, mode)

    def _use_version(self) -> None:
        value = self.version_view.toPlainText()
        if self.note and value:
            self.changed.emit(self.note["id"], {"text": value}, "")

    def _related_double_clicked(self, item: QListWidgetItem) -> None:
        nid = item.data(Qt.UserRole)
        if nid is not None:
            self.related_selected.emit(int(nid))

    def _marker_changed(self) -> None:
        if self._loading or not self.note:
            return
        self.changed.emit(self.note["id"], {"marker": self.marker.currentData()}, "")

    def _scope_changed(self, btn) -> None:
        if self._loading or not self.note:
            return
        self.changed.emit(self.note["id"], {"scope": btn.property("scope")}, "scope")

    def _save_text_and_tags(self) -> None:
        if not self.note:
            return
        fields: dict = {}
        txt = self.text.toPlainText().strip()
        if txt and txt != (self.note.get("text") or ""):
            fields["text"] = txt
        funcs = [x.strip() for x in self.functions.text().split(",") if x.strip()]
        tops = [x.strip() for x in self.topics.text().split(",") if x.strip()]
        learn = ""
        if funcs != (self.note.get("functions") or []):
            fields["functions"] = funcs
            learn = "functions"
        if tops != (self.note.get("topics") or []):
            fields["topics"] = tops
            learn = learn or "topics"
        if fields:
            self.changed.emit(self.note["id"], fields, learn)

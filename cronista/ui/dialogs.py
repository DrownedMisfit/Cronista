"""Diálogos: exportación y envío a chatbot."""
from __future__ import annotations

import html
import threading
import webbrowser
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QTabWidget, QTextBrowser,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from cronista import APP_NAME
from cronista.ai.providers import CHATBOT_URLS, PROVIDERS, SYSTEM_PROMPT, build_provider
from cronista.core import player
from cronista.core.events import Topics
from cronista.core.paths import exports_dir
from cronista.export import editors, exporters
from cronista.notes.manager import MARKERS
from cronista.notes.relations import topic_clusters
from cronista.session.clock import fmt_timestamp
from cronista.ui.bridge import QtBridge

EDL_FPS = 60.0   # fotogramas del EDL para DaVinci Resolve
# formatos propios de este diálogo (además de exporters.FORMATS): clave → (etiqueta, extensión)
EDITOR_FORMATS = {"edl": ("Marcadores EDL para DaVinci Resolve (.edl)", "edl"),
                  "yt": ("Marcas de tiempo para YouTube (.txt)", "txt")}
# documentos para compartir: se generan a partir del Markdown de la sesión
DOCUMENT_FORMATS = {"docx": "Documento Word (.docx) — también Google Docs",
                    "pdf": "Documento PDF (.pdf)",
                    "odt": "OpenDocument (.odt) — Google Docs / LibreOffice"}


class ExportDialog(QDialog):
    def __init__(self, parent, session: dict, notes: list[dict], settings) -> None:
        super().__init__(parent)
        self.setWindowTitle("Exportar sesión")
        self.session, self.notes, self.settings = session, notes, settings
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"{session['game']} · {len(notes)} notas"))
        row = QHBoxLayout()
        row.addWidget(QLabel("Formato"))
        self.fmt = QComboBox()
        for k, v in exporters.FORMATS.items():
            self.fmt.addItem(v, k)
        for k, (label, _ext) in EDITOR_FORMATS.items():
            self.fmt.addItem(label, k)
        for k, label in DOCUMENT_FORMATS.items():
            self.fmt.addItem(label, k)
        row.addWidget(self.fmt, 1)
        lay.addLayout(row)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setMinimumSize(640, 380)
        lay.addWidget(self.preview)
        btns = QHBoxLayout()
        b_copy = QPushButton("Copiar al portapapeles")
        b_copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.preview.toPlainText()))
        b_save = QPushButton("Guardar archivo…")
        b_save.setObjectName("primary")
        b_save.clicked.connect(self._save)
        btns.addWidget(b_copy)
        btns.addStretch(1)
        btns.addWidget(b_save)
        lay.addLayout(btns)
        self.fmt.currentIndexChanged.connect(self._render)
        self._render()

    def _text(self, fmt: str) -> str:
        if fmt in DOCUMENT_FORMATS:
            return exporters.to_markdown(self.session, self.notes)     # vista previa del contenido
        if fmt == "edl":
            return editors.to_marker_edl(self.session, self.notes, fps=EDL_FPS)
        if fmt == "yt":
            return editors.to_youtube_timestamps(self.notes)
        return exporters.render(fmt, self.session, self.notes)

    def _render(self) -> None:
        self.preview.setPlainText(self._text(self.fmt.currentData()))

    def _save(self) -> None:
        fmt = self.fmt.currentData()
        base = Path(self.settings.general.export_dir) if self.settings.general.export_dir else exports_dir()
        if fmt in DOCUMENT_FORMATS:
            from cronista.ui.doc_export import save_markdown_as
            stem = exporters.default_filename(self.session, "x")[:-2]
            if save_markdown_as(self, self._text(fmt), stem, base, title=self.session.get("title"),
                                default_ext=fmt):
                self.accept()
            return
        ext = EDITOR_FORMATS[fmt][1] if fmt in EDITOR_FORMATS else fmt
        base.mkdir(parents=True, exist_ok=True)
        name = exporters.default_filename(self.session, ext)
        if fmt == "yt":
            name = name[:-len(".txt")] + "_youtube.txt"
        default = base / name
        path, _ = QFileDialog.getSaveFileName(self, "Guardar exportación", str(default), f"*.{ext}")
        if path:
            if fmt in EDITOR_FORMATS:
                Path(path).write_text(self._text(fmt), encoding="utf-8")
            else:
                exporters.export_file(fmt, self.session, self.notes, Path(path))
            QMessageBox.information(self, APP_NAME, f"Exportado a:\n{path}")
            self.accept()


class _Worker(QObject):
    done = Signal(str)
    failed = Signal(str)


class ChatbotDialog(QDialog):
    """Prepara un paquete de notas para ChatGPT/Claude/etc. Nada se envía sin pulsar un botón."""

    def __init__(self, parent, app, session: dict, notes: list[dict]) -> None:
        super().__init__(parent)
        self.app, self.session, self.notes = app, session, notes
        self.setWindowTitle("Enviar a chatbot / Analizar sesión")
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"{session['game']} · {len(notes)} notas · Las notas se preparan como texto; "
                             "no se envía audio."))
        row = QHBoxLayout()
        row.addWidget(QLabel("Objetivo"))
        self.goal = QComboBox()
        for k, (title, _) in exporters.CHATBOT_GOALS.items():
            self.goal.addItem(title, k)
        row.addWidget(self.goal, 1)
        self.only_marked = QCheckBox("Solo notas marcadas")
        row.addWidget(self.only_marked)
        lay.addLayout(row)
        self.extra = QLineEdit()
        self.extra.setPlaceholderText("Indicaciones adicionales (opcional): p. ej. 'céntrate en el capítulo 3'")
        lay.addWidget(self.extra)
        self.preview = QPlainTextEdit()
        self.preview.setMinimumSize(720, 360)
        lay.addWidget(self.preview)
        self.parts_label = QLabel("")
        self.parts_label.setObjectName("liveHint")
        lay.addWidget(self.parts_label)

        btns = QHBoxLayout()
        self.b_copy = QPushButton("Copiar al portapapeles")
        self.b_copy.setObjectName("primary")
        self.b_copy.clicked.connect(self._copy)
        btns.addWidget(self.b_copy)
        self.site = QComboBox()
        for name in CHATBOT_URLS:
            self.site.addItem(name)
        idx = self.site.findText(app.settings.ai.chatbot_site)
        self.site.setCurrentIndex(max(0, idx))
        btns.addWidget(self.site)
        b_open = QPushButton("Copiar y abrir chatbot")
        b_open.clicked.connect(self._copy_and_open)
        btns.addWidget(b_open)
        btns.addStretch(1)
        self.provider = build_provider(app.settings.ai)
        self.b_api = QPushButton("Enviar por API (online)")
        self.b_api.setEnabled(self.provider is not None)
        self.b_api.setToolTip("Configura un proveedor en Configuración → IA / Chatbot" if self.provider is None else
                              f"Proveedor: {PROVIDERS.get(app.settings.ai.provider, '')}")
        self.b_api.clicked.connect(self._send_api)
        btns.addWidget(self.b_api)
        lay.addLayout(btns)

        self.response = QPlainTextEdit()
        self.response.setPlaceholderText("Respuesta del modelo (si se envía por API)")
        self.response.setVisible(False)
        self.response.setMinimumHeight(200)
        lay.addWidget(self.response)
        rrow = QHBoxLayout()
        self.b_save_summary = QPushButton("Guardar respuesta como resumen de la sesión")
        self.b_save_summary.setVisible(False)
        self.b_save_summary.clicked.connect(self._save_summary)
        rrow.addWidget(self.b_save_summary)
        self.b_save_doc = QPushButton("Guardar respuesta como documento…")
        self.b_save_doc.setVisible(False)
        self.b_save_doc.clicked.connect(self._save_doc)
        rrow.addWidget(self.b_save_doc)
        rrow.addStretch(1)
        lay.addLayout(rrow)

        self.goal.currentIndexChanged.connect(self._render)
        self.only_marked.toggled.connect(self._render)
        self.extra.textChanged.connect(self._render)
        self._worker = _Worker()
        self._worker.done.connect(self._on_response)
        self._worker.failed.connect(self._on_failed)
        self._parts: list[str] = []
        self._part_idx = 0
        self._render()

    def _selected_notes(self) -> list[dict]:
        return [n for n in self.notes if n.get("marker")] if self.only_marked.isChecked() else self.notes

    def _render(self) -> None:
        text = exporters.build_chatbot_prompt(self.session, self._selected_notes(), self.goal.currentData(),
                                              self.extra.text())
        self.preview.setPlainText(text)
        self._parts = exporters.split_prompt(text)
        self._part_idx = 0
        n = len(self._parts)
        self.parts_label.setText(f"{len(text):,} caracteres" + (f" · se copiará en {n} partes" if n > 1 else ""))
        self.b_copy.setText("Copiar al portapapeles" if n == 1 else f"Copiar parte 1 de {n}")

    def _copy(self) -> None:
        if not self._parts:
            return
        QGuiApplication.clipboard().setText(self._parts[self._part_idx])
        n = len(self._parts)
        if n > 1:
            self._part_idx = (self._part_idx + 1) % n
            self.b_copy.setText(f"Copiar parte {self._part_idx + 1} de {n}")
            self.parts_label.setText(f"Parte {((self._part_idx - 1) % n) + 1} copiada. Pégala y copia la siguiente.")
        else:
            self.parts_label.setText("Copiado. Pégalo en tu chatbot.")

    def _copy_and_open(self) -> None:
        self._copy()
        site = self.site.currentText()
        self.app.settings.ai.chatbot_site = site
        self.app.settings.save()
        webbrowser.open(CHATBOT_URLS[site])

    def _send_api(self) -> None:
        if self.provider is None:
            return
        if QMessageBox.question(self, APP_NAME, "Se enviará el texto de las notas al proveedor configurado "
                                "(conexión a internet). ¿Continuar?") != QMessageBox.Yes:
            return
        self.b_api.setEnabled(False)
        self.b_api.setText("Enviando…")
        prompt = self.preview.toPlainText()

        def run() -> None:
            try:
                self._worker.done.emit(self.provider.complete(prompt, SYSTEM_PROMPT))
            except Exception as e:
                self._worker.failed.emit(str(e))

        threading.Thread(target=run, daemon=True).start()

    def _on_response(self, text: str) -> None:
        self.b_api.setEnabled(True)
        self.b_api.setText("Enviar por API (online)")
        self.response.setPlainText(text)
        self.response.setVisible(True)
        self.b_save_summary.setVisible(True)
        self.b_save_doc.setVisible(True)

    def _on_failed(self, msg: str) -> None:
        self.b_api.setEnabled(True)
        self.b_api.setText("Enviar por API (online)")
        QMessageBox.warning(self, APP_NAME, f"No se pudo obtener respuesta:\n{msg}")

    def _save_doc(self) -> None:
        from cronista.ui.doc_export import save_markdown_as
        base = Path(self.app.settings.general.export_dir) if self.app.settings.general.export_dir else exports_dir()
        title = f"{self.session.get('game', '')} — {self.goal.currentText()}"
        save_markdown_as(self, self.response.toPlainText(), title, base, title=title)

    def _save_summary(self) -> None:
        summary = dict(self.session.get("summary") or {})
        summary[self.goal.currentData()] = self.response.toPlainText()
        self.app.store.update_session(self.session["id"], summary=summary)
        QMessageBox.information(self, APP_NAME, "Guardado en la sesión.")


# ---- informe de sesión ----------------------------------------------------------
SCOPE_NAMES = {"current": "Videojuego actual", "other": "Otros videojuegos", "general": "Temas generales",
               "personal": "Pensamientos personales", "sin clasificar": "Sin clasificar"}
_MUTED = "#8b93a3"


def _esc(value) -> str:
    return html.escape(str(value))


def _fmt_fecha(iso: str | None) -> str:
    try:
        return datetime.fromisoformat(iso or "").strftime("%d/%m/%Y %H:%M")
    except (TypeError, ValueError):
        return iso or "—"


def _html_section(title: str, inner: str) -> str:
    return f"<h3 style='color:{_MUTED};margin-top:16px'>{title}</h3>{inner}"


def _html_count_table(rows: list[tuple[str, int]]) -> str:
    if not rows:
        return f"<p style='color:{_MUTED}'>Sin datos.</p>"
    body = "".join(f"<tr><td>{_esc(k)}</td><td align='right'><b>{v}</b></td></tr>" for k, v in rows)
    return f"<table cellspacing='0' cellpadding='4' width='100%'>{body}</table>"


def _sorted_counts(d: dict) -> list[tuple[str, int]]:
    return sorted(((k, v) for k, v in d.items() if k), key=lambda kv: (-kv[1], kv[0]))


class SessionReportDialog(QDialog):
    """Informe de una sesión: distribución de notas y análisis con IA local (guardado en la sesión)."""

    def __init__(self, parent, app, session_id: int) -> None:
        super().__init__(parent)
        self.app, self.session_id = app, session_id
        self.session: dict = app.store.get_session(session_id) or {}
        self.notes: list[dict] = app.store.list_notes(session_id)
        self.setWindowTitle("Informe de sesión")
        self.resize(900, 650)
        lay = QVBoxLayout(self)

        game = self.session.get("game") or "Sesión sin juego"
        title = QLabel(f"{game} — {_fmt_fecha(self.session.get('started_at'))}")
        title.setStyleSheet("font-size: 16pt; font-weight: 600;")
        lay.addWidget(title)
        meta = QLabel(f"Duración: {fmt_timestamp(self.session.get('duration_ms'))} · Notas: {len(self.notes)}\n"
                      f"Grabación: {self.session.get('obs_path') or 'sin archivo asociado'}")
        meta.setWordWrap(True)
        meta.setObjectName("liveHint")
        lay.addWidget(meta)

        self.tabs = QTabWidget()
        self.dist = QTextBrowser()
        self.dist.setOpenExternalLinks(False)
        self.dist.setHtml(self._distribution_html())
        self.tabs.addTab(self.dist, "Distribución")

        ai = QWidget()
        al = QVBoxLayout(ai)
        self.ai_info = QLabel("")
        self.ai_text = QTextBrowser()
        self.ai_text.setOpenExternalLinks(False)
        self.ai_status = QLabel("")
        self.ai_status.setWordWrap(True)
        row = QHBoxLayout()
        self.btn_analyze = QPushButton("Analizar con IA local")
        self.btn_analyze.setObjectName("primary")
        self.btn_analyze.clicked.connect(self._analyze)
        self.btn_copy = QPushButton("Copiar")
        self.btn_copy.clicked.connect(self._copy)
        self.btn_export = QPushButton("Guardar como documento…")
        self.btn_export.clicked.connect(self._export)
        row.addWidget(self.btn_analyze)
        row.addStretch(1)
        row.addWidget(self.btn_copy)
        row.addWidget(self.btn_export)
        al.addWidget(self.ai_info)
        al.addWidget(self.ai_text, 1)
        al.addWidget(self.ai_status)
        al.addLayout(row)
        self.tabs.addTab(ai, "Análisis IA")
        self.tabs.addTab(self._build_moments(), "Momentos")
        lay.addWidget(self.tabs, 1)

        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        self._refresh_analysis()
        self._bridge = QtBridge(app.bus, self, topics=(Topics.AI_RESULT, Topics.AI_ERROR,
                                                       Topics.JOB_PROGRESS, Topics.JOB_DONE))
        self._bridge.event.connect(self._on_event)

    # ---- momentos destacados ----------------------------------------------------
    def _build_moments(self) -> QWidget:
        w = QWidget()
        ml = QVBoxLayout(w)
        self.moments_info = QLabel("")
        self.moments_info.setWordWrap(True)
        ml.addWidget(self.moments_info)
        self.moments_tree = QTreeWidget()
        self.moments_tree.setRootIsDecorated(False)
        self.moments_tree.setAlternatingRowColors(True)
        self.moments_tree.setHeaderLabels(["Inicio–Fin", "Puntuación", "Tipo", "Título", "Motivos"])
        ml.addWidget(self.moments_tree, 1)
        self.moments_status = QLabel("")
        self.moments_status.setWordWrap(True)
        ml.addWidget(self.moments_status)
        row = QHBoxLayout()
        self.btn_detect = QPushButton("Detectar momentos")
        self.btn_detect.setObjectName("primary")
        self.btn_detect.clicked.connect(self._detect_moments)
        self.btn_open_rec = QPushButton("Abrir en la grabación")
        self.btn_open_rec.clicked.connect(self._open_moment)
        self.btn_copy_marks = QPushButton("Copiar marcas de tiempo")
        self.btn_copy_marks.clicked.connect(self._copy_moment_marks)
        row.addWidget(self.btn_detect)
        row.addWidget(self.btn_open_rec)
        row.addStretch(1)
        row.addWidget(self.btn_copy_marks)
        ml.addLayout(row)
        self._refresh_moments()
        return w

    def _refresh_moments(self) -> None:
        moments = self.app.store.list_moments(self.session_id)
        self.moments_tree.clear()
        for m in moments:
            item = QTreeWidgetItem([
                f"{fmt_timestamp(m.get('start_ms'))}–{fmt_timestamp(m.get('end_ms'))}",
                f"{float(m.get('score') or 0):.2f}",
                m.get("kind") or "",
                m.get("title") or "",
                " · ".join(str(r) for r in m.get("reasons") or []),
            ])
            item.setData(0, Qt.UserRole, int(m.get("start_ms") or 0))
            self.moments_tree.addTopLevelItem(item)
        for col in (0, 1, 2, 3):
            self.moments_tree.resizeColumnToContents(col)
        has = bool(moments)
        self.moments_tree.setVisible(has)
        self.btn_copy_marks.setEnabled(has)
        self.btn_open_rec.setEnabled(has and bool(self.session.get("obs_path")))
        if has:
            self.moments_info.setText(
                f"{len(moments)} momentos destacados, ordenados por tiempo. Se calculan a partir de tus comentarios "
                "y, si lo permites, de la imagen y el sonido de la grabación.")
        else:
            self.moments_info.setText(
                "Aún no hay momentos detectados en esta sesión. Pulsa «Detectar momentos» para buscar los tramos más "
                "destacados, a partir de tus comentarios y, si lo permites, de la imagen y el sonido de la grabación.")

    def _detect_moments(self) -> None:
        if self.app.jobs.busy("moments"):
            self.moments_status.setText("Ya hay un análisis de momentos en curso.")
            return
        use_media = False
        if self.session.get("obs_path"):
            box = QMessageBox(self)
            box.setWindowTitle(APP_NAME)
            box.setText("¿Incluir la imagen y el sonido de la grabación en el análisis?")
            box.setInformativeText("Tarda más, pero también detecta momentos de acción y de sonido. "
                                   "Sin ello, solo se usan tus comentarios.")
            b_media = box.addButton("Sí, con imagen y sonido", QMessageBox.AcceptRole)
            b_notes = box.addButton("Solo mis comentarios", QMessageBox.ActionRole)
            box.addButton(QMessageBox.Cancel)
            box.exec()
            clicked = box.clickedButton()
            if clicked is b_media:
                use_media = True
            elif clicked is b_notes:
                use_media = False
            else:
                return
        self.btn_detect.setEnabled(False)
        self.moments_status.setText("Detectando momentos…")
        self.app.analyze_moments(self.session_id, use_media=use_media)

    def _open_moment(self) -> None:
        item = self.moments_tree.currentItem()
        if item is None:
            self.moments_status.setText("Selecciona un momento de la lista.")
            return
        try:
            msg = player.open_at(self.session.get("obs_path") or "", int(item.data(0, Qt.UserRole)),
                                 self.app.settings.general.player_command)
            self.moments_status.setText(msg)
        except OSError as e:
            QMessageBox.warning(self, APP_NAME, f"No se pudo abrir la grabación:\n{e}")

    def _copy_moment_marks(self) -> None:
        moments = self.app.store.list_moments(self.session_id)
        if not moments:
            return
        text = "\n".join(f"{fmt_timestamp(m.get('start_ms'))} {m.get('title') or ''}".rstrip() for m in moments)
        QGuiApplication.clipboard().setText(text + "\n")
        self.moments_status.setText(f"{len(moments)} marcas de tiempo copiadas al portapapeles.")

    # ---- distribución ---------------------------------------------------------
    def _distribution_html(self) -> str:
        stats = self.app.store.session_stats(self.session_id)
        rel = self.app.store.session_relations(self.session_id)   # una sola consulta
        clusters = topic_clusters(self.notes, rel)

        scope = stats["scope"]
        scope_rows = [(SCOPE_NAMES[k], scope[k]) for k in SCOPE_NAMES if k in scope]
        scope_rows += [(k, v) for k, v in scope.items() if k not in SCOPE_NAMES]
        markers = [(MARKERS[k], v) for k, v in stats["markers"].items() if k in MARKERS]

        parts = [
            _html_section("TEMAS", _html_count_table(_sorted_counts(stats["topics"]))),
            _html_section("TIPOS DE COMENTARIO", _html_count_table(_sorted_counts(stats["functions"]))),
            _html_section("ÁMBITO", _html_count_table(scope_rows)),
            _html_section("MARCADORES", _html_count_table(markers)),
            _html_section("REFERENCIAS A OTROS JUEGOS", _html_count_table(_sorted_counts(stats["references"]))),
        ]

        if clusters:
            filas = []
            for c in clusters:
                tema = " / ".join(x for x in (c["topic"], c["subtopic"]) if x) or "Sin tema"
                rango = f"{fmt_timestamp(c['first_ms'])}–{fmt_timestamp(c['last_ms'])}"
                filas.append(f"<tr><td>{_esc(tema)}</td><td align='right'>{c['count']} comentarios</td>"
                             f"<td align='right'>{rango}</td></tr>")
            grupos = f"<table cellspacing='0' cellpadding='4' width='100%'>{''.join(filas)}</table>"
        else:
            grupos = f"<p style='color:{_MUTED}'>Sin datos.</p>"
        parts.append(_html_section("GRUPOS DE COMENTARIOS RELACIONADOS", grupos))
        return "".join(parts)

    # ---- análisis IA ----------------------------------------------------------
    def _current_summary(self) -> tuple[str, str, str] | None:
        """(origen, markdown, fecha) del análisis guardado; prioriza el local."""
        summ = self.session.get("summary") or {}
        for key in ("local", "api"):
            item = summ.get(key)
            if isinstance(item, dict) and item.get("markdown"):
                return key, str(item["markdown"]), str(item.get("created_at") or "")
        return None

    def _refresh_analysis(self) -> None:
        cur = self._current_summary()
        if cur:
            origen, md, when = cur
            self.ai_info.setText(f"Análisis con {'IA local' if origen == 'local' else 'API'} · {_fmt_fecha(when)}")
            self.ai_text.setMarkdown(md)
            self.btn_analyze.setText("Regenerar")
        else:
            self.ai_info.setText("")
            self.ai_text.setPlainText("Aún no se ha analizado esta sesión.")
            self.btn_analyze.setText("Analizar con IA local")
        self.btn_copy.setEnabled(cur is not None)
        self.btn_export.setEnabled(cur is not None)

    def _analyze(self) -> None:
        ok, motivo = self.app.ai.availability()
        if not ok:
            QMessageBox.warning(self, APP_NAME, f"{motivo}\n\nPuedes configurarla en Configuración → IA local.")
            return
        self.app.ai.request_summary(self.session_id, "local")
        self.btn_analyze.setEnabled(False)
        self.ai_status.setText("Analizando… puede tardar un poco")

    def _copy(self) -> None:
        cur = self._current_summary()
        if cur:
            QGuiApplication.clipboard().setText(cur[1])
            self.ai_status.setText("Copiado al portapapeles.")

    def _export(self) -> None:
        cur = self._current_summary()
        if not cur:
            return
        base = Path(self.app.settings.general.export_dir) if self.app.settings.general.export_dir else exports_dir()
        base.mkdir(parents=True, exist_ok=True)
        from cronista.ui.doc_export import save_markdown_as
        stem = exporters.default_filename(self.session, "x")[:-2] + "_analisis"
        out = save_markdown_as(self, cur[1], stem, base, title=f"{self.session.get('game', '')} — análisis")
        if out:
            self.ai_status.setText(f"Guardado en {out}")

    def _on_event(self, ev) -> None:
        try:
            if ev.topic == Topics.AI_RESULT and ev.get("kind") == "summary" and ev.get("session_id") == self.session_id:
                self.session = self.app.store.get_session(self.session_id) or self.session
                self.btn_analyze.setEnabled(True)
                self.ai_status.setText("Análisis completado.")
                self._refresh_analysis()
            elif ev.topic == Topics.AI_ERROR and ev.get("session_id") == self.session_id:
                self.btn_analyze.setEnabled(True)
                self.ai_status.setText(f"No se pudo analizar: {ev.get('message')}")
            elif ev.topic == Topics.JOB_PROGRESS and ev.get("job") == "moments":
                self.moments_status.setText(str(ev.get("text") or ""))
            elif ev.topic == Topics.JOB_DONE and ev.get("job") == "moments":
                self.btn_detect.setEnabled(True)
                self._refresh_moments()
                if ev.get("ok"):
                    self.moments_status.setText(str(ev.get("message") or ""))
                else:
                    self.moments_status.setText(f"No se pudieron detectar los momentos: {ev.get('message')}")
        except RuntimeError:
            pass

    # ---- cierre ---------------------------------------------------------------
    def done(self, r: int) -> None:
        self._bridge.close()
        super().done(r)

    def closeEvent(self, e) -> None:
        self._bridge.close()
        super().closeEvent(e)

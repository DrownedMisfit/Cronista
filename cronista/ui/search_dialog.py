"""Buscador de notas de todas las sesiones: consulta por significado y/o palabras, filtros,
comparación entre juegos y «notas parecidas». Las búsquedas corren en un hilo para no bloquear la
interfaz; el resultado vuelve al hilo de Qt por una señal.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QPushButton, QTabWidget, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from cronista.context.engine import FUNCTIONS, TOPICS
from cronista.core.events import Event, Topics
from cronista.search.embedder import model_available
from cronista.session.clock import fmt_timestamp
from cronista.ui.bridge import QtBridge
from cronista.ui.theme import COLORS
from cronista.ui.widgets import fmt_score

log = logging.getLogger(__name__)

SCOPE_OPTIONS = [("Todos", ""), ("Juego actual", "current"), ("Otro juego", "other"),
                 ("Tema general", "general"), ("Personal", "personal")]
OPINION_OPTIONS = [("Todas", ""), ("Solo críticas y problemas", "critical"), ("Solo lo que me gustó", "liked")]
OPINION_TIP = ("El buscador entiende el tema, pero no distingue por sí solo opiniones positivas de negativas.\n"
               "Por eso existe este filtro: «críticas» usa la función crítica y las notas marcadas como problema; "
               "«me gustó» usa las notas marcadas como me gustó.")
FETCH_LIMIT = 100        # resultados que pide al motor antes de filtrar por opinión
RESULT_LIMIT = 30        # resultados que se muestran
COMPARE_PER_GAME = 8     # resultados por juego en la comparación
MATCH_TIP = "semántica: por significado · palabras: coincidencia de palabras · ambas"


def _contar(n: int) -> str:
    """«1 resultado» / «5 resultados»."""
    return f"{n} resultado" if n == 1 else f"{n} resultados"


def _opinion_ok(note: dict[str, Any], opinion: str) -> bool:
    if opinion == "critical":
        return "crítica" in (note.get("functions") or []) or note.get("marker") == "problem"
    if opinion == "liked":
        return note.get("marker") == "liked"
    return True


def _passes_filters(note: dict[str, Any], scope: str, function: str, topic: str, opinion: str) -> bool:
    if scope and note.get("scope") != scope:
        return False
    if function and function not in (note.get("functions") or []):
        return False
    if topic and topic not in (note.get("topics") or []):
        return False
    return _opinion_ok(note, opinion)


class SearchDialog(QDialog):
    """Buscador. `on_open_note(session_id, note_id)` lo llama la ventana principal al hacer doble clic en un resultado."""

    _finished = Signal(object)   # payload dict: token, kind, data, error (emitido desde el hilo de búsqueda)

    def __init__(self, parent: QWidget | None = None, app: Any = None,
                 on_open_note: Callable[[int, int], None] | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self._on_open_note = on_open_note
        self._token = 0
        self._similar_note_id: int | None = None
        self.setWindowTitle("Buscar en todas las notas")
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.resize(1040, 660)
        self._build()
        self._finished.connect(self._on_finished)
        self.bridge = QtBridge(app.bus, self, topics=(Topics.JOB_PROGRESS, Topics.JOB_DONE))
        self.bridge.event.connect(self._on_bus_event)
        self._reload_games()
        self._refresh_model_status()
        self._start_index_if_possible()
        self.query.setFocus()

    # ---------------------------------------------------------------- construcción
    def _build(self) -> None:
        lay = QVBoxLayout(self)

        # estado del índice
        row = QHBoxLayout()
        self.lbl_index = QLabel("Índice de búsqueda: comprobando…")
        self.lbl_index.setObjectName("liveHint")
        self.lbl_index.setWordWrap(True)
        self.btn_index = QPushButton("Actualizar índice")
        self.btn_index.setToolTip("Calcula el significado de las notas nuevas o modificadas (incremental).")
        self.btn_index.clicked.connect(self._update_index)
        row.addWidget(self.lbl_index, 1)
        row.addWidget(self.btn_index)
        lay.addLayout(row)
        self.lbl_model = QLabel("Modelo de búsqueda no disponible: se usa solo búsqueda por palabras.")
        self.lbl_model.setStyleSheet(f"color: {COLORS['warn']};")
        self.lbl_model.setWordWrap(True)
        lay.addWidget(self.lbl_model)

        # consulta
        qrow = QHBoxLayout()
        self.query = QLineEdit()
        self.query.setPlaceholderText("Escribe lo que buscas, por significado o por palabras (Enter para buscar)")
        self.query.returnPressed.connect(self._run)
        self.btn_search = QPushButton("Buscar")
        self.btn_search.setObjectName("primary")
        self.btn_search.clicked.connect(self._run)
        qrow.addWidget(self.query, 1)
        qrow.addWidget(self.btn_search)
        lay.addLayout(qrow)

        # filtros
        frow = QHBoxLayout()
        self.f_game = QComboBox()
        self.f_scope = self._combo(SCOPE_OPTIONS)
        self.f_function = QComboBox()
        self.f_function.addItem("Todas", "")
        for f in FUNCTIONS:
            self.f_function.addItem(f, f)
        self.f_topic = QComboBox()
        self.f_topic.addItem("Todos", "")
        for t in TOPICS:
            self.f_topic.addItem(t, t)
        self.f_opinion = self._combo(OPINION_OPTIONS)
        self.f_opinion.setToolTip(OPINION_TIP)
        for label, widget in (("Juego", self.f_game), ("Ámbito", self.f_scope), ("Función", self.f_function),
                              ("Tema", self.f_topic), ("Opinión", self.f_opinion)):
            frow.addWidget(QLabel(label))
            frow.addWidget(widget, 1)
        lay.addLayout(frow)

        # pestañas: búsqueda y comparación entre juegos
        self.tabs = QTabWidget()
        page = QWidget()
        pl = QVBoxLayout(page)
        self.count_lbl = QLabel("")
        pl.addWidget(self.count_lbl)
        self.results = self._tree(grouped=False)
        self.results.itemDoubleClicked.connect(self._open_item)
        pl.addWidget(self.results, 1)
        self.tabs.addTab(page, "Buscar")

        page = QWidget()
        pl = QVBoxLayout(page)
        pl.addWidget(QLabel("Juegos a comparar (se usa la misma consulta y los filtros de función, tema, "
                            "ámbito y opinión):"))
        self.games_list = QListWidget()
        self.games_list.setMaximumHeight(120)
        pl.addWidget(self.games_list)
        self.compare_lbl = QLabel("")
        pl.addWidget(self.compare_lbl)
        self.cmp_results = self._tree(grouped=True)
        self.cmp_results.itemDoubleClicked.connect(self._open_item)
        pl.addWidget(self.cmp_results, 1)
        self.tabs.addTab(page, "Comparar entre juegos")
        self.tabs.currentChanged.connect(self._tab_changed)
        lay.addWidget(self.tabs, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    @staticmethod
    def _combo(options: list[tuple[str, str]]) -> QComboBox:
        cb = QComboBox()
        for label, value in options:
            cb.addItem(label, value)
        return cb

    @staticmethod
    def _tree(grouped: bool) -> QTreeWidget:
        t = QTreeWidget()
        t.setColumnCount(6)
        t.setHeaderLabels(["Puntuación", "Juego", "Sesión (fecha)", "Momento", "Texto", "Coincidencia"])
        t.setRootIsDecorated(grouped)
        t.setAlternatingRowColors(True)
        t.setSelectionBehavior(QTreeWidget.SelectRows)
        t.setEditTriggers(QTreeWidget.NoEditTriggers)
        header = t.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.Stretch)
        return t

    def _reload_games(self) -> None:
        games = self.app.store.list_games()
        self.f_game.clear()
        self.f_game.addItem("Todos los juegos", "")
        for g in games:
            self.f_game.addItem(g, g)
        self.games_list.clear()
        for g in games:
            it = QListWidgetItem(g)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked)
            self.games_list.addItem(it)

    # ---------------------------------------------------------------- índice
    def _refresh_model_status(self) -> None:
        self.lbl_model.setVisible(not model_available())

    def _start_index_if_possible(self) -> None:
        """Al abrir el buscador se actualiza el índice (incremental y rápido), si el modelo está disponible."""
        if not model_available():
            self.lbl_index.setText("Índice no actualizado: falta el modelo de búsqueda.")
            return
        self.app.index_search()
        self.btn_index.setEnabled(not self.app.jobs.busy("index"))

    def _update_index(self) -> None:
        self.app.index_search()     # si ya hay una indexación en curso, no añade otra
        self.btn_index.setEnabled(False)
        self.lbl_index.setText("Índice: actualizando…")

    def _on_bus_event(self, ev: Event) -> None:
        if ev.get("job") != "index":
            return
        if ev.topic == Topics.JOB_PROGRESS:
            self.btn_index.setEnabled(False)
            self.lbl_index.setText(f"Índice: {ev.get('text') or 'actualizando…'}")
            return
        # JOB_DONE
        self.btn_index.setEnabled(True)
        message = str(ev.get("message") or "")
        if ev.get("ok"):
            self.lbl_index.setStyleSheet("")
            self.lbl_index.setText(f"Índice: {message}")
            indexed = int((ev.get("result") or {}).get("indexed") or 0)
            if indexed and self.tabs.currentIndex() == 0 and self._similar_note_id is None and self.query.text().strip():
                self._run()     # hay notas nuevas en el índice: repetir la búsqueda con ellas
        elif message == "Cancelado":
            self.lbl_index.setText("Índice: cancelado")
        else:
            self.lbl_index.setStyleSheet(f"color: {COLORS['warn']};")
            self.lbl_index.setText(f"Índice: no se pudo actualizar ({message})")

    # ---------------------------------------------------------------- búsqueda en hilo
    def _start(self, kind: str, fn: Callable[[Any], Any]) -> None:
        """Ejecuta fn(motor_de_búsqueda) en un hilo. El resultado llega a _on_finished en el hilo de Qt."""
        self._token += 1
        token = self._token
        engine = self.app.search        # se crea aquí (hilo de Qt) para no hacerlo dos veces a la vez
        self._set_busy(True)

        def work() -> None:
            try:
                payload = {"token": token, "kind": kind, "data": fn(engine), "error": ""}
            except Exception as e:
                log.exception("Búsqueda fallida")
                payload = {"token": token, "kind": kind, "data": None, "error": str(e) or type(e).__name__}
            try:
                self._finished.emit(payload)
            except RuntimeError:
                pass    # el diálogo se cerró mientras buscaba

        threading.Thread(target=work, name="search", daemon=True).start()

    def _set_busy(self, busy: bool) -> None:
        self.btn_search.setEnabled(not busy)
        if busy:
            self.count_lbl.setText("Buscando…")
            self.compare_lbl.setText("Buscando…")

    def _on_finished(self, payload: dict) -> None:
        if payload.get("token") != self._token:
            return      # resultado de una búsqueda superada por otra más reciente
        self._set_busy(False)
        if payload.get("error"):
            self.count_lbl.setText(f"Error al buscar: {payload['error']}")
            self.compare_lbl.setText(f"Error al buscar: {payload['error']}")
            return
        kind = payload.get("kind")
        if kind == "search":
            self._show_results(payload.get("data") or [])
        elif kind == "compare":
            self._show_compare(payload.get("data") or {})
        elif kind == "similar":
            self._show_similar(payload.get("data") or [])

    def _run(self) -> None:
        if self.tabs.currentIndex() == 1:
            self._run_compare()
        else:
            self._run_search()

    def _run_search(self) -> None:
        q = self.query.text().strip()
        game = self.f_game.currentData() or None
        scope, function, topic = self.f_scope.currentData(), self.f_function.currentData(), self.f_topic.currentData()
        opinion = self.f_opinion.currentData() or ""
        self._similar_note_id = None

        def job(engine):
            rows = engine.search(q, game=game, scope=scope or None, function=function or None,
                                 topic=topic or None, limit=FETCH_LIMIT)
            return [n for n in rows if _opinion_ok(n, opinion)][:RESULT_LIMIT]

        self._start("search", job)

    def _run_compare(self) -> None:
        games = [self.games_list.item(i).text() for i in range(self.games_list.count())
                 if self.games_list.item(i).checkState() == Qt.Checked]
        if not games:
            self.compare_lbl.setText("Marca al menos un juego para comparar.")
            return
        q = self.query.text().strip()
        scope = self.f_scope.currentData() or ""
        function = self.f_function.currentData() or ""
        topic = self.f_topic.currentData() or ""
        opinion = self.f_opinion.currentData() or ""

        def job(engine):
            raw = engine.compare_games(q, games, per_game=FETCH_LIMIT)
            out: dict[str, list[dict[str, Any]]] = {}
            for g in games:
                rows = [n for n in raw.get(g, []) if _passes_filters(n, scope, function, topic, opinion)]
                out[g] = rows[:COMPARE_PER_GAME]
            return out

        self._start("compare", job)

    def show_similar(self, note_id: int) -> None:
        """Modo «notas parecidas»: rellena los resultados con las notas más parecidas a `note_id`."""
        self._similar_note_id = int(note_id)
        self.tabs.setCurrentIndex(0)
        note = self.app.store.get_note(int(note_id))
        text = " ".join(str((note or {}).get("text") or "").split())
        short = text if len(text) <= 60 else text[:57] + "…"
        self.setWindowTitle("Notas parecidas")
        self._similar_head = f"Notas parecidas a «{short}»" if short else "Notas parecidas"
        self._start("similar", lambda engine: engine.similar_notes(int(note_id), limit=RESULT_LIMIT))

    # ---------------------------------------------------------------- resultados
    def _note_item(self, n: dict[str, Any]) -> QTreeWidgetItem:
        text = " ".join(str(n.get("text") or n.get("text_raw") or "").split())
        short = text if len(text) <= 200 else text[:197] + "…"
        title = str(n.get("session_title") or "").strip()
        started = str(n.get("session_started") or "")[:16].replace("T", " ")
        session = f"{title} · {started}" if title and started else (title or started)
        score = n.get("score")
        match = str(n.get("match") or "")
        it = QTreeWidgetItem([fmt_score(score) if score is not None else "—", str(n.get("game") or ""), session,
                              fmt_timestamp(n.get("position_ms")), short, match])
        it.setData(0, Qt.UserRole, (int(n["session_id"]), int(n["id"])))
        it.setToolTip(4, text)
        it.setToolTip(5, MATCH_TIP)
        return it

    def _show_results(self, rows: list[dict[str, Any]]) -> None:
        self.results.clear()
        for n in rows:
            self.results.addTopLevelItem(self._note_item(n))
        if rows:
            self.count_lbl.setText(_contar(len(rows)))
        else:
            self.count_lbl.setText("Sin resultados. Prueba otras palabras, quita filtros o actualiza el índice.")
        self.results.resizeColumnToContents(0)

    def _show_similar(self, rows: list[dict[str, Any]]) -> None:
        self.results.clear()
        for n in rows:
            self.results.addTopLevelItem(self._note_item(n))
        head = getattr(self, "_similar_head", "Notas parecidas")
        self.count_lbl.setText(f"{head} — {_contar(len(rows))}" if rows else
                               f"{head} — ninguna. ¿Está al día el índice? (Actualizar índice)")

    def _show_compare(self, data: dict[str, list[dict[str, Any]]]) -> None:
        self.cmp_results.clear()
        total = 0
        for game, rows in data.items():
            group = QTreeWidgetItem(["", game, "", "", _contar(len(rows)), ""])
            for n in rows:
                group.addChild(self._note_item(n))
            self.cmp_results.addTopLevelItem(group)
            group.setExpanded(True)
            total += len(rows)
        self.compare_lbl.setText((f"{_contar(total)} en {len(data)} juegos") if data else "Sin juegos seleccionados.")

    def _open_item(self, item: QTreeWidgetItem, column: int = 0) -> None:
        ref = item.data(0, Qt.UserRole)
        if not ref or self._on_open_note is None:
            return      # cabecera de juego, o sin callback
        session_id, note_id = ref
        self._on_open_note(int(session_id), int(note_id))
        self.accept()

    # ---------------------------------------------------------------- pestañas y cierre
    def _tab_changed(self, index: int) -> None:
        comparing = index == 1
        self.f_game.setEnabled(not comparing)
        self.btn_search.setText("Comparar" if comparing else "Buscar")

    def done(self, result: int) -> None:
        try:
            self.bridge.close()     # dejar de escuchar el bus al cerrar
        except Exception:
            log.debug("No se pudo cerrar el puente del bus", exc_info=True)
        super().done(result)

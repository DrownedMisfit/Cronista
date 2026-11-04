"""Ventana principal: estado en tiempo real, transcripción en vivo, notas de la
sesión, lista de sesiones, panel de contexto y bandeja. Modular: cada panel es un
dock que se puede ocultar."""
from __future__ import annotations

import importlib
import logging
import os
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (QApplication, QComboBox, QDockWidget, QFileDialog, QFrame, QHBoxLayout, QInputDialog,
                               QLabel, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QProgressBar,
                               QPushButton, QSizePolicy, QSplitter, QVBoxLayout, QWidget, QLineEdit)

from cronista import APP_NAME, __version__
from cronista.app import App
from cronista.core.events import Event, Topics
from cronista.core.player import open_at
from cronista.export import exporters
from cronista.ai.tasks import TRANSFORM_MODES
from cronista.notes import relations
from cronista.notes.manager import MARKERS
from cronista.session.clock import fmt_timestamp
from cronista.ui.bridge import QtBridge
from cronista.ui.theme import COLORS
from cronista.ui.tray import TrayIcon
from cronista.ui.widgets import LevelMeter, NoteDetail, StatusLabel, SCOPE_BUTTONS, fmt_score, is_other_voice

log = logging.getLogger(__name__)

OTHER_VOICE_FILTER = "__other_voice"   # valor del filtro "🗣️ Otra voz" (no es un marcador)

SCOPE_STAT_LABELS = {"current": "juego actual", "other": "otros juegos", "general": "tema general",
                     "personal": "personal", "sin clasificar": "sin clasificar"}
MODE_LABELS = {"manual": "Manual (F8)", "auto": "Automático", "hybrid": "Híbrido"}
MARKER_ICON = {"important": "⭐", "video_idea": "🔥", "use_in_video": "🎬", "develop": "📝", "problem": "❌", "liked": "👍"}
JOB_LABELS = {"reclaimer": "Proyecto de Reclaimer", "clips": "Clips de notas", "moments": "Momentos destacados",
              "index": "Índice de búsqueda", "script": "Guion"}


class MainWindow(QMainWindow):
    def __init__(self, app: App, qapp: QApplication) -> None:
        super().__init__()
        self.app = app
        self.qapp = qapp
        self.settings = app.settings
        self.view_session_id: int | None = None
        self._partial_utt = ""
        self._quitting = False
        self._moments_session: int | None = None   # sesión para la que se pidió «Detectar momentos»
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1280, 780)
        self._build_menu()
        self._build_central()
        self._build_docks()
        self.statusBar().showMessage("Listo")
        self._build_job_bar()
        self.tray = TrayIcon(self)
        self._wire_tray()
        self.tray.show()
        self.bridge = QtBridge(app.bus, self)
        self.bridge.event.connect(self._on_event)
        self._apply_ui_settings()
        self.refresh_sessions()
        self._refresh_status_all()
        self._apply_ai_status(self.app.ai.state, self.app.ai.detail, self.app.ai.queue_size())
        if self.app.sessions.current:
            self.show_session(self.app.sessions.current["id"])

    # ---------------------------------------------------------------- construcción
    def _build_menu(self) -> None:
        mb = self.menuBar()
        m_sess = mb.addMenu("&Sesión")
        self.act_toggle_session = QAction("Iniciar sesión manual", self)
        self.act_toggle_session.triggered.connect(self.toggle_session)
        self.act_rename_game = QAction("Cambiar juego de la sesión…", self)
        self.act_rename_game.triggered.connect(self.rename_game)
        self.act_manual_note = QAction("Añadir nota escrita…", self)
        self.act_manual_note.setShortcut(QKeySequence("Ctrl+N"))
        self.act_manual_note.triggered.connect(self.add_manual_note)
        self.act_export = QAction("Exportar sesión…", self)
        self.act_export.triggered.connect(self.export_session)
        self.act_chatbot = QAction("Enviar a chatbot / Analizar…", self)
        self.act_chatbot.triggered.connect(self.open_chatbot)
        self.act_report = QAction("Informe de la sesión…", self)
        self.act_report.triggered.connect(self.open_session_report)
        self.act_ai_summary = QAction("Analizar sesión con IA local", self)
        self.act_ai_summary.triggered.connect(self.analyze_session_ai)
        self.act_ai_reclass = QAction("Reclasificar sesión con IA", self)
        self.act_ai_reclass.triggered.connect(self.reclassify_session_ai)
        self.act_open_rec = QAction("Abrir grabación", self)
        self.act_open_rec.triggered.connect(lambda: self.open_recording(None))
        self.act_quit = QAction("Cerrar Cronista", self)
        self.act_quit.triggered.connect(self.quit_app)
        for a in (self.act_toggle_session, self.act_rename_game, self.act_manual_note, None, self.act_export,
                  self.act_chatbot, self.act_report, self.act_ai_summary, self.act_ai_reclass, self.act_open_rec,
                  None):
            m_sess.addSeparator() if a is None else m_sess.addAction(a)

        # ---- editor y video (fase 3) ----
        self.act_reclaimer = QAction("Enviar a Reclaimer…", self)
        self.act_reclaimer.triggered.connect(lambda: self.send_to_reclaimer())
        self.act_clips = QAction("Exportar clips de notas…", self)
        self.act_clips.triggered.connect(lambda: self.export_clips_dialog())
        self.act_moments = QAction("Detectar momentos destacados…", self)
        self.act_moments.triggered.connect(lambda: self.detect_moments())
        self.act_script = QAction("Generar guion…", self)
        self.act_script.triggered.connect(lambda: self.generate_script_dialog())
        m_sess.addSection("Editor y video")
        for a in (self.act_reclaimer, self.act_clips, self.act_moments, self.act_script):
            m_sess.addAction(a)
        m_sess.addSeparator()
        m_sess.addAction(self.act_quit)

        m_search = mb.addMenu("&Buscar")
        self.act_search = QAction("Buscar en todas las notas…", self)
        self.act_search.setShortcut(QKeySequence("Ctrl+Shift+F"))
        self.act_search.triggered.connect(lambda: self.open_search())
        m_search.addAction(self.act_search)

        m_listen = mb.addMenu("&Escucha")
        self.act_mic = QAction("Micrófono activado", self)
        self.act_mic.setCheckable(True)
        self.act_mic.setChecked(self.app.listening_enabled)
        self.act_mic.toggled.connect(self.app.set_listening)
        self.act_stop_all = QAction("Detener completamente la escucha", self)
        self.act_stop_all.triggered.connect(self._stop_listening_completely)
        m_listen.addAction(self.act_mic)
        m_listen.addAction(self.act_stop_all)
        m_listen.addSeparator()
        self.mode_actions: dict[str, QAction] = {}
        for key, label in MODE_LABELS.items():
            a = QAction(f"Modo {label}", self)
            a.setCheckable(True)
            a.triggered.connect(lambda _=False, k=key: self.app.set_mode(k))
            self.mode_actions[key] = a
            m_listen.addAction(a)

        self.m_view = mb.addMenu("&Ver")
        m_cfg = mb.addMenu("&Configuración")
        a = QAction("Preferencias…", self)
        a.setShortcut(QKeySequence("Ctrl+,"))
        a.triggered.connect(self.open_settings)
        m_cfg.addAction(a)
        m_help = mb.addMenu("A&yuda")
        a = QAction("Carpeta de datos", self)
        a.triggered.connect(self._open_data_dir)
        m_help.addAction(a)
        a = QAction("Acerca de", self)
        a.triggered.connect(lambda: QMessageBox.information(self, APP_NAME, f"{APP_NAME} {__version__}\n{self.app.hw.describe()}"))
        m_help.addAction(a)

    def _build_central(self) -> None:
        central = QWidget()
        lay = QVBoxLayout(central)
        lay.setContentsMargins(10, 8, 10, 8)
        strip = QFrame()
        strip.setObjectName("statusStrip")
        sl = QHBoxLayout(strip)
        sl.setContentsMargins(4, 0, 4, 0)
        self.st_obs = StatusLabel("OBS")
        self.st_rec = StatusLabel("Grabación")
        self.st_mic = StatusLabel("Micrófono")
        self.st_listen = StatusLabel("Escucha")
        self.st_model = StatusLabel("Modelo")
        self.st_ai = StatusLabel("IA")
        self.st_mode = StatusLabel("Modo")
        self.st_hotkey = StatusLabel("Hotkey")
        for w in (self.st_obs, self.st_rec, self.st_mic, self.st_listen, self.st_model, self.st_ai, self.st_mode,
                  self.st_hotkey):
            sl.addWidget(w)
        sl.addStretch(1)
        self.btn_session = QPushButton("Iniciar sesión manual")
        self.btn_session.setObjectName("primary")
        self.btn_session.clicked.connect(self.toggle_session)
        sl.addWidget(self.btn_session)
        lay.addWidget(strip)

        self.live = QLabel("En espera…")
        self.live.setObjectName("liveText")
        self.live.setWordWrap(True)
        self.live.setMinimumHeight(60)
        lay.addWidget(self.live)
        self.meter = LevelMeter()
        lay.addWidget(self.meter)
        self.live_hint = QLabel("")
        self.live_hint.setObjectName("liveHint")
        lay.addWidget(self.live_hint)

        split = QSplitter(Qt.Vertical)
        notes_box = QWidget()
        nl = QVBoxLayout(notes_box)
        nl.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        self.session_title = QLabel("Sin sesión")
        self.session_title.setObjectName("ctxBig")
        head.addWidget(self.session_title, 1)
        self.filter_marker = QComboBox()
        self.filter_marker.addItem("Todas las notas", "")
        for k, v in MARKERS.items():
            if k:
                self.filter_marker.addItem(v, k)
        self.filter_marker.addItem("🗣️ Otra voz", OTHER_VOICE_FILTER)
        self.filter_marker.currentIndexChanged.connect(lambda: self.refresh_notes())
        self.filter_text = QLineEdit()
        self.filter_text.setPlaceholderText("Buscar en la sesión…")
        self.filter_text.textChanged.connect(lambda: self.refresh_notes())
        head.addWidget(self.filter_text)
        head.addWidget(self.filter_marker)
        nl.addLayout(head)
        self.notes_list = QListWidget()
        self.notes_list.setWordWrap(True)
        self.notes_list.currentItemChanged.connect(self._note_selected)
        self.notes_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.notes_list.customContextMenuRequested.connect(self._notes_menu)
        nl.addWidget(self.notes_list)
        split.addWidget(notes_box)
        self.detail = NoteDetail()
        self.detail.changed.connect(self._note_changed)
        self.detail.delete_requested.connect(self._note_delete)
        self.detail.open_requested.connect(lambda n: self.open_recording(n))
        self.detail.copy_requested.connect(self._copy_timestamp)
        self.detail.transform_requested.connect(self._transform_requested)
        self.detail.reclassify_requested.connect(self._reclassify_requested)
        self.detail.related_selected.connect(self._related_selected)
        split.addWidget(self.detail)
        split.setSizes([400, 300])   # el detalle tiene pestañas: necesita algo más de alto
        lay.addWidget(split, 1)
        self.setCentralWidget(central)

    def _build_docks(self) -> None:
        # ---- sesiones -------------------------------------------------------------
        self.dock_sessions = QDockWidget("Sesiones", self)
        self.dock_sessions.setObjectName("dockSessions")
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(6, 6, 6, 6)
        self.sessions_game = QComboBox()
        self.sessions_game.addItem("Todos los juegos", "")
        self.sessions_game.currentIndexChanged.connect(lambda: self.refresh_sessions())
        l.addWidget(self.sessions_game)
        self.sessions_list = QListWidget()
        self.sessions_list.currentItemChanged.connect(self._session_selected)
        self.sessions_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.sessions_list.customContextMenuRequested.connect(self._sessions_menu)
        l.addWidget(self.sessions_list, 1)
        row = QHBoxLayout()
        b = QPushButton("Exportar")
        b.clicked.connect(self.export_session)
        row.addWidget(b)
        b = QPushButton("Informe")
        b.clicked.connect(self.open_session_report)
        row.addWidget(b)
        b = QPushButton("Chatbot")
        b.clicked.connect(self.open_chatbot)
        row.addWidget(b)
        b = QPushButton("Buscar")
        b.setToolTip("Buscar en las notas de todas las sesiones (Ctrl+Shift+F)")
        b.clicked.connect(lambda: self.open_search())
        row.addWidget(b)
        l.addLayout(row)
        self.dock_sessions.setWidget(w)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.dock_sessions)

        # ---- contexto -------------------------------------------------------------
        self.dock_context = QDockWidget("Contexto detectado", self)
        self.dock_context.setObjectName("dockContext")
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(8, 6, 8, 6)

        def heading(t: str) -> QLabel:
            h = QLabel(t.upper())
            h.setObjectName("heading")
            return h

        l.addWidget(heading("Contexto detectado"))
        self.ctx_main = QLabel("—")
        self.ctx_main.setObjectName("ctxBig")
        self.ctx_main.setWordWrap(True)
        l.addWidget(self.ctx_main)
        self.ctx_conf = QLabel("")
        self.ctx_conf.setObjectName("liveHint")
        l.addWidget(self.ctx_conf)
        l.addWidget(heading("Estado de la sesión"))
        self.ctx_state = QLabel("—")
        self.ctx_state.setWordWrap(True)
        l.addWidget(self.ctx_state)
        l.addWidget(heading("Contexto relacionado"))
        self.ctx_related = QLabel("—")
        self.ctx_related.setWordWrap(True)
        l.addWidget(self.ctx_related)
        l.addWidget(heading("Referencias detectadas"))
        self.ctx_refs = QLabel("—")
        self.ctx_refs.setWordWrap(True)
        l.addWidget(self.ctx_refs)
        l.addWidget(heading("Último comentario"))
        self.ctx_last = QLabel("—")
        self.ctx_last.setWordWrap(True)
        l.addWidget(self.ctx_last)
        row = QHBoxLayout()
        self.ctx_scope_btns: list[QPushButton] = []
        for key, label in SCOPE_BUTTONS[:3]:
            b = QPushButton(label)
            b.setCheckable(True)
            b.setProperty("scope", key)
            b.clicked.connect(lambda _=False, k=key: self._set_last_scope(k))
            row.addWidget(b)
            self.ctx_scope_btns.append(b)
        l.addLayout(row)
        l.addWidget(heading("Estadísticas de la sesión"))
        self.ctx_stats = QLabel("—")
        self.ctx_stats.setWordWrap(True)
        l.addWidget(self.ctx_stats)
        l.addStretch(1)
        self.dock_context.setWidget(w)
        self.addDockWidget(Qt.RightDockWidgetArea, self.dock_context)
        self.m_view.addAction(self.dock_sessions.toggleViewAction())
        self.m_view.addAction(self.dock_context.toggleViewAction())
        self.dock_sessions.visibilityChanged.connect(lambda v: self._save_dock_state())
        self.dock_context.visibilityChanged.connect(lambda v: self._save_dock_state())

    def _wire_tray(self) -> None:
        t = self.tray
        t.act_open.triggered.connect(self.show_from_tray)
        t.act_session.triggered.connect(self.toggle_session)
        t.act_listen.setChecked(self.app.listening_enabled)
        t.act_listen.toggled.connect(self.app.set_listening)
        t.act_settings.triggered.connect(self.open_settings)
        t.act_quit.triggered.connect(self.quit_app)
        t.act_mic.aboutToShow.connect(self._fill_mic_menu)

    def _fill_mic_menu(self) -> None:
        from cronista.audio.capture import list_input_devices
        menu = self.tray.act_mic
        menu.clear()
        seen = set()
        for d in list_input_devices():
            if d.name in seen:
                continue
            seen.add(d.name)
            a = menu.addAction(d.name)
            a.setCheckable(True)
            a.setChecked(bool(self.settings.audio.device) and d.name in self.settings.audio.device)
            a.triggered.connect(lambda _=False, name=d.name: self._set_mic(name))

    def _set_mic(self, name: str) -> None:
        new = self.settings.copy()
        new.audio.device = name
        self.app.apply_settings(new)

    def _apply_ui_settings(self) -> None:
        self.dock_context.setVisible(self.settings.ui.show_context_panel)
        self.dock_sessions.setVisible(self.settings.ui.show_sessions_panel)

    def _save_dock_state(self) -> None:
        try:
            if self._quitting or not self.isVisible():
                return   # al ocultar a la bandeja los docks también "desaparecen": no es una elección del usuario
            self.settings.ui.show_context_panel = self.dock_context.isVisible()
            self.settings.ui.show_sessions_panel = self.dock_sessions.isVisible()
        except RuntimeError:
            pass   # objeto Qt ya destruido durante el cierre

    # ---------------------------------------------------------------- eventos del bus
    @Slot(object)
    def _on_event(self, ev: Event) -> None:
        t = ev.topic
        if t == Topics.AUDIO_LEVEL:
            self.meter.set_level(ev.rms_db, ev.speech_prob, ev.get("speaking", False), ev.get("noise_db", -60))
            if ev.get("speaking") and self.tray.state == "idle":
                self.tray.set_state("listening")
            elif not ev.get("speaking") and self.tray.state == "listening":
                self.tray.set_state("idle" if not self.app.audio.listening else "listening")
            return
        if t == Topics.SESSION_TICK:
            self.st_rec.set(fmt_timestamp(ev.position_ms) + (" (pausa)" if ev.get("paused") else ""),
                            COLORS["warn"] if ev.get("paused") else COLORS["rec"])
            return
        if t == Topics.UTTERANCE_START:
            self._partial_utt = ev.utterance_id
            pos = self.app.sessions.position_at(float(ev.start_mono)) if self.app.sessions.active else None
            self.live.setText("🎙 …")
            self.live_hint.setText(f"Hablando desde {fmt_timestamp(pos)}" + (" (hotkey)" if ev.get("forced") else ""))
            self.tray.set_state("listening")
        elif t == Topics.STT_PARTIAL:
            if ev.utterance_id == self._partial_utt:
                self.live.setText(ev.text + " …")
        elif t == Topics.UTTERANCE_END:
            self.live_hint.setText("Transcribiendo…")
            self.tray.set_state("processing")
        elif t == Topics.STT_STATUS:
            state = ev.get("state")
            name = ev.get("model", "")
            if state == "loading":
                self.st_model.set(ev.get("detail", f"Cargando {name}…"), COLORS["busy"])
            elif state == "busy":
                self.st_model.set(f"{name} · procesando", COLORS["busy"])
                self.tray.set_state("processing")
            else:
                self.st_model.set(name, COLORS["ok"] if self.app.stt._main else COLORS["off"])
                if self.tray.state == "processing":
                    self.tray.set_state("idle")
        elif t == Topics.STT_FINAL:
            if ev.utterance_id == self._partial_utt:
                self.live.setText(ev.text or "(nada reconocido)")
                self.live_hint.setText("")
        elif t == Topics.STT_ERROR:
            self.statusBar().showMessage(f"Error de transcripción: {ev.message}", 8000)
            self.tray.set_state("error", 4000)
        elif t == Topics.NOTE_CREATED:
            n = ev.note
            self.tray.set_state("saved", 2500)
            if self.view_session_id == n["session_id"]:
                self._append_note_item(n, select=True)
            self._update_context_last(n)
            self._update_stats()
            self.statusBar().showMessage(f"Nota guardada en {fmt_timestamp(n.get('position_ms'))}", 4000)
        elif t == Topics.NOTE_UPDATED:
            self._update_note_item(ev.note)
        elif t == Topics.NOTE_DELETED:
            self.refresh_notes()
        elif t == Topics.NOTE_DISCARDED:
            reason = ev.get("reason")
            if reason == "other_voice":
                sc = fmt_score((ev.get("voice") or {}).get("score"))
                self.statusBar().showMessage(f"Frase descartada: no parece tu voz (similitud {sc})", 4000)
            elif reason not in ("too_short", "empty"):
                self.statusBar().showMessage(f"Descartado ({reason}): {ev.get('text', '')[:60]}", 4000)
            if self.tray.state == "processing":
                self.tray.set_state("idle")
        elif t == Topics.CONTEXT_UPDATED:
            self._update_context_state(ev.context)
        elif t in (Topics.SESSION_STARTED, Topics.SESSION_STOPPED, Topics.SESSION_UPDATED):
            self.refresh_sessions()
            self._refresh_status_all()
            if t == Topics.SESSION_STARTED:
                self.show_session(ev.session["id"])
                self.tray.notify("Sesión iniciada", ev.session["title"])
            elif t == Topics.SESSION_STOPPED:
                self.tray.notify("Sesión terminada", f"{ev.session['title']} · {fmt_timestamp(ev.session['duration_ms'])}")
                self.st_rec.set("Sin grabación", COLORS["off"])
                self.refresh_notes()
        elif t in (Topics.OBS_CONNECTED, Topics.OBS_DISCONNECTED, Topics.AUDIO_STARTED, Topics.AUDIO_STOPPED,
                   Topics.LISTENING_CHANGED, Topics.MODE_CHANGED, Topics.SETTINGS_CHANGED, Topics.OBS_RECORD_STARTED,
                   Topics.OBS_RECORD_STOPPED, Topics.OBS_RECORD_PAUSED, Topics.OBS_RECORD_RESUMED):
            self._refresh_status_all()
        elif t == Topics.AUDIO_ERROR:
            self.statusBar().showMessage(f"Audio: {ev.message}", 10000)
            self.tray.set_state("error")
        elif t == Topics.STATUS:
            self.statusBar().showMessage(ev.message, 8000)
            if ev.get("level") in ("warn", "error"):
                self.tray.set_state("warn" if ev.level == "warn" else "error", 5000)
        elif t == Topics.PERF_GAME_DETECTED:
            self.statusBar().showMessage(("Juego detectado: " + ev.get("title", "")) if ev.detected else
                                         "Juego ya no está en primer plano", 5000)
        elif t == Topics.AI_STATUS:
            self._apply_ai_status(ev.get("state", "off"), ev.get("detail") or "", ev.get("queue") or 0)
        elif t == Topics.AI_RESULT:
            self._on_ai_result(ev)
        elif t == Topics.AI_ERROR:
            self._on_ai_error(ev)
        elif t == Topics.RELATIONS_UPDATED:
            self._on_relations_updated(ev.get("note_id"))
        elif t == Topics.JOB_PROGRESS:
            self._on_job_progress(ev)
        elif t == Topics.JOB_DONE:
            self._on_job_done(ev)

    # ---------------------------------------------------------------- estado
    def _refresh_status_all(self) -> None:
        obs = self.app.obs
        if obs.connected:
            self.st_obs.set("Conectado", COLORS["ok"])
            self.st_obs.setToolTip(obs.version)
        else:
            self.st_obs.set("Desconectado", COLORS["off"])
            self.st_obs.setToolTip(obs.last_error)
        s = self.app.sessions
        if s.active:
            kind = "OBS" if s.current["mode"] == "obs" else "manual"
            self.st_rec.set(f"{fmt_timestamp(s.position_now())} · {kind}", COLORS["warn"] if s.paused else COLORS["rec"])
            self.btn_session.setText("Detener sesión")
            self.btn_session.setObjectName("danger")
        else:
            self.st_rec.set("Sin grabación", COLORS["off"])
            self.btn_session.setText("Iniciar sesión manual")
            self.btn_session.setObjectName("primary")
        self.btn_session.style().unpolish(self.btn_session)
        self.btn_session.style().polish(self.btn_session)
        self.act_toggle_session.setText(self.btn_session.text())
        self.tray.set_session_active(s.active)
        self.tray.set_recording(s.active)
        mic_on = self.app.listening_enabled
        self.st_mic.set("ON" if mic_on else "OFF", COLORS["ok"] if mic_on else COLORS["off"])
        self.act_mic.blockSignals(True)
        self.act_mic.setChecked(mic_on)
        self.act_mic.blockSignals(False)
        self.tray.act_listen.blockSignals(True)
        self.tray.act_listen.setChecked(mic_on)
        self.tray.act_listen.blockSignals(False)
        if self.app.audio.listening:
            self.st_listen.set("Activa", COLORS["ok"])
            self.st_listen.setToolTip(self.app.audio.device_label)
            if self.tray.state == "idle":
                self.tray.set_state("listening")
        else:
            why = "Apagado" if not mic_on else "Esperando sesión" if not self.settings.general.listen_without_session else "Inactiva"
            self.st_listen.set(why, COLORS["off"])
            self.tray.set_state("idle")
        self.st_model.set(self.app.stt.live_model_name, COLORS["ok"] if self.app.stt._main else COLORS["off"])
        mode = self.settings.hotkey.mode
        self.st_mode.set(MODE_LABELS.get(mode, mode))
        for k, a in self.mode_actions.items():
            a.setChecked(k == mode)
        hk = self.app.hotkey
        self.st_hotkey.set(self.settings.hotkey.note_key.upper(), COLORS["ok"] if hk.active else COLORS["err"])
        self.st_hotkey.setToolTip(hk.error or ("Mantener pulsado para capturar" if not self.settings.hotkey.toggle_mode else "Pulsar para empezar/terminar"))

    # ---------------------------------------------------------------- sesiones
    def refresh_sessions(self) -> None:
        games = self.app.store.list_games()
        cur = self.sessions_game.currentData()
        self.sessions_game.blockSignals(True)
        self.sessions_game.clear()
        self.sessions_game.addItem("Todos los juegos", "")
        for g in games:
            self.sessions_game.addItem(g, g)
        idx = self.sessions_game.findData(cur or "")
        self.sessions_game.setCurrentIndex(max(0, idx))
        self.sessions_game.blockSignals(False)
        self.sessions_list.blockSignals(True)
        self.sessions_list.clear()
        for s in self.app.store.list_sessions(game=self.sessions_game.currentData() or None):
            live = " ● " if self.app.sessions.current and self.app.sessions.current["id"] == s["id"] else ""
            it = QListWidgetItem(f"{live}{s['game']}\n{(s['started_at'] or '')[:16].replace('T', ' ')} · "
                                 f"{fmt_timestamp(s['duration_ms'])} · {s.get('notes_count', 0)} notas")
            it.setData(Qt.UserRole, s["id"])
            self.sessions_list.addItem(it)
            if s["id"] == self.view_session_id:
                self.sessions_list.setCurrentItem(it)
        self.sessions_list.blockSignals(False)

    def _session_selected(self, cur: QListWidgetItem | None, prev=None) -> None:
        if cur is not None:
            self.show_session(cur.data(Qt.UserRole))

    def show_session(self, session_id: int) -> None:
        self.view_session_id = session_id
        s = self.app.store.get_session(session_id)
        if not s:
            return
        self.session_title.setText(f"{s['game']} — {(s['started_at'] or '')[:16].replace('T', ' ')} · {fmt_timestamp(s['duration_ms'])}")
        self.session_title.setToolTip(s.get("obs_path") or "Sin archivo de grabación asociado")
        for i in range(self.sessions_list.count()):
            it = self.sessions_list.item(i)
            if it.data(Qt.UserRole) == session_id and not it.isSelected():
                self.sessions_list.blockSignals(True)
                self.sessions_list.setCurrentItem(it)
                self.sessions_list.blockSignals(False)
        self.refresh_notes()
        self._update_stats()

    def _sessions_menu(self, pos) -> None:
        it = self.sessions_list.itemAt(pos)
        if not it:
            return
        sid = it.data(Qt.UserRole)
        m = QMenu(self)
        m.addAction("Abrir grabación", lambda: self.open_recording(None))
        m.addAction("Asociar archivo de grabación…", lambda: self._link_recording(sid))
        m.addAction("Cambiar juego…", self.rename_game)
        m.addSeparator()
        m.addAction("Eliminar sesión", lambda: self._delete_session(sid))
        m.exec(self.sessions_list.mapToGlobal(pos))

    def _delete_session(self, sid: int) -> None:
        if self.app.sessions.current and self.app.sessions.current["id"] == sid:
            QMessageBox.information(self, APP_NAME, "Detén la sesión antes de eliminarla.")
            return
        if QMessageBox.question(self, APP_NAME, "¿Eliminar la sesión y todas sus notas?") == QMessageBox.Yes:
            self.app.store.delete_session(sid)
            if self.view_session_id == sid:
                self.view_session_id = None
                self.notes_list.clear()
                self.detail.set_note(None)
            self.refresh_sessions()

    def _link_recording(self, sid: int) -> None:
        start = self.app.obs.record_dir or ""
        path, _ = QFileDialog.getOpenFileName(self, "Archivo de grabación", start, "Vídeo (*.mkv *.mp4 *.mov *.flv *.ts);;Todos (*)")
        if path:
            self.app.store.update_session(sid, obs_path=path)
            if self.app.sessions.current and self.app.sessions.current["id"] == sid:
                self.app.sessions.current["obs_path"] = path
            self.show_session(sid)

    def toggle_session(self) -> None:
        if self.app.sessions.active:
            if self.app.sessions.current["mode"] == "obs" and self.app.obs.recording:
                if QMessageBox.question(self, APP_NAME, "OBS sigue grabando. ¿Detener la sesión de todos modos?") != QMessageBox.Yes:
                    return
            self.app.stop_session()
        else:
            game, ok = QInputDialog.getText(self, "Sesión manual", "Juego:",
                                            text=self.app.sessions.game_hint or self.settings.general.default_game)
            if ok:
                self.app.start_manual_session(game.strip())

    def rename_game(self) -> None:
        sid = self.view_session_id
        if sid is None:
            return
        s = self.app.store.get_session(sid)
        name, ok = QInputDialog.getText(self, "Juego", "Nombre del juego:", text=s["game"] if s else "")
        if ok and name.strip():
            if self.app.sessions.current and self.app.sessions.current["id"] == sid:
                self.app.sessions.set_game(name.strip())
            else:
                self.app.store.update_session(sid, game=name.strip())
            self.refresh_sessions()
            self.show_session(sid)

    # ---------------------------------------------------------------- notas
    def _note_label(self, n: dict) -> str:
        icon = MARKER_ICON.get(n.get("marker") or "", "")
        tags = []
        if n.get("functions"):
            tags.append("/".join(n["functions"][:2]))
        if n.get("topics"):
            tags.append("/".join(n["topics"][:2]))
        if n.get("scope") and n["scope"] != "current":
            tags.append({"other": "otro juego", "general": "general", "personal": "personal"}.get(n["scope"], n["scope"]))
        meta = ("   ·   " + "  ·  ".join(tags)) if tags else ""
        ctx = n.get("context") or {}
        flags = ((" ✨" if ctx.get("source") == "llm" else "") + (" 🔒" if ctx.get("user_locked") else "")
                 + (" 🗣️" if is_other_voice(n) else ""))
        return f"{fmt_timestamp(n.get('position_ms'))}  {icon} {n.get('text') or ''}{meta}{flags}"

    def refresh_notes(self) -> None:
        self.notes_list.blockSignals(True)
        self.notes_list.clear()
        self.detail.set_note(None)
        if self.view_session_id is not None:
            marker = self.filter_marker.currentData() or None
            other = marker == OTHER_VOICE_FILTER
            notes = self.app.store.list_notes(self.view_session_id, marker=None if other else marker,
                                              text_filter=self.filter_text.text().strip() or None)
            if other:   # filtro de voz: no es un marcador, se filtra en Python por stt_info.voice
                notes = [n for n in notes if is_other_voice(n)]
            for n in notes:
                self._append_note_item(n)
        self.notes_list.blockSignals(False)

    def _append_note_item(self, n: dict, select: bool = False) -> None:
        it = QListWidgetItem(self._note_label(n))
        it.setData(Qt.UserRole, n["id"])
        it.setToolTip(n.get("text_raw") or "")
        self.notes_list.addItem(it)
        if select:
            self.notes_list.setCurrentItem(it)
            self.notes_list.scrollToItem(it)

    def _update_note_item(self, n: dict) -> None:
        for i in range(self.notes_list.count()):
            it = self.notes_list.item(i)
            if it.data(Qt.UserRole) == n["id"]:
                it.setText(self._note_label(n))
                if self.notes_list.currentItem() is it:
                    self.detail.set_note(n)
                break
        self._update_stats()

    def _note_selected(self, cur: QListWidgetItem | None, prev=None) -> None:
        if cur is None:
            self.detail.set_note(None)
            return
        nid = cur.data(Qt.UserRole)
        self.detail.set_note(self.app.store.get_note(nid))
        self.detail.set_related(self.app.store.related_notes(nid))
        self.detail.set_clips(self.app.store.clips_for_note(nid))

    def _note_changed(self, note_id: int, fields: dict, learn: str) -> None:
        self.app.notes.update(note_id, learn_field=learn or None, **fields)

    def _note_delete(self, note_id: int) -> None:
        self.app.notes.delete(note_id)

    def _notes_menu(self, pos) -> None:
        it = self.notes_list.itemAt(pos)
        if not it:
            return
        n = self.app.store.get_note(it.data(Qt.UserRole))
        if not n:
            return
        m = QMenu(self)
        m.addAction("Copiar timestamp", lambda: self._copy_timestamp(n))
        m.addAction("Copiar texto", lambda: QGuiApplication.clipboard().setText(n.get("text") or ""))
        m.addAction("Abrir en grabación", lambda: self.open_recording(n))
        m.addAction("Exportar clip de esta nota…", lambda: self._export_clip_for_note(n))
        m.addAction("Notas parecidas…", lambda: self.open_search(n["id"]))
        mm = m.addMenu("Marcador")
        for k, v in MARKERS.items():
            mm.addAction(v, lambda _=False, k=k: self.app.notes.update(n["id"], marker=k))
        m.addSeparator()
        m.addAction("Eliminar", lambda: self._note_delete(n["id"]))
        m.exec(self.notes_list.mapToGlobal(pos))

    def _copy_timestamp(self, n: dict) -> None:
        QGuiApplication.clipboard().setText(fmt_timestamp(n.get("position_ms")))
        self.statusBar().showMessage("Timestamp copiado", 2000)

    def add_manual_note(self) -> None:
        if not self.app.sessions.active:
            QMessageBox.information(self, APP_NAME, "Inicia una sesión para añadir notas.")
            return
        text, ok = QInputDialog.getMultiLineText(self, "Nota escrita", f"Nota en {fmt_timestamp(self.app.sessions.position_now())}:")
        if ok and text.strip():
            self.app.notes.create_manual(text.strip())

    def open_recording(self, note: dict | None) -> None:
        sid = note["session_id"] if note else self.view_session_id
        if sid is None:
            return
        s = self.app.store.get_session(sid)
        path = (s or {}).get("obs_path")
        if not path:
            QMessageBox.information(self, APP_NAME, "Esta sesión no tiene archivo de grabación asociado.\n"
                                    "Clic derecho en la sesión → Asociar archivo de grabación.")
            return
        try:
            msg = open_at(path, note.get("position_ms") if note else None, self.settings.general.player_command)
            self.statusBar().showMessage(msg, 5000)
        except Exception as e:
            QMessageBox.warning(self, APP_NAME, str(e))

    # ---------------------------------------------------------------- contexto
    def _update_context_last(self, n: dict) -> None:
        parts = []
        if n.get("scope"):
            parts.append({"current": "Videojuego actual", "other": "Otro videojuego", "general": "Tema general",
                          "personal": "Pensamiento personal"}.get(n["scope"], n["scope"]))
        parts += [f.capitalize() for f in (n.get("functions") or [])]
        parts += [t.capitalize() for t in (n.get("topics") or [])]
        self.ctx_main.setText("\n".join("✓ " + p for p in parts) if parts else "Contexto ambiguo")
        c = int(float(n.get("confidence") or 0) * 100)
        self.ctx_conf.setText(f"Confianza: {c}%" + ("  ·  contexto ambiguo" if n.get("ambiguous") else ""))
        self.ctx_last.setText(f"[{fmt_timestamp(n.get('position_ms'))}] {n.get('text', '')[:220]}")
        self._last_note_id = n["id"]
        for b in self.ctx_scope_btns:
            b.setChecked(b.property("scope") == n.get("scope"))
        refs = [r.get("name") if isinstance(r, dict) else str(r) for r in (n.get("refs") or [])]
        if refs:
            self.ctx_refs.setText("\n".join("• " + r for r in refs))

    def _update_context_state(self, st: dict) -> None:
        lines = [f"Juego: {st.get('game') or '—'}", f"Tema: {st.get('topic') or '—'}"]
        if st.get("subtopic"):
            lines.append(f"Subtema: {st['subtopic']}")
        if st.get("chapter"):
            lines.append(f"Capítulo: {st['chapter']}")
        if st.get("section"):
            lines.append(f"Sección: {st['section']}")
        if st.get("last_function"):
            lines.append(f"Última actividad: {st['last_function']}")
        self.ctx_state.setText("\n".join(lines))
        self.ctx_related.setText("\n".join("• " + r for r in st.get("related") or []) or "—")
        self.ctx_refs.setText("\n".join("• " + r for r in st.get("references") or []) or "—")

    def _set_last_scope(self, scope: str) -> None:
        nid = getattr(self, "_last_note_id", None)
        if nid:
            self.app.notes.update(nid, learn_field="scope", scope=scope)

    def _update_stats(self) -> None:
        if self.view_session_id is None:
            self.ctx_stats.setText("—")
            return
        st = self.app.store.session_stats(self.view_session_id)
        lines = [f"{st['notes']} notas"]
        for title, key in (("Temas", "topics"), ("Funciones", "functions"), ("Ámbito", "scope")):
            items = sorted(st[key].items(), key=lambda kv: -kv[1])[:6]
            if items:
                label = (lambda k: SCOPE_STAT_LABELS.get(k, k)) if key == "scope" else (lambda k: k)
                lines.append(f"{title}: " + ", ".join(f"{label(k)} {v}" for k, v in items))
        if st["markers"]:
            lines.append("Marcadores: " + ", ".join(f"{MARKER_ICON.get(k, k)} {v}" for k, v in st["markers"].items()))
        # grupos de comentarios relacionados (solo al actualizar estadísticas, no en cada evento de audio)
        notes = self.app.store.list_notes(self.view_session_id)
        rel = self.app.store.session_relations(self.view_session_id)   # una sola consulta
        clusters = relations.topic_clusters(notes, rel)[:3]
        if clusters:
            lines.append("Comentarios relacionados:")
            for c in clusters:
                label = " / ".join(x for x in (c["topic"], c["subtopic"]) if x) or "sin tema"
                span = ""
                if c["first_ms"] is not None and c["last_ms"] is not None:
                    span = f" ({fmt_timestamp(c['first_ms'])}–{fmt_timestamp(c['last_ms'])})"
                lines.append(f"• {label}: {c['count']} comentarios relacionados{span}")
        self.ctx_stats.setText("\n".join(lines))

    # ---------------------------------------------------------------- IA local (fase 2)
    def _apply_ai_status(self, state: str, detail: str, queue: int) -> None:
        if state == "off":
            text, color, tip = "Desactivada", COLORS["off"], detail
        elif state == "unavailable":
            text, color, tip = "No instalada", COLORS["warn"], detail
        elif state == "idle":
            text, color, tip = ("Lista (en reposo)" if "descargado" in detail else "Lista"), COLORS["ok"], detail
        elif state == "loading":
            text, color, tip = "Cargando…", COLORS["busy"], detail
        elif state == "busy":
            text, color, tip = "Procesando", COLORS["busy"], detail
        elif state == "deferred":
            text, color = f"En espera ({queue})", COLORS["warn"]
            tip = "Esperando a que el juego no esté en primer plano"
        else:
            text, color, tip = state, COLORS["off"], detail
        self.st_ai.set(text, color)
        self.st_ai.setToolTip(tip)

    def _on_ai_result(self, ev) -> None:
        kind = ev.get("kind")
        if kind == "transform":
            nid = ev.get("note_id")
            mode = (ev.get("data") or {}).get("mode", "")
            if self._selected_note_id() == nid:
                self.detail.set_note(self.app.store.get_note(nid))
            self.detail.transform_finished(nid, mode, True)
            self.statusBar().showMessage("Transformación de IA lista", 4000)
        elif kind == "classify":
            # NOTE_UPDATED (source=ai) ya refresca la lista, el detalle y las estadísticas
            self.statusBar().showMessage("Nota clasificada con IA local", 3000)
        elif kind == "summary":
            sid = ev.get("session_id")
            if sid is not None and sid == self.view_session_id:
                self.statusBar().showMessage("Análisis de la sesión listo", 6000)
                self._open_session_report(sid)

    def _on_ai_error(self, ev) -> None:
        kind = ev.get("kind")
        msg = ev.get("message") or "error desconocido"
        self.statusBar().showMessage(f"IA: {msg}", 8000)
        if kind == "transform":
            self.detail.transform_finished(ev.get("note_id"), "", False)
        if kind in ("summary", "transform"):
            QMessageBox.warning(self, APP_NAME, msg)

    def _on_relations_updated(self, note_id) -> None:
        if note_id is None:
            return
        if self._selected_note_id() == note_id:
            self.detail.set_related(self.app.store.related_notes(note_id))
        note = self.app.store.get_note(note_id)
        if note and note["session_id"] == self.view_session_id:
            self._update_stats()

    def _selected_note_id(self) -> int | None:
        cur = self.notes_list.currentItem()
        return cur.data(Qt.UserRole) if cur is not None else None

    def _transform_requested(self, note_id: int, mode: str) -> None:
        self.app.ai.request_transform(note_id, mode)
        self.statusBar().showMessage(f"{TRANSFORM_MODES[mode]['title']} con IA local…", 3000)

    def _reclassify_requested(self, note_id: int) -> None:
        self.app.ai.request_classify(note_id)
        self.statusBar().showMessage("Reclasificando la nota con IA local…", 3000)

    def _related_selected(self, note_id: int) -> None:
        if not self._select_note_in_list(note_id):
            self.statusBar().showMessage("Esa nota no está en la sesión visible con los filtros actuales", 5000)

    def _select_note_in_list(self, note_id: int) -> bool:
        for i in range(self.notes_list.count()):
            it = self.notes_list.item(i)
            if it.data(Qt.UserRole) == note_id:
                self.notes_list.setCurrentItem(it)      # dispara _note_selected
                self.notes_list.scrollToItem(it)
                return True
        return False

    def _open_session_report(self, sid: int) -> None:
        try:
            from cronista.ui.dialogs import SessionReportDialog
        except ImportError:
            log.exception("SessionReportDialog no está disponible")
            QMessageBox.warning(self, APP_NAME, "El informe de la sesión no está disponible en esta versión.")
            return
        SessionReportDialog(self, self.app, sid).exec()

    def open_session_report(self) -> None:
        sid = self.view_session_id
        if sid is None:
            QMessageBox.information(self, APP_NAME, "Selecciona una sesión.")
            return
        self._open_session_report(sid)

    def analyze_session_ai(self) -> None:
        sid = self.view_session_id
        if sid is None:
            QMessageBox.information(self, APP_NAME, "Selecciona una sesión.")
            return
        ok, why = self.app.ai.availability()
        if not ok:
            QMessageBox.information(self, APP_NAME, f"La IA local no está disponible.\n\n{why}\n\n"
                                    "Puedes configurarla en Configuración → IA local.")
            return
        self.app.ai.request_summary(sid, "local")
        self.statusBar().showMessage("Analizando la sesión con IA local…", 6000)

    def reclassify_session_ai(self) -> None:
        sid = self.view_session_id
        if sid is None:
            QMessageBox.information(self, APP_NAME, "Selecciona una sesión.")
            return
        ok, why = self.app.ai.availability()
        if not ok:
            QMessageBox.information(self, APP_NAME, f"La IA local no está disponible.\n\n{why}\n\n"
                                    "Puedes configurarla en Configuración → IA local.")
            return
        n = self.app.ai.reclassify_session(sid)
        self.statusBar().showMessage(f"Reclasificando {n} notas con IA local en segundo plano…", 6000)

    # ---------------------------------------------------------------- buscador (fase 3)
    def open_search(self, note_id: int | None = None) -> None:
        """Buscador de notas; con `note_id` abre directamente «Notas parecidas» a esa nota."""
        from cronista.ui.search_dialog import SearchDialog
        dlg = SearchDialog(self, self.app, on_open_note=self._open_note_from_search)
        if note_id is not None:
            dlg.show_similar(int(note_id))
        dlg.exec()

    def _open_note_from_search(self, session_id: int, note_id: int) -> None:
        """Muestra la sesión y selecciona la nota. Si los filtros actuales la ocultan, se quitan."""
        if not self.isVisible():
            self.show_from_tray()
        else:
            self.raise_()
            self.activateWindow()
        self.show_session(session_id)
        if self._select_note_in_list(note_id):
            return
        self.filter_marker.setCurrentIndex(0)      # "Todas las notas"
        self.filter_text.clear()
        if not self._select_note_in_list(note_id):
            self.statusBar().showMessage("Esa nota no está en la sesión (¿se eliminó?)", 5000)

    # ---------------------------------------------------------------- editor, clips, momentos y guiones (fase 3)
    def _require_session(self) -> int | None:
        if self.view_session_id is None:
            QMessageBox.information(self, APP_NAME, "Selecciona una sesión.")
            return None
        return self.view_session_id

    def _dialog_class(self, module: str, name: str):
        """Clase de un diálogo de otro módulo; si aún no existe, avisa y devuelve None."""
        try:
            return getattr(importlib.import_module(module), name)
        except (ImportError, AttributeError):
            log.warning("Diálogo no disponible: %s.%s", module, name, exc_info=True)
            QMessageBox.information(self, APP_NAME, "Esta función todavía no está disponible en esta versión.")
            return None

    def send_to_reclaimer(self) -> None:
        sid = self._require_session()
        if sid is None:
            return
        s = self.app.store.get_session(sid) or {}
        obs = s.get("obs_path") or ""
        if not obs or not Path(obs).is_file():
            QMessageBox.information(
                self, APP_NAME,
                "Esta sesión no tiene un archivo de grabación existente, y Reclaimer necesita el vídeo.\n\n"
                "Clic derecho en la sesión → Asociar archivo de grabación, y vuelve a intentarlo.")
            return
        from cronista.editor import reclaimer
        if reclaimer.find_reclaimer(self.settings) is None:
            QMessageBox.warning(self, APP_NAME,
                                "No se encontró Reclaimer.\n\nIndica su carpeta en Configuración → Editor y clips.")
            return
        open_after = QMessageBox.question(self, APP_NAME, "¿Abrir Reclaimer al terminar?") == QMessageBox.Yes
        self.app.export_reclaimer(sid, open_after=open_after)
        self.statusBar().showMessage("Creando el proyecto de Reclaimer…", 5000)

    def export_clips_dialog(self) -> None:
        sid = self._require_session()
        if sid is None:
            return
        self._open_clips_dialog(sid, None)

    def _export_clip_for_note(self, n: dict) -> None:
        self._open_clips_dialog(int(n["session_id"]), [int(n["id"])])

    def _open_clips_dialog(self, session_id: int, note_ids: list[int] | None) -> None:
        cls = self._dialog_class("cronista.ui.clips_dialog", "ClipsDialog")
        if cls is None:
            return
        cls(self, self.app, session_id, note_ids).exec()
        self._refresh_selected_clips()

    def detect_moments(self) -> None:
        sid = self._require_session()
        if sid is None:
            return
        s = self.app.store.get_session(sid) or {}
        obs = s.get("obs_path") or ""
        has_recording = bool(obs) and Path(obs).is_file()
        use_media = False
        if has_recording:
            box = QMessageBox(self)
            box.setWindowTitle(APP_NAME)
            box.setText("¿Analizar también la imagen y el sonido de la grabación?")
            box.setInformativeText("Recomendado: tarda unos 60 s por cada hora de grabación.\n"
                                   "«Solo mis comentarios» usa únicamente tus notas, sin abrir el vídeo.")
            b_media = box.addButton("Imagen y sonido (recomendado)", QMessageBox.AcceptRole)
            b_notes = box.addButton("Solo mis comentarios", QMessageBox.ActionRole)
            box.addButton(QMessageBox.Cancel)
            box.exec()
            clicked = box.clickedButton()
            if clicked == b_media:
                use_media = True
            elif clicked == b_notes:
                use_media = False
            else:
                return
        self._moments_session = sid
        self.app.analyze_moments(sid, use_media=use_media)
        extra = "" if has_recording else " (sin grabación asociada: solo tus comentarios)"
        self.statusBar().showMessage("Detectando momentos destacados…" + extra, 6000)

    def generate_script_dialog(self) -> None:
        game = None
        if self.view_session_id is not None:
            s = self.app.store.get_session(self.view_session_id) or {}
            game = s.get("game") or None
        cls = self._dialog_class("cronista.ui.scripts_dialog", "ScriptsDialog")
        if cls is None:
            return
        cls(self, self.app, game).exec()

    def _refresh_selected_clips(self) -> None:
        nid = self._selected_note_id()
        if nid is not None:
            self.detail.set_clips(self.app.store.clips_for_note(nid))

    # ---------------------------------------------------------------- barra de tareas en segundo plano
    def _build_job_bar(self) -> None:
        """Progreso de las tareas largas (JobRunner) en la barra de estado; oculta si no hay ninguna."""
        box = QWidget()
        hl = QHBoxLayout(box)
        hl.setContentsMargins(0, 0, 6, 0)
        self.job_label = QLabel("")
        self.job_progress = QProgressBar()
        self.job_progress.setRange(0, 100)
        self.job_progress.setTextVisible(False)
        self.job_progress.setMaximumWidth(180)
        self.job_progress.setMaximumHeight(14)
        self.job_cancel = QPushButton("Cancelar")
        self.job_cancel.setMaximumHeight(22)
        self.job_cancel.clicked.connect(lambda: self._cancel_job())
        hl.addWidget(self.job_label)
        hl.addWidget(self.job_progress)
        hl.addWidget(self.job_cancel)
        box.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Maximum)   # que no se estire en la barra de estado
        self._job_box = box
        self._current_job: str | None = None
        box.setVisible(False)
        self.statusBar().addPermanentWidget(box)

    def _cancel_job(self) -> None:
        if self._current_job:
            self.app.jobs.cancel(self._current_job)
        else:
            self.app.jobs.cancel_all()
        self.job_label.setText("Cancelando…")
        self.job_cancel.setEnabled(False)

    def _on_job_progress(self, ev: Event) -> None:
        job = str(ev.get("job") or "")
        self._current_job = job
        text = str(ev.get("text") or "")
        label = JOB_LABELS.get(job, job)
        self.job_label.setText(f"{label}: {text}" if text else label)
        self.job_progress.setValue(int(round(float(ev.get("fraction") or 0) * 100)))
        self.job_cancel.setEnabled(True)
        self._job_box.setVisible(True)

    def _on_job_done(self, ev: Event) -> None:
        job = str(ev.get("job") or "")
        ok = bool(ev.get("ok"))
        message = str(ev.get("message") or "")
        result = ev.get("result") or {}
        self._current_job = None
        if self.app.jobs.pending() == 0:
            self._job_box.setVisible(False)       # si quedan tareas en cola, la barra sigue a la vista
        label = JOB_LABELS.get(job, job)
        if not ok:
            if message == "Cancelado":
                self.statusBar().showMessage(f"{label}: cancelado", 4000)
            else:
                QMessageBox.warning(self, APP_NAME, f"{label}: {message}")
            return
        if job == "reclaimer":
            self._reclaimer_done(result, message)
        elif job == "clips":
            self._clips_done(result, message)
        elif job == "moments":
            self.statusBar().showMessage(message, 8000)
            if self._moments_session is not None and self._moments_session == self.view_session_id:
                self._update_stats()
        elif job == "script":
            self._script_done(result, message)
        elif job == "index":
            self.statusBar().showMessage(message, 5000)
        else:
            self.statusBar().showMessage(message or f"{label}: listo", 5000)

    def _reclaimer_done(self, result: dict, message: str) -> None:
        path = str(result.get("path") or "")
        if not path:
            self.statusBar().showMessage(message, 6000)
            return
        opened = bool(result.get("opened"))
        box = QMessageBox(self)
        box.setWindowTitle(APP_NAME)
        box.setText(f"Proyecto de Reclaimer creado:\n{path}"
                    + ("\n\nReclaimer se ha abierto con el proyecto." if opened else ""))
        b_open = None if opened else box.addButton("Abrir en Reclaimer", QMessageBox.AcceptRole)
        b_dir = box.addButton("Abrir carpeta", QMessageBox.ActionRole)
        box.addButton("Cerrar", QMessageBox.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if b_open is not None and clicked == b_open:
            try:
                from cronista.editor import reclaimer
                reclaimer.open_in_reclaimer(Path(path), self.settings)
            except Exception as e:
                log.exception("No se pudo abrir Reclaimer")
                QMessageBox.warning(self, APP_NAME, f"No se pudo abrir Reclaimer:\n{e}")
        elif clicked == b_dir:
            os.startfile(str(Path(path).parent))  # type: ignore[attr-defined]

    def _clips_done(self, result: dict, message: str) -> None:
        self._refresh_selected_clips()
        folder = str(result.get("folder") or "")
        if not folder:
            QMessageBox.information(self, APP_NAME, message or "No se exportó ningún clip.")
            return
        box = QMessageBox(self)
        box.setWindowTitle(APP_NAME)
        box.setText(message or "Clips exportados.")
        b_dir = box.addButton("Abrir carpeta", QMessageBox.ActionRole)
        box.addButton("Cerrar", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() == b_dir:
            os.startfile(folder)  # type: ignore[attr-defined]

    def _script_done(self, result: dict, message: str) -> None:
        self.statusBar().showMessage(message or "Guion listo", 6000)
        cls = self._dialog_class("cronista.ui.scripts_dialog", "ScriptsDialog")
        if cls is not None:
            cls(self, self.app).exec()

    # ---------------------------------------------------------------- acciones
    def export_session(self) -> None:
        sid = self.view_session_id
        if sid is None:
            return
        s = self.app.store.get_session(sid)
        notes = self.app.store.list_notes(sid)
        from PySide6.QtWidgets import QDialog
        from cronista.ui.dialogs import ExportDialog
        dlg = ExportDialog(self, s, notes, self.settings)
        dlg.exec()

    def open_chatbot(self) -> None:
        sid = self.view_session_id
        if sid is None:
            QMessageBox.information(self, APP_NAME, "Selecciona una sesión.")
            return
        from cronista.ui.dialogs import ChatbotDialog
        s = self.app.store.get_session(sid)
        notes = self.app.store.list_notes(sid)
        dlg = ChatbotDialog(self, self.app, s, notes)
        dlg.exec()

    def open_settings(self) -> None:
        from cronista.ui.settings_dialog import SettingsDialog
        dlg = SettingsDialog(self, self.app)
        if dlg.exec():
            self.app.apply_settings(dlg.result_settings())
            self._apply_ui_settings()
            self._refresh_status_all()

    def _stop_listening_completely(self) -> None:
        self.app.stop_listening_completely()
        self.statusBar().showMessage("Escucha detenida: el micrófono está cerrado y los modelos descargados.", 6000)

    def _open_data_dir(self) -> None:
        import os
        from cronista.core.paths import data_dir
        os.startfile(str(data_dir()))  # type: ignore[attr-defined]

    def show_from_tray(self) -> None:
        self.showNormal()
        self.activateWindow()
        self.raise_()

    def closeEvent(self, ev) -> None:
        if self._quitting or not self.settings.ui.close_to_tray:
            self.quit_app()
            ev.accept()
            return
        ev.ignore()
        self.hide()
        self.tray.notify(APP_NAME, "Sigue en segundo plano. Doble clic en el icono para abrir.", 2500)

    def quit_app(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        self._save_dock_state()
        self.tray.hide()
        try:
            self.app.shutdown()
        except Exception:
            log.exception("Error al cerrar")
        self.qapp.quit()

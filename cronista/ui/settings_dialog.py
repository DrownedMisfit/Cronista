"""Diálogo de configuración con pestañas: micrófono, OBS, transcripción, hotkey,
rendimiento, general/privacidad, IA/chatbot."""
from __future__ import annotations

import dataclasses
import logging
import threading
import time
from pathlib import Path
from datetime import datetime

from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
                               QSpinBox, QTabWidget, QVBoxLayout, QWidget, QFileDialog)

from cronista import APP_NAME
from cronista.ai import llm_setup
from cronista.ai.local_llm import LocalLLM, LLMUnavailable
from cronista.ai.providers import PROVIDERS
from cronista.audio.capture import list_input_devices
from cronista.core.settings import LLMSettings, Settings
from cronista.editor.reclaimer import find_reclaimer
from cronista.obs.client import enable_obs_websocket_server, read_obs_websocket_config, obs_running
from cronista.stt.engine import MODEL_CHOICES, MODEL_SIZES_MB, model_is_downloaded
from cronista.ui.theme import COLORS

log = logging.getLogger(__name__)


def _fmt_num(x: float, nd: int = 1) -> str:
    """Número con coma decimal (estilo español)."""
    return f"{x:.{nd}f}".replace(".", ",")


class _Signals(QObject):
    mic_progress = Signal(float, float)
    mic_done = Signal(dict)
    dl_done = Signal(str, str)
    hk_done = Signal(str)


class _LLMSignals(QObject):
    """Señales de los hilos de descarga y prueba de la IA local (se emiten desde hilos secundarios)."""
    progress = Signal(str)          # texto de progreso de la descarga
    finished = Signal(str)          # error ("" = correcto)
    test_result = Signal(str, bool)  # texto del resultado, ok


class _ParakeetSignals(QObject):
    """Señales de la descarga de Parakeet (se emiten desde un hilo secundario)."""
    progress = Signal(str)          # texto de progreso
    finished = Signal(str)          # error ("" = correcto)


class SettingsDialog(QDialog):
    def __init__(self, parent, app) -> None:
        super().__init__(parent)
        self.app = app
        self.s: Settings = app.settings.copy()
        self.hw = app.hw
        self.setWindowTitle("Configuración")
        self.resize(760, 620)
        self.sig = _Signals()
        self._llm_sig = _LLMSignals()
        self._parakeet_sig = _ParakeetSignals()
        lay = QVBoxLayout(self)
        self.tabs = QTabWidget()
        lay.addWidget(self.tabs)
        self._tab_audio()
        self._tab_obs()
        self._tab_stt()
        self._tab_llm()
        self._tab_hotkey()
        self._tab_perf()
        self._tab_general()
        self._tab_editor()
        self._tab_ai()
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._compact_texts()

    def _compact_texts(self, width: int = 78) -> None:
        """Evita que los textos largos ensanchen el diálogo: ajuste de línea en etiquetas y
        casillas partidas en varias líneas (QCheckBox no ajusta texto por sí sola)."""
        import textwrap
        for lbl in self.findChildren(QLabel):
            if len(lbl.text()) > width:
                lbl.setWordWrap(True)
        for cb in self.findChildren(QCheckBox):
            text = cb.text()
            if len(text) > width and "\n" not in text:
                cb.setText("\n".join(textwrap.wrap(text, width)))

    # ---------------------------------------------------------------- pestañas
    def _tab_audio(self) -> None:
        w = QWidget()
        f = QFormLayout(w)
        self.device = QComboBox()
        self.device.addItem("Predeterminado de Windows", "")
        seen = set()
        for d in list_input_devices():
            if d.name in seen:
                continue
            seen.add(d.name)
            self.device.addItem(d.name, d.name)
        idx = self.device.findData(self.s.audio.device or "")
        if idx < 0 and self.s.audio.device:
            self.device.addItem(self.s.audio.device, self.s.audio.device)
            idx = self.device.count() - 1
        self.device.setCurrentIndex(max(0, idx))
        f.addRow("Micrófono", self.device)
        self.gain = QDoubleSpinBox()
        self.gain.setRange(-20, 20)
        self.gain.setSuffix(" dB")
        self.gain.setValue(self.s.audio.gain_db)
        f.addRow("Ganancia", self.gain)
        self.ns = QComboBox()
        for k, v in (("off", "Desactivada"), ("light", "Ligera"), ("auto", "Auto"), ("strong", "Fuerte")):
            self.ns.addItem(v, k)
        self.ns.setCurrentIndex(max(0, self.ns.findData(self.s.audio.noise_suppression)))
        f.addRow("Supresión de ruido", self.ns)
        self.denoise_stt = QCheckBox("Enviar audio filtrado al transcriptor (por defecto recibe el original: prioridad a no perder palabras)")
        self.denoise_stt.setChecked(self.s.audio.denoise_for_stt)
        f.addRow("", self.denoise_stt)
        self.vad = QComboBox()
        for k, v in (("low", "Baja (exige voz clara)"), ("medium", "Media"), ("high", "Alta (detecta antes)")):
            self.vad.addItem(v, k)
        self.vad.setCurrentIndex(max(0, self.vad.findData(self.s.audio.vad_sensitivity)))
        f.addRow("Sensibilidad VAD", self.vad)
        self.end_silence = QSpinBox()
        self.end_silence.setRange(500, 5000)
        self.end_silence.setSingleStep(100)
        self.end_silence.setSuffix(" ms")
        self.end_silence.setValue(self.s.audio.end_silence_ms)
        f.addRow("Pausa máxima dentro de una frase", self.end_silence)
        self.pre_roll = QSpinBox()
        self.pre_roll.setRange(100, 2000)
        self.pre_roll.setSuffix(" ms")
        self.pre_roll.setValue(self.s.audio.pre_roll_ms)
        f.addRow("Margen antes de la voz", self.pre_roll)
        self.max_utt = QSpinBox()
        self.max_utt.setRange(20, 300)
        self.max_utt.setSuffix(" s")
        self.max_utt.setValue(self.s.audio.max_utterance_s)
        f.addRow("Corte suave de comentarios largos", self.max_utt)
        g = QGroupBox("Prueba de micrófono (6 s): habla con normalidad")
        gl = QVBoxLayout(g)
        row = QHBoxLayout()
        self.btn_mictest = QPushButton("Iniciar prueba")
        self.btn_mictest.clicked.connect(self._mic_test)
        self.mic_level = QLabel("")
        row.addWidget(self.btn_mictest)
        row.addWidget(self.mic_level, 1)
        gl.addLayout(row)
        self.mic_result = QPlainTextEdit()
        self.mic_result.setReadOnly(True)
        self.mic_result.setMaximumHeight(150)
        gl.addWidget(self.mic_result)
        self.btn_apply_rec = QPushButton("Aplicar recomendación")
        self.btn_apply_rec.setEnabled(False)
        self.btn_apply_rec.clicked.connect(self._apply_rec)
        gl.addWidget(self.btn_apply_rec)
        f.addRow(g)
        g = QGroupBox("Identificación de voz")
        gl = QVBoxLayout(g)
        self.voice_id = QCheckBox("Usar identificación de voz (marca las frases que no son tu voz)")
        self.voice_id.setChecked(self.s.voice.use_voice_id)
        gl.addWidget(self.voice_id)
        self.voice_status = QLabel("")
        self.voice_status.setWordWrap(True)
        gl.addWidget(self.voice_status)
        row = QHBoxLayout()
        b = QPushButton("Configurar mi voz…")
        b.clicked.connect(self._open_voice_enroll)
        row.addWidget(b)
        row.addStretch(1)
        gl.addLayout(row)
        vf = QFormLayout()
        self.non_match = QComboBox()
        self.non_match.addItem("Transcribir y marcar como otra voz (recomendado)", "mark")
        self.non_match.addItem("Descartar sin transcribir", "skip")
        self.non_match.setCurrentIndex(max(0, self.non_match.findData(self.s.voice.non_match_action)))
        vf.addRow("Si no es tu voz", self.non_match)
        self.voice_thr = QDoubleSpinBox()
        self.voice_thr.setRange(0.0, 0.9)
        self.voice_thr.setSingleStep(0.05)
        self.voice_thr.setDecimals(2)
        self.voice_thr.setSpecialValueText("Automático")
        self.voice_thr.setValue(float(self.s.voice.threshold or 0.0))
        vf.addRow("Umbral", self.voice_thr)
        gl.addLayout(vf)
        f.addRow(g)
        self._refresh_voice_state()
        self.sig.mic_progress.connect(lambda t, lvl: self.mic_level.setText(f"{t:0.1f} s · {lvl:0.0f} dB"))
        self.sig.mic_done.connect(self._mic_done)
        self.tabs.addTab(w, "Micrófono")

    def _refresh_voice_state(self) -> None:
        """Estado del perfil de voz: la casilla solo se activa con modelo y perfil."""
        from cronista.ui.voice_dialog import quality_label
        from cronista.voice.speaker import model_available
        has_model = model_available()
        prof = self.app.voice_gate.profile()
        ready = has_model and prof is not None
        self.voice_id.setEnabled(ready)
        if not ready:
            self.voice_id.setChecked(False)
        if not has_model:
            text = "Falta descargar el modelo de voz (se descarga desde «Configurar mi voz…»)."
        elif prof is None:
            text = "Sin perfil de voz. Pulsa «Configurar mi voz…» para crearlo (~45 s de lectura)."
        else:
            try:
                fecha = datetime.fromisoformat(prof.created_at).strftime("%d/%m/%Y %H:%M")
            except (TypeError, ValueError):
                fecha = str(prof.created_at)
            quality, _ = quality_label(prof.consistency)
            text = f"Perfil creado el {fecha} · calidad {quality}"
        self.voice_status.setText(text)

    def _open_voice_enroll(self) -> None:
        from cronista.ui.voice_dialog import VoiceEnrollDialog
        VoiceEnrollDialog(self, self.app).exec()
        self._refresh_voice_state()

    def _tab_obs(self) -> None:
        w = QWidget()
        f = QFormLayout(w)
        self.obs_auto = QCheckBox("Leer puerto y contraseña de la configuración local de OBS")
        self.obs_auto.setChecked(self.s.obs.auto_discover)
        f.addRow("", self.obs_auto)
        self.obs_host = QLineEdit(self.s.obs.host)
        f.addRow("Host", self.obs_host)
        self.obs_port = QSpinBox()
        self.obs_port.setRange(1, 65535)
        self.obs_port.setValue(self.s.obs.port)
        f.addRow("Puerto", self.obs_port)
        self.obs_pass = QLineEdit(self.s.obs.password)
        self.obs_pass.setEchoMode(QLineEdit.Password)
        self.obs_pass.setPlaceholderText("vacío = usar la de OBS si se autodetecta")
        f.addRow("Contraseña", self.obs_pass)
        self.obs_auto_session = QCheckBox("Iniciar/detener la sesión automáticamente con la grabación de OBS")
        self.obs_auto_session.setChecked(self.s.obs.auto_session)
        f.addRow("", self.obs_auto_session)
        self.obs_status = QLabel("")
        self.obs_status.setWordWrap(True)
        f.addRow("Estado", self.obs_status)
        row = QHBoxLayout()
        b = QPushButton("Reconectar ahora")
        b.clicked.connect(self._obs_reconnect)
        row.addWidget(b)
        self.btn_enable_ws = QPushButton("Activar servidor WebSocket en OBS (con OBS cerrado)")
        self.btn_enable_ws.clicked.connect(self._enable_ws)
        row.addWidget(self.btn_enable_ws)
        f.addRow(row)
        self._refresh_obs_status()
        self.tabs.addTab(w, "OBS")

    def _tab_stt(self) -> None:
        w = QWidget()
        f = QFormLayout(w)
        # motor en vivo (Parakeet o Whisper) y segunda pasada con Whisper al dejar de jugar
        self.backend = QComboBox()
        for k, v in (("auto", "Automático (Parakeet si está instalado — recomendado)"),
                     ("parakeet", "Parakeet (rápido, ~15× Whisper en tu CPU)"),
                     ("faster-whisper", "Whisper (más lento, admite pistas de vocabulario)")):
            self.backend.addItem(v, k)
        self.backend.setCurrentIndex(max(0, self.backend.findData(self.s.stt.backend)))
        f.addRow("Motor en vivo", self.backend)
        row = QHBoxLayout()
        self.parakeet_status = QLabel("")
        self.btn_parakeet = QPushButton("Descargar Parakeet (internet)")
        self.btn_parakeet.clicked.connect(self._download_parakeet)
        row.addWidget(self.parakeet_status, 1)
        row.addWidget(self.btn_parakeet)
        f.addRow("Parakeet", row)
        self._refresh_parakeet_status()
        self._parakeet_sig.progress.connect(self._set_parakeet_text, Qt.QueuedConnection)
        self._parakeet_sig.finished.connect(self._parakeet_dl_finished, Qt.QueuedConnection)
        self.second_pass = QCheckBox("Segunda pasada con Whisper al dejar de jugar (mejora títulos en inglés; "
                                     "guarda el audio de cada frase hasta repasarla y luego lo borra)")
        self.second_pass.setChecked(self.s.stt.second_pass)
        f.addRow("", self.second_pass)
        self.second_pending = QLabel("")
        self.second_pending.setObjectName("liveHint")
        f.addRow("", self.second_pending)
        try:
            pendientes = len(self.app.store.notes_pending_refine(limit=100000))
        except Exception:
            log.warning("No se pudo contar las frases pendientes de repaso", exc_info=True)
            pendientes = 0
        self.second_pending.setText(f"Frases pendientes de repasar con Whisper: {pendientes}")
        live_hint = QLabel("Mientras juegas transcribe Parakeet (rápido y ligero). Cuando no hay un juego en primer "
                           "plano, Whisper repasa lo dicho con tu glosario; si su resultado se aleja demasiado del "
                           "original, se conserva el original. Nunca cambia notas que hayas editado.")
        live_hint.setObjectName("liveHint")
        live_hint.setWordWrap(True)
        f.addRow(live_hint)
        f.addRow(QLabel(f"Hardware: {self.hw.describe()} · recomendado: {self.hw.recommended_model()}"))
        self.model = QComboBox()
        for m in MODEL_CHOICES:
            label = m if m == "auto" else f"{m}  (~{MODEL_SIZES_MB.get(m, 0)} MB{'' if model_is_downloaded(m) else ', no descargado'})"
            self.model.addItem(label, m)
        self.model.setCurrentIndex(max(0, self.model.findData(self.s.stt.model)))
        f.addRow("Modelo Whisper", self.model)
        self.pmodel = QComboBox()
        self.pmodel.addItem("El mismo que el principal", "same")
        for m in ("tiny", "base", "small"):
            self.pmodel.addItem(m, m)
        self.pmodel.setCurrentIndex(max(0, self.pmodel.findData(self.s.stt.partial_model)))
        f.addRow("Modelo para parciales", self.pmodel)
        self.partials = QComboBox()
        for k, v in (("auto", "Auto (se desactivan con juego en primer plano si priorizas rendimiento)"),
                     ("on", "Siempre"), ("off", "Nunca")):
            self.partials.addItem(v, k)
        self.partials.setCurrentIndex(max(0, self.partials.findData(self.s.stt.partials)))
        f.addRow("Transcripción parcial", self.partials)
        self.stt_device = QComboBox()
        for k in ("auto", "cpu", "cuda"):
            self.stt_device.addItem(k, k)
        self.stt_device.setCurrentIndex(max(0, self.stt_device.findData(self.s.stt.device)))
        f.addRow("Dispositivo", self.stt_device)
        self.compute = QComboBox()
        for k in ("auto", "int8", "int8_float16", "float16", "float32"):
            self.compute.addItem(k, k)
        self.compute.setCurrentIndex(max(0, self.compute.findData(self.s.stt.compute_type)))
        f.addRow("Tipo de cómputo", self.compute)
        self.threads = QSpinBox()
        self.threads.setRange(0, 64)
        self.threads.setSpecialValueText(f"Auto ({self.hw.recommended_threads()})")
        self.threads.setValue(self.s.stt.cpu_threads)
        f.addRow("Hilos de CPU", self.threads)
        self.beam = QSpinBox()
        self.beam.setRange(1, 10)
        self.beam.setValue(self.s.stt.beam_size)
        f.addRow("Beam size (precisión vs. velocidad)", self.beam)
        self.idle_unload = QSpinBox()
        self.idle_unload.setRange(0, 240)
        self.idle_unload.setSpecialValueText("Nunca")
        self.idle_unload.setSuffix(" min")
        self.idle_unload.setValue(self.s.stt.idle_unload_min)
        f.addRow("Descargar modelos tras inactividad", self.idle_unload)
        glossary_help = QLabel(
            "Títulos y términos (sobre todo en inglés) tal como deben escribirse, uno por línea. Si alguno se "
            "entiende mal, añade variantes: «Rise of Iron: D-Day = Rise of Iron Di Day, Rise of Iron didey». "
            "El juego de la sesión, los juegos que ya registraste y los términos de videojuegos (gameplay, boss, "
            "checkpoint…) se añaden solos.")
        glossary_help.setWordWrap(True)
        glossary_help.setObjectName("liveHint")
        f.addRow(glossary_help)
        self.vocab = QPlainTextEdit("\n".join(self.s.stt.vocabulary))
        self.vocab.setMaximumHeight(130)
        f.addRow("Glosario (inglés y nombres)", self.vocab)
        self.fix_terms = QCheckBox("Corregir títulos y términos mal entendidos tras transcribir")
        self.fix_terms.setChecked(self.s.stt.fix_terms)
        f.addRow("", self.fix_terms)
        row = QHBoxLayout()
        self.btn_dl = QPushButton("Descargar modelo seleccionado (internet)")
        self.btn_dl.clicked.connect(self._download_model)
        self.dl_status = QLabel("")
        row.addWidget(self.btn_dl)
        row.addWidget(self.dl_status, 1)
        f.addRow(row)
        self.sig.dl_done.connect(self._dl_done)
        self.tabs.addTab(w, "Transcripción")

    def _tab_llm(self) -> None:
        w = QWidget()
        f = QFormLayout(w)
        intro = QLabel("La IA local (llama.cpp + Qwen3.5-4B) clasifica las notas por función y tema, corrige y mejora "
                       "textos y resume sesiones. Todo se procesa en este equipo, sin internet salvo la descarga inicial.")
        intro.setWordWrap(True)
        f.addRow(intro)
        self.llm_enabled = QCheckBox("Activar IA local")
        self.llm_enabled.setChecked(self.s.llm.enabled)
        f.addRow("", self.llm_enabled)
        self.llm_auto = QCheckBox("Refinar con IA la clasificación de cada nota nueva")
        self.llm_auto.setChecked(self.s.llm.auto_classify)
        f.addRow("", self.llm_auto)
        self.llm_device = QComboBox()
        self.llm_device.addItem("GPU (Vulkan)", "gpu")
        self.llm_device.addItem("CPU", "cpu")
        self.llm_device.setCurrentIndex(max(0, self.llm_device.findData(self.s.llm.device)))
        f.addRow("Dispositivo", self.llm_device)
        self.llm_game = QComboBox()
        for k, v in (("defer", "Esperar a que no esté jugando (recomendado)"),
                     ("cpu", "Solo CPU mientras juego (lento: ~20 s por nota, ~20 % de CPU)"),
                     ("gpu", "Sin restricciones (usa ~3 GB de VRAM mientras juegas)")):
            self.llm_game.addItem(v, k)
        self.llm_game.setCurrentIndex(max(0, self.llm_game.findData(self.s.llm.during_game)))
        f.addRow("Mientras juego", self.llm_game)
        self.llm_idle = QSpinBox()
        self.llm_idle.setRange(0, 120)
        self.llm_idle.setSpecialValueText("Nunca")
        self.llm_idle.setSuffix(" min")
        self.llm_idle.setValue(self.s.llm.idle_unload_min)
        f.addRow("Descargar de memoria tras inactividad", self.llm_idle)
        self.llm_ctx = QSpinBox()
        self.llm_ctx.setRange(2048, 32768)
        self.llm_ctx.setSingleStep(1024)
        self.llm_ctx.setValue(self.s.llm.context_size)
        f.addRow("Contexto (tokens)", self.llm_ctx)
        self.llm_model = QLineEdit(self.s.llm.model_path)
        self.llm_model.setPlaceholderText("Automático: primer .gguf de la carpeta models\\llm")
        f.addRow("Modelo (.gguf)", self._path_row(self.llm_model, "Modelos GGUF (*.gguf)"))
        self.llm_runtime = QLineEdit(self.s.llm.runtime_path)
        self.llm_runtime.setPlaceholderText("Automático: tools\\llama\\llama-server.exe")
        f.addRow("Motor (llama-server.exe)", self._path_row(self.llm_runtime, "llama-server (*.exe)"))
        self.llm_status = QLabel("")
        self.llm_status.setWordWrap(True)
        f.addRow("Estado", self.llm_status)
        total_gb = llm_setup.MODEL_SIZE_GB + llm_setup.RUNTIME_SIZE_MB / 1000
        self.btn_llm_dl = QPushButton(f"Descargar motor y modelo (internet, ~{_fmt_num(total_gb)} GB)")
        self.btn_llm_dl.clicked.connect(self._download_llm)
        self.llm_dl_status = QLabel("")
        self.llm_dl_status.setWordWrap(True)
        row = QHBoxLayout()
        row.addWidget(self.btn_llm_dl)
        row.addWidget(self.llm_dl_status, 1)
        f.addRow(row)
        self.btn_llm_test = QPushButton("Probar IA")
        self.btn_llm_test.clicked.connect(self._test_llm)
        self.llm_test_status = QLabel("")
        self.llm_test_status.setWordWrap(True)
        row = QHBoxLayout()
        row.addWidget(self.btn_llm_test)
        row.addWidget(self.llm_test_status, 1)
        f.addRow(row)
        self._llm_sig.progress.connect(self._set_llm_dl_text, Qt.QueuedConnection)
        self._llm_sig.finished.connect(self._llm_dl_finished, Qt.QueuedConnection)
        self._llm_sig.test_result.connect(self._llm_test_done, Qt.QueuedConnection)
        self.llm_model.textChanged.connect(lambda _t="": self._refresh_llm_status())
        self.llm_runtime.textChanged.connect(lambda _t="": self._refresh_llm_status())
        self._refresh_llm_status()
        self.tabs.addTab(w, "IA local")

    def _tab_hotkey(self) -> None:
        w = QWidget()
        f = QFormLayout(w)
        row = QHBoxLayout()
        self.hotkey = QLineEdit(self.s.hotkey.note_key)
        row.addWidget(self.hotkey, 1)
        b = QPushButton("Capturar tecla…")
        b.clicked.connect(self._capture_key)
        row.addWidget(b)
        f.addRow("Tecla de nota", row)
        self.toggle_mode = QCheckBox("Alternar: pulsar para empezar, pulsar para terminar (en vez de mantener)")
        self.toggle_mode.setChecked(self.s.hotkey.toggle_mode)
        f.addRow("", self.toggle_mode)
        self.mode = QComboBox()
        for k, v in (("hybrid", "Híbrido (recomendado): detección automática + hotkey"),
                     ("auto", "Automático: solo detección de voz"), ("manual", "Manual: solo con hotkey")):
            self.mode.addItem(v, k)
        self.mode.setCurrentIndex(max(0, self.mode.findData(self.s.hotkey.mode)))
        f.addRow("Modo", self.mode)
        self.sig.hk_done.connect(lambda k: self.hotkey.setText(k) if k else None)
        self.tabs.addTab(w, "Hotkey")

    def _tab_perf(self) -> None:
        w = QWidget()
        f = QFormLayout(w)
        self.prio_game = QCheckBox("Priorizar rendimiento del juego (baja la prioridad del proceso y simplifica la transcripción "
                                   "mientras hay un juego a pantalla completa)")
        self.prio_game.setChecked(self.s.performance.prioritize_game)
        f.addRow("", self.prio_game)
        self.low_always = QCheckBox("Prioridad baja siempre")
        self.low_always.setChecked(self.s.performance.low_priority_always)
        f.addRow("", self.low_always)
        self.preload = QCheckBox("Cargar el modelo al arrancar (más RAM en reposo, primera nota más rápida)")
        self.preload.setChecked(self.s.general.preload_model)
        f.addRow("", self.preload)
        f.addRow(QLabel("En silencio solo trabajan el VAD (≈1 ms por bloque de 32 ms) y la supresión de ruido ligera.\n"
                        "El transcriptor se ejecuta únicamente cuando termina una frase."))
        self.tabs.addTab(w, "Rendimiento")

    def _tab_general(self) -> None:
        w = QWidget()
        f = QFormLayout(w)
        self.start_min = QCheckBox("Iniciar minimizado")
        self.start_min.setChecked(self.s.ui.start_minimized)
        f.addRow("", self.start_min)
        self.close_tray = QCheckBox("Al cerrar la ventana, seguir en la bandeja (desactivado: cerrar = salir por completo)")
        self.close_tray.setChecked(self.s.ui.close_to_tray)
        f.addRow("", self.close_tray)
        self.listen_always = QCheckBox("Mantener el micrófono abierto aunque no haya sesión (por defecto solo durante una sesión)")
        self.listen_always.setChecked(self.s.general.listen_without_session)
        f.addRow("", self.listen_always)
        self.auto_manual = QCheckBox("El hotkey crea una sesión manual si no hay ninguna activa")
        self.auto_manual.setChecked(self.s.general.auto_manual_session)
        f.addRow("", self.auto_manual)
        self.default_game = QLineEdit(self.s.general.default_game)
        f.addRow("Juego por defecto", self.default_game)
        row = QHBoxLayout()
        self.player = QLineEdit(self.s.general.player_command)
        self.player.setPlaceholderText("Autodetectar mpv / VLC / PotPlayer")
        b = QPushButton("…")
        b.clicked.connect(lambda: self._pick_file(self.player, "Reproductor (*.exe)"))
        row.addWidget(self.player, 1)
        row.addWidget(b)
        f.addRow("Reproductor para timestamps", row)
        row = QHBoxLayout()
        self.export_dir = QLineEdit(self.s.general.export_dir)
        self.export_dir.setPlaceholderText("Carpeta de datos / exports")
        b = QPushButton("…")
        b.clicked.connect(lambda: self._pick_dir(self.export_dir))
        row.addWidget(self.export_dir, 1)
        row.addWidget(b)
        f.addRow("Carpeta de exportación", row)
        g = QGroupBox("Personalización (local)")
        gl = QVBoxLayout(g)
        self.learn_style = QCheckBox("Aprender de mi estilo (notas y expresiones)")
        self.learn_style.setChecked(self.s.learning.learn_style)
        self.learn_corr = QCheckBox("Aprender de mis correcciones de clasificación")
        self.learn_corr.setChecked(self.s.learning.learn_corrections)
        gl.addWidget(self.learn_style)
        gl.addWidget(self.learn_corr)
        b = QPushButton("Eliminar todo lo aprendido")
        b.clicked.connect(self._clear_learning)
        gl.addWidget(b)
        f.addRow(g)
        f.addRow(QLabel("Privacidad: todo se procesa y guarda en este equipo. Solo las funciones marcadas como "
                        "(online) usan internet, y únicamente cuando las activas."))
        self.tabs.addTab(w, "General")

    def _tab_editor(self) -> None:
        w = QWidget()
        f = QFormLayout(w)
        e = self.s.editor
        f.addRow(QLabel("Integración con el editor Reclaimer, clips de notas y momentos destacados. Todo local."))

        self.reclaimer_dir = QLineEdit(e.reclaimer_dir)
        self.reclaimer_dir.setPlaceholderText("Autodetectar (F:\\Reclaimer o D:\\EditorIA)")
        self.reclaimer_dir.textChanged.connect(lambda _t: self._refresh_reclaimer_status())
        row = QHBoxLayout()
        b = QPushButton("…")
        b.clicked.connect(lambda: self._pick_dir(self.reclaimer_dir))
        row.addWidget(self.reclaimer_dir, 1)
        row.addWidget(b)
        f.addRow("Carpeta de Reclaimer", row)
        self.reclaimer_status = QLabel("")
        self.reclaimer_status.setWordWrap(True)
        f.addRow("", self.reclaimer_status)
        self._refresh_reclaimer_status()

        self.project_dir = QLineEdit(e.project_dir)
        self.project_dir.setPlaceholderText("Junto a la grabación")
        row = QHBoxLayout()
        b = QPushButton("…")
        b.clicked.connect(lambda: self._pick_dir(self.project_dir))
        row.addWidget(self.project_dir, 1)
        row.addWidget(b)
        f.addRow("Carpeta de proyectos generados", row)

        self.clip_dir = QLineEdit(e.clip_dir)
        self.clip_dir.setPlaceholderText("Carpeta «Clips Cronista» junto a la grabación")
        row = QHBoxLayout()
        b = QPushButton("…")
        b.clicked.connect(lambda: self._pick_dir(self.clip_dir))
        row.addWidget(self.clip_dir, 1)
        row.addWidget(b)
        f.addRow("Carpeta de clips", row)

        self.clip_pre = QDoubleSpinBox()
        self.clip_post = QDoubleSpinBox()
        for spin, value in ((self.clip_pre, e.clip_pre_s), (self.clip_post, e.clip_post_s)):
            spin.setRange(0.0, 60.0)
            spin.setDecimals(1)
            spin.setSingleStep(0.5)
            spin.setSuffix(" s")
            spin.setValue(float(value))
        f.addRow("Segundos antes de la nota", self.clip_pre)
        f.addRow("Segundos después de lo dicho", self.clip_post)
        self.clip_mode = QComboBox()
        self.clip_mode.addItem("Rápido: sin recodificar (puede empezar unos segundos antes)", "copy")
        self.clip_mode.addItem("Exacto: recodifica con la GPU", "exact")
        self.clip_mode.setCurrentIndex(max(0, self.clip_mode.findData(e.clip_mode)))
        f.addRow("Modo de clip", self.clip_mode)

        self.marker_notes = QCheckBox("Poner un marcador por nota en el proyecto de Reclaimer")
        self.marker_notes.setChecked(e.marker_notes)
        self.favorites_notes = QCheckBox("Guardar cada nota como momento favorito")
        self.favorites_notes.setChecked(e.favorites_notes)
        self.include_moments = QCheckBox("Incluir los momentos detectados (marcadores de otro color)")
        self.include_moments.setChecked(e.include_moments)
        f.addRow("", self.marker_notes)
        f.addRow("", self.favorites_notes)
        f.addRow("", self.include_moments)
        self.tabs.addTab(w, "Editor y clips")

    def _refresh_reclaimer_status(self) -> None:
        tmp = Settings()
        tmp.editor.reclaimer_dir = self.reclaimer_dir.text().strip()
        found = find_reclaimer(tmp)
        typed = tmp.editor.reclaimer_dir
        if typed and (not found or Path(typed).resolve() != Path(found).resolve()):
            # la carpeta escrita no es Reclaimer: avisar aunque haya otra detectada por defecto
            extra = f" Se usará la detectada en {found}." if found else ""
            self.reclaimer_status.setText(f"«{typed}» no contiene Reclaimer.{extra}")
            self.reclaimer_status.setStyleSheet(f"color: {COLORS['warn']};")
        elif found:
            self.reclaimer_status.setText(f"Reclaimer encontrado en {found}")
            self.reclaimer_status.setStyleSheet(f"color: {COLORS['ok']};")
        else:
            self.reclaimer_status.setText("No encontrado. Se busca en F:\\Reclaimer y D:\\EditorIA; "
                                          "indica la carpeta si está en otro sitio.")
            self.reclaimer_status.setStyleSheet(f"color: {COLORS['warn']};")

    def _tab_ai(self) -> None:
        w = QWidget()
        f = QFormLayout(w)
        f.addRow(QLabel("Opcional. Por defecto el paquete de notas se copia al portapapeles para pegarlo en ChatGPT, "
                        "Claude, Gemini… Si configuras una API, el envío es ONLINE y solo ocurre al pulsar el botón."))
        self.provider = QComboBox()
        for k, v in PROVIDERS.items():
            self.provider.addItem(v, k)
        self.provider.setCurrentIndex(max(0, self.provider.findData(self.s.ai.provider)))
        f.addRow("Proveedor", self.provider)
        self.base_url = QLineEdit(self.s.ai.base_url)
        self.base_url.setPlaceholderText("https://api.openai.com/v1 · http://localhost:1234/v1 (LM Studio) · http://localhost:11434/v1 (Ollama)")
        f.addRow("URL base (OpenAI-compatible)", self.base_url)
        self.api_key = QLineEdit(self.s.ai.api_key)
        self.api_key.setEchoMode(QLineEdit.Password)
        f.addRow("API key", self.api_key)
        self.ai_model = QLineEdit(self.s.ai.model)
        self.ai_model.setPlaceholderText("gpt-4o-mini · claude-sonnet-5-5 · nombre del modelo local")
        f.addRow("Modelo", self.ai_model)
        self.tabs.addTab(w, "IA / Chatbot")

    # ---------------------------------------------------------------- acciones
    def _pick_file(self, edit: QLineEdit, flt: str) -> None:
        p, _ = QFileDialog.getOpenFileName(self, "Seleccionar", "", flt)
        if p:
            edit.setText(p)

    def _pick_dir(self, edit: QLineEdit) -> None:
        p = QFileDialog.getExistingDirectory(self, "Seleccionar carpeta")
        if p:
            edit.setText(p)

    def _mic_test(self) -> None:
        from cronista.audio.mictest import run_mic_test
        self.btn_mictest.setEnabled(False)
        self.mic_result.setPlainText("Grabando…")
        device = self.device.currentData() or None
        was_listening = self.app.audio.listening
        if was_listening:
            self.app.audio.stop()

        def run() -> None:
            try:
                r = run_mic_test(device, 6.0, lambda t, lvl: self.sig.mic_progress.emit(t, lvl))
            except Exception as e:
                r = {"ok": False, "error": str(e)}
            r["_restart"] = was_listening
            self.sig.mic_done.emit(r)

        threading.Thread(target=run, daemon=True).start()

    def _mic_done(self, r: dict) -> None:
        self.btn_mictest.setEnabled(True)
        if r.get("_restart"):
            self.app._refresh_audio()
        if not r.get("ok"):
            self.mic_result.setPlainText(f"Error: {r.get('error')}")
            return
        self._rec = r["recommend"]
        lines = [f"Dispositivo: {r['device']} @ {r['rate']} Hz",
                 f"Ruido de fondo: {r['noise_db']} dB · Voz: {r['speech_db']} dB · Pico: {r['peak_db']} dB",
                 f"Relación señal/ruido: {r['snr_db']} dB · Voz detectada el {int(r['speech_ratio'] * 100)}% del tiempo",
                 f"Ruido constante: {'sí' if r['constant_noise'] else 'no'}", ""] + r["notes"]
        lines.append("")
        lines.append("Recomendación: " + ", ".join(f"{k}={v}" for k, v in self._rec.items()))
        self.mic_result.setPlainText("\n".join(lines))
        self.btn_apply_rec.setEnabled(True)

    def _apply_rec(self) -> None:
        rec = getattr(self, "_rec", {})
        if "noise_suppression" in rec:
            self.ns.setCurrentIndex(max(0, self.ns.findData(rec["noise_suppression"])))
        if "vad_sensitivity" in rec:
            self.vad.setCurrentIndex(max(0, self.vad.findData(rec["vad_sensitivity"])))
        if "gain_db" in rec:
            self.gain.setValue(float(rec["gain_db"]))

    def _refresh_obs_status(self) -> None:
        cfg = read_obs_websocket_config()
        parts = [self.app.obs.status_text()]
        if cfg is None:
            parts.append("No se encontró la configuración de obs-websocket (¿OBS instalado?).")
        else:
            parts.append(f"Config de OBS: servidor {'ACTIVADO' if cfg.enabled else 'DESACTIVADO'}, puerto {cfg.port}, "
                         f"autenticación {'sí' if cfg.auth_required else 'no'}.")
            self.btn_enable_ws.setEnabled(not cfg.enabled)
        parts.append("OBS " + ("está abierto." if obs_running() else "no está abierto."))
        self.obs_status.setText("\n".join(parts))

    def _obs_reconnect(self) -> None:
        self.app.settings.obs.host = self.obs_host.text().strip() or "localhost"
        self.app.settings.obs.port = self.obs_port.value()
        self.app.settings.obs.password = self.obs_pass.text()
        self.app.settings.obs.auto_discover = self.obs_auto.isChecked()
        self.app.obs.reconnect_now()
        from PySide6.QtCore import QTimer
        QTimer.singleShot(2500, self._refresh_obs_status)

    def _enable_ws(self) -> None:
        ok, msg = enable_obs_websocket_server(self.obs_port.value())
        QMessageBox.information(self, APP_NAME, msg) if ok else QMessageBox.warning(self, APP_NAME, msg)
        self._refresh_obs_status()

    # ---- Parakeet (motor en vivo) ----------------------------------------------------
    def _refresh_parakeet_status(self) -> None:
        from cronista.stt import parakeet
        installed = parakeet.available()
        if installed:
            self.parakeet_status.setText("Parakeet instalado")
            self.parakeet_status.setStyleSheet(f"color: {COLORS['ok']};")
        else:
            self.parakeet_status.setText("Parakeet no instalado (~670 MB)")
            self.parakeet_status.setStyleSheet(f"color: {COLORS['warn']};")
        self.btn_parakeet.setEnabled(not installed)

    def _set_parakeet_text(self, text: str) -> None:
        try:
            self.parakeet_status.setText(text)
        except RuntimeError:       # el diálogo ya se cerró y se destruyó
            pass

    def _parakeet_dl_finished(self, err: str) -> None:
        try:
            self._refresh_parakeet_status()
            if err:
                self.parakeet_status.setText(f"Error: {err}")
                self.parakeet_status.setStyleSheet(f"color: {COLORS['err']};")
                QMessageBox.warning(self, APP_NAME, f"No se pudo descargar Parakeet:\n{err}")
        except RuntimeError:
            pass

    def _download_parakeet(self) -> None:
        from cronista.stt import parakeet
        if parakeet.available():
            self._refresh_parakeet_status()
            return
        texto = ("Se descargará de internet el modelo de transcripción Parakeet-TDT-0.6B-v3 (~670 MB) desde "
                 "Hugging Face (repositorio del autor de sherpa-onnx).\n\n"
                 "Modelo de NVIDIA, licencia CC-BY-4.0. Solo se descarga una vez; después la transcripción en vivo "
                 "funciona sin internet.\n\n"
                 "¿Continuar?")
        if QMessageBox.question(self, APP_NAME, texto) != QMessageBox.Yes:
            return
        self.btn_parakeet.setEnabled(False)
        self.parakeet_status.setText("Iniciando descarga…")
        sig = self._parakeet_sig

        def progreso(hechos: int, total: int) -> None:
            total_mb = total / 1e6 if total else 670
            sig.progress.emit(f"Descargando Parakeet… {_fmt_num(hechos / 1e6, 0)} / {_fmt_num(total_mb, 0)} MB")

        def run() -> None:
            try:
                parakeet.install_model(progreso)
                sig.finished.emit("")
            except Exception as e:
                log.warning("Descarga de Parakeet fallida: %s", e)
                sig.finished.emit(str(e) or type(e).__name__)

        threading.Thread(target=run, daemon=True, name="parakeet-download").start()

    def _download_model(self) -> None:
        name = self.model.currentData()
        if name == "auto":
            name = self.hw.recommended_model()
        self.btn_dl.setEnabled(False)
        self.dl_status.setText(f"Descargando {name}… (~{MODEL_SIZES_MB.get(name, 0)} MB)")
        from cronista.stt.engine import download_model

        def run() -> None:
            try:
                download_model(name)
                self.sig.dl_done.emit(name, "")
            except Exception as e:
                self.sig.dl_done.emit(name, str(e))

        threading.Thread(target=run, daemon=True).start()

    def _dl_done(self, name: str, err: str) -> None:
        self.btn_dl.setEnabled(True)
        self.dl_status.setText(f"{name} listo." if not err else f"Error: {err}")

    def _capture_key(self) -> None:
        from cronista.hotkey.manager import HotkeyManager
        self.hotkey.setText("Pulsa una tecla…")
        self.app.hotkey.stop()

        def run() -> None:
            k = HotkeyManager.read_key()
            self.sig.hk_done.emit(k)
            self.app.hotkey.start()

        threading.Thread(target=run, daemon=True).start()

    def _clear_learning(self) -> None:
        if QMessageBox.question(self, APP_NAME, "¿Eliminar todas las correcciones y datos de personalización?") == QMessageBox.Yes:
            self.app.clear_learning()

    # ---------------------------------------------------------------- IA local
    def _path_row(self, edit: QLineEdit, flt: str) -> QHBoxLayout:
        row = QHBoxLayout()
        b = QPushButton("…")
        b.clicked.connect(lambda: self._pick_file(edit, flt))
        row.addWidget(edit, 1)
        row.addWidget(b)
        return row

    def _llm_cfg_now(self) -> LLMSettings:
        """Copia de la configuración LLM con los valores actuales de los controles (sin tocar self.s)."""
        return self._apply_llm_widgets(dataclasses.replace(self.s.llm))

    def _apply_llm_widgets(self, cfg: LLMSettings) -> LLMSettings:
        cfg.enabled = self.llm_enabled.isChecked()
        cfg.auto_classify = self.llm_auto.isChecked()
        cfg.device = self.llm_device.currentData()
        cfg.during_game = self.llm_game.currentData()
        cfg.idle_unload_min = self.llm_idle.value()
        cfg.context_size = self.llm_ctx.value()
        cfg.model_path = self.llm_model.text().strip()
        cfg.runtime_path = self.llm_runtime.text().strip()
        return cfg

    def _refresh_llm_status(self) -> None:
        st = llm_setup.status(self._llm_cfg_now())
        motor = f"Motor: {st['runtime']}" if st["runtime"] else "Motor: no instalado"
        modelo = f"Modelo: {st['model']}" if st["model"] else "Modelo: no instalado"
        self.llm_status.setText(motor + "\n" + modelo)

    def _set_llm_dl_text(self, text: str) -> None:
        try:
            self.llm_dl_status.setText(text)
        except RuntimeError:       # el diálogo ya se cerró y se destruyó
            pass

    def _llm_dl_finished(self, err: str) -> None:
        try:
            self.btn_llm_dl.setEnabled(True)
            self.llm_dl_status.setText("Listo." if not err else f"Error: {err}")
            self._refresh_llm_status()
            if err:
                QMessageBox.warning(self, APP_NAME, f"No se pudo descargar la IA local:\n{err}")
        except RuntimeError:
            pass

    def _download_llm(self) -> None:
        st = llm_setup.status(self._llm_cfg_now())
        need_rt = st["runtime"] is None
        need_model = st["model"] is None
        if not (need_rt or need_model):
            QMessageBox.information(self, APP_NAME, "El motor y el modelo ya están instalados.")
            return
        partes = []
        if need_rt:
            partes.append(f"• llama.cpp oficial (GitHub, ~{llm_setup.RUNTIME_SIZE_MB} MB)")
        if need_model:
            partes.append(f"• Modelo Qwen3.5-4B Q4_K_M (Hugging Face, ~{_fmt_num(llm_setup.MODEL_SIZE_GB)} GB)")
        texto = ("Se descargarán de internet:\n" + "\n".join(partes) +
                 "\n\nLicencia: Apache-2.0. Solo se descargan una vez; después la IA funciona sin internet.\n\n"
                 "¿Continuar?")
        if QMessageBox.question(self, APP_NAME, texto) != QMessageBox.Yes:
            return
        self.btn_llm_dl.setEnabled(False)
        self.llm_dl_status.setText("Iniciando descarga…")
        sig = self._llm_sig

        def progreso_motor(hechos: int, total: int) -> None:
            if total:
                sig.progress.emit(f"Descargando motor… {int(hechos * 100 / total)} %")
            else:
                sig.progress.emit("Descargando motor…")

        def progreso_modelo(hechos: int, total: int) -> None:
            total_b = total or llm_setup.MODEL_SIZE_GB * 1e9
            sig.progress.emit(f"Descargando modelo… {_fmt_num(hechos / 1e9)} / {_fmt_num(total_b / 1e9)} GB")

        def run() -> None:
            try:
                if need_rt:
                    llm_setup.install_runtime(progreso_motor)
                if need_model:
                    llm_setup.install_model(progreso_modelo)
                sig.finished.emit("")
            except Exception as e:
                log.warning("Descarga de la IA local fallida: %s", e)
                sig.finished.emit(str(e) or type(e).__name__)

        threading.Thread(target=run, daemon=True, name="llm-download").start()

    def _test_llm(self) -> None:
        self.btn_llm_test.setEnabled(False)
        self.llm_test_status.setText("Probando… (si el modelo no está cargado, tarda más)")
        cfg = self._llm_cfg_now()
        sig = self._llm_sig

        def run() -> None:
            llm = None
            t0 = time.monotonic()
            try:
                llm = LocalLLM(cfg)
                respuesta = llm.chat([{"role": "user", "content": "Responde solo: OK"}], max_tokens=8)
                dt = time.monotonic() - t0
                sig.test_result.emit(f"Respuesta en {_fmt_num(dt)} s (incluye carga del modelo). "
                                     f"Contestó: «{respuesta.strip()[:40]}»", True)
            except LLMUnavailable as e:
                sig.test_result.emit(str(e), False)
            except Exception as e:
                log.warning("Prueba de IA local fallida: %s", e)
                sig.test_result.emit(f"Error inesperado: {e}", False)
            finally:
                if llm is not None:
                    try:
                        llm.unload()
                    except Exception:
                        log.debug("No se pudo descargar la IA tras la prueba", exc_info=True)

        threading.Thread(target=run, daemon=True, name="llm-test").start()

    def _llm_test_done(self, text: str, ok: bool) -> None:
        try:
            self.btn_llm_test.setEnabled(True)
            self.llm_test_status.setText(text)
            self.llm_test_status.setStyleSheet(f"color: {COLORS['ok'] if ok else COLORS['err']};")
            self._refresh_llm_status()
        except RuntimeError:
            pass

    # ---------------------------------------------------------------- resultado
    def result_settings(self) -> Settings:
        s = self.s
        s.stt.backend = self.backend.currentData()
        s.stt.second_pass = self.second_pass.isChecked()
        s.audio.device = self.device.currentData() or None
        s.audio.gain_db = self.gain.value()
        s.audio.noise_suppression = self.ns.currentData()
        s.audio.denoise_for_stt = self.denoise_stt.isChecked()
        s.audio.vad_sensitivity = self.vad.currentData()
        s.audio.end_silence_ms = self.end_silence.value()
        s.audio.pre_roll_ms = self.pre_roll.value()
        s.audio.max_utterance_s = self.max_utt.value()
        s.voice.use_voice_id = self.voice_id.isChecked()
        s.voice.non_match_action = self.non_match.currentData()
        s.voice.threshold = self.voice_thr.value()
        s.obs.auto_discover = self.obs_auto.isChecked()
        s.obs.host = self.obs_host.text().strip() or "localhost"
        s.obs.port = self.obs_port.value()
        s.obs.password = self.obs_pass.text()
        s.obs.auto_session = self.obs_auto_session.isChecked()
        s.stt.model = self.model.currentData()
        s.stt.partial_model = self.pmodel.currentData()
        s.stt.partials = self.partials.currentData()
        s.stt.device = self.stt_device.currentData()
        s.stt.compute_type = self.compute.currentData()
        s.stt.cpu_threads = self.threads.value()
        s.stt.beam_size = self.beam.value()
        s.stt.idle_unload_min = self.idle_unload.value()
        s.stt.vocabulary = [x.strip() for x in self.vocab.toPlainText().splitlines() if x.strip()]
        s.stt.fix_terms = self.fix_terms.isChecked()
        key = self.hotkey.text().strip().lower()
        if key and not key.startswith("pulsa"):
            s.hotkey.note_key = key
        s.hotkey.toggle_mode = self.toggle_mode.isChecked()
        s.hotkey.mode = self.mode.currentData()
        s.performance.prioritize_game = self.prio_game.isChecked()
        s.performance.low_priority_always = self.low_always.isChecked()
        s.general.preload_model = self.preload.isChecked()
        s.ui.start_minimized = self.start_min.isChecked()
        s.ui.close_to_tray = self.close_tray.isChecked()
        s.general.listen_without_session = self.listen_always.isChecked()
        s.general.auto_manual_session = self.auto_manual.isChecked()
        s.general.default_game = self.default_game.text().strip()
        s.general.player_command = self.player.text().strip()
        s.general.export_dir = self.export_dir.text().strip()
        s.learning.learn_style = self.learn_style.isChecked()
        s.learning.learn_corrections = self.learn_corr.isChecked()
        s.ai.provider = self.provider.currentData()
        s.ai.base_url = self.base_url.text().strip()
        s.ai.api_key = self.api_key.text().strip()
        s.ai.model = self.ai_model.text().strip()
        e = s.editor
        e.reclaimer_dir = self.reclaimer_dir.text().strip()
        e.project_dir = self.project_dir.text().strip()
        e.clip_dir = self.clip_dir.text().strip()
        e.clip_pre_s = float(self.clip_pre.value())
        e.clip_post_s = float(self.clip_post.value())
        e.clip_mode = self.clip_mode.currentData()
        e.marker_notes = self.marker_notes.isChecked()
        e.favorites_notes = self.favorites_notes.isChecked()
        e.include_moments = self.include_moments.isChecked()
        self._apply_llm_widgets(s.llm)
        return s

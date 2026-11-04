"""Diálogo «Configurar mi voz»: crea el perfil de voz local y permite probarlo.

Flujo: descarga del modelo (si falta) → lectura de un texto (30-45 s) → análisis → guardar o repetir.
La grabación NUNCA se guarda en disco: solo el perfil (256 números). Todo se procesa en este equipo.
"""
from __future__ import annotations

import functools
import logging
import queue
import threading
import time
from datetime import datetime

import numpy as np
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QDialog, QFrame, QGroupBox, QHBoxLayout, QLabel, QMessageBox, QProgressBar,
                               QPushButton, QScrollArea, QTextBrowser, QVBoxLayout, QWidget)

from cronista import APP_NAME
from cronista.audio import dsp
from cronista.audio.capture import MicCapture, resolve_device
from cronista.audio.vad import build_vad
from cronista.ui.theme import COLORS
from cronista.voice.speaker import (MIN_ENROLL_SPEECH_S, MODEL_SIZE_MB, SpeakerEncoder, VoiceProfile, build_profile,
                                    install_model, model_available, speech_only)

log = logging.getLogger(__name__)

SR = 16000
BLOCK = 512                       # muestras por bloque de VAD a 16 kHz
ENROLL_MAX_S = 45.0               # la grabación del perfil se para sola a los 45 s...
ENROLL_TARGET_SPEECH_S = 40.0     # ...o cuando hay 40 s de voz
BAR_GOAL_S = 30                   # la barra de progreso se llena con 30 s de voz
TEST_S = 5.0                      # duración de la prueba "¿Es tu voz?"
SPEECH_P = 0.5                    # probabilidad de VAD a partir de la cual un bloque cuenta como voz

READ_TEXT = (
    "Me encanta jugar, y cuando encuentro un buen juego no puedo parar. Lo que más valoro es el diseño de los "
    "niveles: un escenario que te invita a explorar, a probar caminos distintos y a descubrir secretos escondidos "
    "hace que la experiencia sea memorable. El combate también importa. Un buen mando responde al instante, las "
    "animaciones transmiten peso y cada enemigo te obliga a pensar antes de atacar. Y la banda sonora, a veces, "
    "cuenta más que cualquier diálogo. Hay títulos que no tienen una historia complicada, pero te atrapan desde el "
    "primer minuto. Otros necesitan horas para mostrar lo que realmente valen. Así que, si me preguntas qué hace "
    "grande a un juego, te diría que es la combinación de todo eso: jugabilidad sólida, mundos con personalidad y "
    "la sensación de que tus decisiones importan. Por eso sigo probando juegos nuevos, aunque no todos acaben "
    "siendo mis favoritos."
)


def _fmt(x: float, nd: int = 1) -> str:
    """Número con coma decimal (estilo español)."""
    return f"{x:.{nd}f}".replace(".", ",")


def quality_label(consistency: float) -> tuple[str, str]:
    """(etiqueta, consejo) según la similitud media de los trozos de la grabación con su media."""
    if consistency >= 0.75:
        return "buena", ""
    if consistency >= 0.6:
        return "aceptable", ""
    return "baja", "Repite la grabación en un sitio con menos ruido, leyendo con naturalidad y sin pausas largas."


def _record(device_name: str | None, max_s: float, target_speech_s: float | None, stop: threading.Event,
            progress) -> tuple[np.ndarray, list[float]]:
    """Graba del micrófono configurado a 16 kHz hasta `max_s`, hasta `stop` o hasta tener `target_speech_s` de voz.
    Devuelve el audio (bloques de 512 muestras con VAD) y la probabilidad de voz de cada bloque.
    Bloqueante: llamar desde un hilo de trabajo. Cierra siempre el micrófono."""
    dev = resolve_device(device_name)
    if dev is None:
        raise RuntimeError("No se encontró ningún micrófono")
    cap = MicCapture(dev)
    rate = cap.start()
    res = dsp.StreamResampler(rate, SR)
    vad = build_vad(SR)
    acc = np.zeros(0, dtype=np.float32)
    parts: list[np.ndarray] = []
    probs: list[float] = []
    speech_s = 0.0
    lvl = -120.0
    last_emit = -1.0
    t0 = time.monotonic()
    try:
        while not stop.is_set():
            elapsed = time.monotonic() - t0
            if elapsed >= max_s or (target_speech_s and speech_s >= target_speech_s):
                break
            try:
                blk = cap.queue.get(timeout=0.2)
            except queue.Empty:
                continue
            x = blk.samples
            lvl = dsp.rms_db(x)
            acc = np.concatenate([acc, res.process(x)])
            while len(acc) >= BLOCK:
                c, acc = acc[:BLOCK], acc[BLOCK:]
                p = vad.prob(c)
                parts.append(c)
                probs.append(p)
                if p >= SPEECH_P:
                    speech_s += BLOCK / SR
            if elapsed - last_emit >= 0.2:       # no saturar la interfaz con eventos
                last_emit = elapsed
                progress(elapsed, speech_s, lvl)
    finally:
        cap.stop()
    audio = np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
    return audio, probs


def _safe(fn):
    """Los hilos pueden responder cuando el diálogo ya se cerró y los widgets se destruyeron: se ignora."""
    @functools.wraps(fn)
    def wrapper(self, *args):
        try:
            fn(self, *args)
        except RuntimeError:
            pass
    return wrapper


class _Signals(QObject):
    """Señales que emiten los hilos de trabajo; la UI las recibe en su hilo (conexión en cola)."""
    rec_progress = Signal(float, float, float)   # segundos grabados, segundos de voz, nivel dB
    rec_done = Signal(str, object)               # tipo ("enroll" | "test"), resultado (dict)
    analyzed = Signal(object)                    # resultado del análisis (dict)
    dl_progress = Signal(str)                    # texto de progreso de la descarga
    dl_done = Signal(str)                        # error ("" = correcto)


class VoiceEnrollDialog(QDialog):
    def __init__(self, parent, app) -> None:
        super().__init__(parent)
        self.app = app
        self.setWindowTitle("Configurar mi voz")
        self.resize(680, 720)   # grabación, resultado y prueba visibles sin desplazarse
        self._sig = _Signals()
        self._sig.rec_progress.connect(self._on_rec_progress, Qt.QueuedConnection)
        self._sig.rec_done.connect(self._on_rec_done, Qt.QueuedConnection)
        self._sig.analyzed.connect(self._on_analyzed, Qt.QueuedConnection)
        self._sig.dl_progress.connect(self._on_dl_progress, Qt.QueuedConnection)
        self._sig.dl_done.connect(self._on_dl_done, Qt.QueuedConnection)
        self._stop_evt = threading.Event()
        self._closing = False
        self._recording = False
        self._rec_kind = ""
        self._speech_s = 0.0
        self._busy = False                        # análisis en marcha
        self._pending: VoiceProfile | None = None  # perfil recién creado, pendiente de guardar
        self._build()
        self._refresh_view()

    # ---------------------------------------------------------------- construcción
    def _build(self) -> None:
        lay = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(4, 4, 4, 4)
        scroll.setWidget(body)
        lay.addWidget(scroll, 1)

        intro = QLabel("Se graba tu voz leyendo un texto durante unos 30-45 segundos. Con esa grabación se crea una "
                       "huella numérica local (256 números) que ayuda a distinguir tu voz de otras voces y ruidos.")
        intro.setWordWrap(True)
        bl.addWidget(intro)
        privacy = QLabel("• La grabación NO se guarda: solo se guarda la huella.\n"
                         "• Puedes borrar el perfil cuando quieras.\n"
                         "• Se usa solo en este equipo; no sirve para identificarte fuera de él.")
        privacy.setWordWrap(True)
        bl.addWidget(privacy)

        # ---- modelo (solo si falta) -----------------------------------------------------
        self.model_box = QGroupBox("Modelo de voz necesario")
        ml = QVBoxLayout(self.model_box)
        m_lbl = QLabel(f"Antes de grabar hay que descargar el modelo de voz (~{_fmt(MODEL_SIZE_MB)} MB). "
                       "Origen: GitHub, proyecto sherpa-onnx (modelo WeSpeaker), licencia Apache-2.0. "
                       "Solo se descarga una vez.")
        m_lbl.setWordWrap(True)
        ml.addWidget(m_lbl)
        row = QHBoxLayout()
        self.btn_download = QPushButton(f"Descargar modelo de voz (internet, ~{_fmt(MODEL_SIZE_MB)} MB)")
        self.btn_download.clicked.connect(self._download_model)
        self.dl_status = QLabel("")
        self.dl_status.setWordWrap(True)
        row.addWidget(self.btn_download)
        row.addWidget(self.dl_status, 1)
        ml.addLayout(row)
        bl.addWidget(self.model_box)

        # ---- texto para leer ------------------------------------------------------------
        bl.addWidget(QLabel("Lee en voz alta este texto, con tu voz normal:"))
        self.text = QTextBrowser()
        self.text.setReadOnly(True)
        self.text.setPlainText(READ_TEXT)
        font = self.text.font()
        font.setPointSize(13)
        self.text.setFont(font)
        self.text.setMinimumHeight(170)
        bl.addWidget(self.text)

        # ---- grabación ------------------------------------------------------------------
        self.rec_box = QGroupBox("Grabación")
        rl = QVBoxLayout(self.rec_box)
        row = QHBoxLayout()
        self.btn_start = QPushButton("Empezar grabación")
        self.btn_start.setObjectName("primary")
        self.btn_start.clicked.connect(lambda: self._start_recording("enroll"))
        self.btn_stop = QPushButton("Terminar")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop_recording)
        self.rec_level = QLabel("")
        self.rec_level.setObjectName("liveHint")
        row.addWidget(self.btn_start)
        row.addWidget(self.btn_stop)
        row.addStretch(1)
        row.addWidget(self.rec_level)
        rl.addLayout(row)
        self.bar = QProgressBar()
        self.bar.setRange(0, BAR_GOAL_S)
        self.bar.setFormat("%v / 30 s de voz")
        self.bar.setValue(0)
        rl.addWidget(self.bar)
        self.rec_status = QLabel("Pulsa «Empezar grabación» y lee el texto con tu voz normal.")
        self.rec_status.setWordWrap(True)
        rl.addWidget(self.rec_status)
        bl.addWidget(self.rec_box)

        # ---- resultado del análisis ---------------------------------------------------------
        self.result_box = QGroupBox("Perfil listo para guardar")
        rsl = QVBoxLayout(self.result_box)
        self.result_lbl = QLabel("")
        self.result_lbl.setWordWrap(True)
        rsl.addWidget(self.result_lbl)
        row = QHBoxLayout()
        self.btn_save = QPushButton("Guardar perfil")
        self.btn_save.setObjectName("primary")
        self.btn_save.clicked.connect(self._save_profile)
        self.btn_repeat = QPushButton("Repetir")
        self.btn_repeat.clicked.connect(self._repeat)
        row.addWidget(self.btn_save)
        row.addWidget(self.btn_repeat)
        row.addStretch(1)
        rsl.addLayout(row)
        bl.addWidget(self.result_box)

        # ---- prueba -----------------------------------------------------------------------
        self.test_box = QGroupBox("Probar")
        tl = QVBoxLayout(self.test_box)
        self.btn_test = QPushButton(f"Probar mi voz ({TEST_S:.0f} s)")
        self.btn_test.clicked.connect(lambda: self._start_recording("test"))
        tl.addWidget(self.btn_test)
        self.test_lbl = QLabel("")
        self.test_lbl.setWordWrap(True)
        tl.addWidget(self.test_lbl)
        bl.addWidget(self.test_box)

        self.profile_lbl = QLabel("")
        self.profile_lbl.setWordWrap(True)
        bl.addWidget(self.profile_lbl)
        bl.addStretch(1)

        # ---- pie ----------------------------------------------------------------------------
        foot = QHBoxLayout()
        self.btn_delete = QPushButton("Eliminar perfil")
        self.btn_delete.setObjectName("danger")
        self.btn_delete.clicked.connect(self._delete_profile)
        btn_close = QPushButton("Cerrar")
        btn_close.clicked.connect(lambda: self.reject())
        foot.addWidget(self.btn_delete)
        foot.addStretch(1)
        foot.addWidget(btn_close)
        lay.addLayout(foot)

    @staticmethod
    def _set_text(label: QLabel, text: str, color: str | None = None) -> None:
        label.setText(text)
        label.setStyleSheet(f"color: {color};" if color else "")

    # ---------------------------------------------------------------- estado de la vista
    def _profile_text(self, prof: VoiceProfile | None, has_model: bool) -> str:
        if prof is None:
            return "Sin perfil de voz guardado." if has_model else "Sin perfil de voz (primero descarga el modelo)."
        try:
            fecha = datetime.fromisoformat(prof.created_at).strftime("%d/%m/%Y %H:%M")
        except (TypeError, ValueError):
            fecha = str(prof.created_at)
        quality, _ = quality_label(prof.consistency)
        return f"Perfil guardado el {fecha} · calidad {quality} · umbral automático {_fmt(prof.threshold, 2)}."

    def _refresh_view(self) -> None:
        has_model = model_available()
        prof = self.app.voice_gate.profile()
        idle = not self._recording and not self._busy
        self.model_box.setVisible(not has_model)
        self.rec_box.setEnabled(has_model)
        self.btn_start.setEnabled(has_model and idle and self._pending is None)
        self.btn_stop.setEnabled(self._recording and self._rec_kind == "enroll"
                                 and self._speech_s >= MIN_ENROLL_SPEECH_S and not self._stop_evt.is_set())
        self.btn_test.setEnabled(has_model and idle and prof is not None)
        self.result_box.setVisible(self._pending is not None)
        self.btn_save.setEnabled(idle and self._pending is not None)
        self.btn_repeat.setEnabled(idle and self._pending is not None)
        self.btn_delete.setEnabled(idle and prof is not None)
        self.profile_lbl.setText(self._profile_text(prof, has_model))

    # ---------------------------------------------------------------- descarga del modelo
    def _download_model(self) -> None:
        texto = (f"Se descargará de internet el modelo de voz (~{_fmt(MODEL_SIZE_MB)} MB).\n\n"
                 "Origen: GitHub, proyecto sherpa-onnx (modelo WeSpeaker ResNet34).\n"
                 "Licencia: Apache-2.0. Solo se descarga una vez; después la identificación de voz funciona sin internet."
                 "\n\n¿Continuar?")
        if QMessageBox.question(self, APP_NAME, texto) != QMessageBox.Yes:
            return
        self.btn_download.setEnabled(False)
        self.dl_status.setText("Iniciando descarga…")
        sig = self._sig

        def progress(hechos: int, total: int) -> None:
            if total:
                sig.dl_progress.emit(f"Descargando… {int(hechos * 100 / total)} %")
            else:
                sig.dl_progress.emit(f"Descargando… {_fmt(hechos / 1e6)} MB")

        def run() -> None:
            try:
                install_model(progress)
                sig.dl_done.emit("")
            except Exception as e:
                log.warning("Descarga del modelo de voz fallida: %s", e)
                sig.dl_done.emit(str(e) or type(e).__name__)

        threading.Thread(target=run, daemon=True, name="voice-model-download").start()

    @_safe
    def _on_dl_progress(self, text: str) -> None:
        self.dl_status.setText(text)

    @_safe
    def _on_dl_done(self, err: str) -> None:
        self.btn_download.setEnabled(True)
        if err:
            self._set_text(self.dl_status, f"Error: {err}", COLORS["err"])
            QMessageBox.warning(self, APP_NAME, f"No se pudo descargar el modelo de voz:\n{err}")
        else:
            self.dl_status.setText("Modelo descargado.")
        self._refresh_view()

    # ---------------------------------------------------------------- grabación
    def _start_recording(self, kind: str) -> None:
        """kind = "enroll" (perfil, hasta 45 s) o "test" (prueba de 5 s). Graba en un hilo y cierra el micrófono."""
        if self._recording or self._busy or not model_available():
            return
        self._recording = True
        self._rec_kind = kind
        self._speech_s = 0.0
        self._stop_evt = threading.Event()
        stop = self._stop_evt
        sig = self._sig
        app = self.app
        device = app.settings.audio.device
        max_s = ENROLL_MAX_S if kind == "enroll" else TEST_S
        target = ENROLL_TARGET_SPEECH_S if kind == "enroll" else None
        self.bar.setValue(0)
        self.rec_level.setText("")
        if kind == "enroll":
            self.rec_status.setText("Grabando… lee el texto con tu voz normal.")
        else:
            self.rec_status.setText(f"Grabando {TEST_S:.0f} s… habla con normalidad.")
        self._refresh_view()

        def progress(elapsed: float, speech_s: float, lvl: float) -> None:
            sig.rec_progress.emit(elapsed, speech_s, lvl)

        def run() -> None:
            result: dict = {}
            try:
                if app.audio.listening:          # el micrófono no se puede abrir dos veces con comodidad
                    app.audio.stop()
                audio, probs = _record(device, max_s, target, stop, progress)
                result = {"audio": audio, "probs": probs}
                if kind == "test":
                    result["score"] = app.voice_gate.score(audio)
                    result["threshold"] = app.voice_gate.threshold()
            except Exception as e:
                log.warning("Grabación de voz fallida: %s", e)
                result = {"error": str(e) or type(e).__name__}
            finally:
                try:
                    app._refresh_audio()         # vuelve a abrir la escucha si corresponde, pase lo que pase
                except Exception:
                    log.exception("No se pudo reabrir la escucha tras la grabación de voz")
                sig.rec_done.emit(kind, result)

        threading.Thread(target=run, daemon=True, name="voice-record").start()

    def _stop_recording(self) -> None:
        self._stop_evt.set()
        self.btn_stop.setEnabled(False)
        self.rec_status.setText("Terminando la grabación…")

    @_safe
    def _on_rec_progress(self, elapsed: float, speech_s: float, lvl: float) -> None:
        self._speech_s = speech_s
        self.rec_level.setText(f"{elapsed:0.0f} s · nivel {lvl:0.0f} dB")
        if self._rec_kind == "enroll":
            self.bar.setValue(min(BAR_GOAL_S, int(speech_s)))
            if speech_s >= MIN_ENROLL_SPEECH_S:
                self.rec_status.setText(f"Voz detectada: {_fmt(speech_s, 0)} s. Ya puedes pulsar «Terminar» "
                                        f"(se para sola a los {ENROLL_MAX_S:.0f} s o con {ENROLL_TARGET_SPEECH_S:.0f} s de voz).")
            else:
                self.rec_status.setText(f"Grabando… voz detectada: {_fmt(speech_s, 0)} s "
                                        f"(hacen falta al menos {MIN_ENROLL_SPEECH_S:.0f} s).")
            self.btn_stop.setEnabled(speech_s >= MIN_ENROLL_SPEECH_S and not self._stop_evt.is_set())

    @_safe
    def _on_rec_done(self, kind: str, result: dict) -> None:
        if self._closing:
            return
        self._recording = False
        self._rec_kind = ""
        self._speech_s = 0.0
        if "error" in result:
            self._set_text(self.rec_status, f"No se pudo grabar: {result['error']}", COLORS["err"])
            self._refresh_view()
            return
        if kind == "test":
            self._show_test(result)
            self._refresh_view()
            return
        self._start_analysis(result["audio"], result["probs"])

    def _show_test(self, res: dict) -> None:
        score, thr = res.get("score"), res.get("threshold")
        if score is None or thr is None:
            self._set_text(self.test_lbl, "No se oyó suficiente voz en la prueba. Repítela hablando con normalidad.",
                           COLORS["warn"])
        elif score >= thr:
            self._set_text(self.test_lbl, f"✓ Es tu voz (similitud {_fmt(score, 2)} · umbral {_fmt(thr, 2)})",
                           COLORS["ok"])
        else:
            self._set_text(self.test_lbl, f"✗ No parece tu voz (similitud {_fmt(score, 2)} · umbral {_fmt(thr, 2)})",
                           COLORS["err"])

    # ---------------------------------------------------------------- análisis del perfil
    def _start_analysis(self, audio: np.ndarray, probs: list[float]) -> None:
        self._busy = True
        self._set_text(self.rec_status, "Analizando la grabación… (unos segundos)")
        self._refresh_view()
        sig = self._sig

        def run() -> None:
            try:
                speech = speech_only(audio, probs)
                out = {"profile": build_profile(SpeakerEncoder(), speech)}
            except ValueError as e:               # poca voz o grabación inútil: se puede repetir
                out = {"error": str(e)}
            except Exception as e:
                log.exception("Análisis de la voz fallido")
                out = {"error": f"No se pudo analizar la grabación: {e}"}
            sig.analyzed.emit(out)

        threading.Thread(target=run, daemon=True, name="voice-enroll").start()

    @_safe
    def _on_analyzed(self, out: dict) -> None:
        if self._closing:
            return
        self._busy = False
        if "error" in out:
            self._pending = None
            self._set_text(self.rec_status, out["error"], COLORS["err"])
            self._refresh_view()
            return
        prof: VoiceProfile = out["profile"]
        self._pending = prof
        quality, advice = quality_label(prof.consistency)
        color = {"buena": COLORS["ok"], "aceptable": COLORS["warn"]}.get(quality, COLORS["err"])
        lines = [f"Calidad de la grabación: {quality} ({_fmt(prof.consistency, 2)})",
                 f"Segundos de voz utilizados: {_fmt(prof.speech_seconds, 0)} s · "
                 f"umbral automático: {_fmt(prof.threshold, 2)}"]
        if advice:
            lines.append(advice)
        self._set_text(self.result_lbl, "\n".join(lines), color)
        self._set_text(self.rec_status, "Grabación analizada. Guarda el perfil o repite la grabación.")
        self._refresh_view()

    def _save_profile(self) -> None:
        if self._pending is None:
            return
        try:
            self._pending.save()                  # solo el perfil; el audio nunca se escribe en disco
            self.app.voice_gate.profile()         # recarga el perfil en el verificador
        except OSError as e:
            QMessageBox.warning(self, APP_NAME, f"No se pudo guardar el perfil:\n{e}")
            return
        self._pending = None
        self._set_text(self.rec_status, "Perfil de voz guardado.", COLORS["ok"])
        self._refresh_view()
        QMessageBox.information(self, APP_NAME, "Perfil de voz guardado.\n\nActiva «Usar identificación de voz» "
                                "en Configuración → Micrófono para usarlo.")

    def _repeat(self) -> None:
        self._pending = None
        self.result_lbl.setText("")
        self.bar.setValue(0)
        self._set_text(self.rec_status, "Pulsa «Empezar grabación» y lee el texto con tu voz normal.")
        self._refresh_view()

    def _delete_profile(self) -> None:
        if QMessageBox.question(self, APP_NAME, "¿Eliminar el perfil de voz?\n\nLa identificación de voz dejará de "
                                "funcionar hasta que crees otro perfil.") != QMessageBox.Yes:
            return
        try:
            VoiceProfile.delete()
        except OSError as e:
            QMessageBox.warning(self, APP_NAME, f"No se pudo eliminar el perfil:\n{e}")
        self.app.voice_gate.profile()
        self._refresh_view()

    # ---------------------------------------------------------------- cierre
    def done(self, result: int) -> None:
        """Cierre por cualquier vía (Cerrar, Esc, botón X): si hay una grabación en marcha, el hilo la para
        y cierra el micrófono. Los resultados que lleguen después se ignoran."""
        self._closing = True
        self._stop_evt.set()
        super().done(result)

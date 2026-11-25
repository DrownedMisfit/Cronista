"""Ventana del teleprompter: lectura, edición del texto, velocidad, tipo y tamaño de letra, espaciado, espejo y
pantalla completa.

Los ajustes se guardan en settings.teleprompter (settings.json) y el texto en teleprompter.txt.
"""
from __future__ import annotations

import logging
import zlib
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import (QCloseEvent, QCursor, QDragEnterEvent, QDropEvent, QFont, QFontMetrics, QGuiApplication,
                           QKeySequence,
                           QMouseEvent, QShortcut, QShowEvent, QTextCursor)
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
                               QPlainTextEdit, QPushButton, QSlider, QSpinBox, QStackedWidget, QVBoxLayout,
                               QWidget)

from cronista import APP_NAME, __version__
from cronista.core import paths
from cronista.core.settings import Settings
from cronista.teleprompter import diseno, ensayo, fonts
from cronista.teleprompter import text as tp_text
from cronista.teleprompter.text import (ANGLE_MAX, ANGLE_MIN, FONT_MAX, FONT_MIN, SPACING_MAX, SPACING_MIN,
                                        SPACING_STEP, SPEED_MAX, SPEED_MIN)
from cronista.teleprompter.view import create_view

log = logging.getLogger(__name__)

PAGE_READ = 0
PAGE_EDIT = 1
FILE_FILTER = "Textos (*.txt *.md *.docx);;Todos los archivos (*)"
PLACEHOLDER = "Escribe o pega aquí tu texto, o abre un archivo (.txt, .md o .docx)…"
HINT_TEXT = "Esc: salir de pantalla completa · Espacio: iniciar/pausar · A: ajustes"
IDLE_MS = 3000             # pantalla completa leyendo: sin mover el ratón, se oculta la barra y el cursor
HINT_MS = 3000             # cuánto tiempo se ve el aviso al entrar en pantalla completa
SETTINGS_SAVE_MS = 800     # los ajustes se guardan tras una pausa, no en cada paso del control
TEXT_SAVE_MS = 1000
FONT_STEP = 4              # puntos por pulsación de + / -
LINE_STEP = 3              # líneas por pulsación de RePág / AvPág
SMART_TIP = ("Modo Smart: el texto sigue tu voz. Avanza mientras lees en voz alta y se para si te callas o "
             "improvisas. Todo en tu PC, sin internet (la primera vez baja un modelo de voz de unos 40 MB).")
ANCHO_INICIAL = 1700       # px: la barra de abajo y el panel de ajustes abierto
REVISAR_OPCIONES = (("apagado", "Apagado"), ("silencioso", "Silencioso"), ("discreto", "Discreto"),
                    ("atento", "Atento"))
FPS_OPCIONES =((0, "Automático"), (60, "60 fps"), (120, "120 fps"), (144, "144 fps"), (240, "240 fps"))
CUENTA_OPCIONES = ((0, "Sin cuenta atrás"), (3, "3 s"), (5, "5 s"))


def _texto_angulo(grados: int) -> str:
    if grados > 0:
        return f"{grados}° desde arriba"
    if grados < 0:
        return f"{-grados}° desde abajo"
    return "0° (de frente)"


def formato_tiempo(segundos: float) -> str:
    """m:ss, o h:mm:ss si pasa de una hora."""
    total = max(0, int(round(segundos)))
    horas, resto = divmod(total, 3600)
    minutos, seg = divmod(resto, 60)
    if horas:
        return f"{horas}:{minutos:02d}:{seg:02d}"
    return f"{minutos}:{seg:02d}"


class _EtiquetaDobleClic(QLabel):
    """Etiqueta que avisa del doble clic (el del ángulo vuelve a 0°)."""

    doubleClicked = Signal()

    def mouseDoubleClickEvent(self, event) -> None:
        self.doubleClicked.emit()
        super().mouseDoubleClickEvent(event)


class _Canvas(QWidget):
    """Contenedor: la vista ocupa todo el hueco y la barra va encima, abajo.

    Al editar, la barra reserva su franja para que el editor no quede tapado. Al leer, la barra se superpone:
    así, al ocultarla en pantalla completa, el texto y la línea de lectura no se mueven.
    """

    def __init__(self, stack: QWidget, bar: QWidget, aviso: QWidget, cuenta: QWidget,
                 panel: QWidget | None = None) -> None:
        super().__init__()
        self._stack = stack
        self._bar = bar
        self._panel = panel
        self._aviso = aviso
        self._cuenta = cuenta
        self._reserve_bar = False
        self.bar_min = None           # callable() → ancho mínimo de la barra ya compactada (lo da la ventana)
        self.on_place = None          # callable(alto_barra): la ventana avisa a la vista del hueco que tapa la barra
        for hijo in (stack, bar, aviso, cuenta) + ((panel,) if panel is not None else ()):
            hijo.setParent(self)    # setParent oculta el widget: hay que volver a mostrarlo
        stack.show()
        bar.show()
        aviso.hide()
        cuenta.hide()
        self.setMouseTracking(True)
        self.place()

    def set_reserve_bar(self, on: bool) -> None:
        self._reserve_bar = on
        self.place()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.place()

    def place(self) -> None:
        ancho_total, alto = self.width(), self.height()
        # El panel de ajustes (si está abierto) ocupa la derecha de arriba abajo; el texto y la barra, el resto.
        lateral = self._panel.width() if self._panel is not None and not self._panel.isHidden() else 0
        alto_barra = self._bar.maximumHeight() if self._bar.maximumHeight() < 10000 else self._bar.sizeHint().height()
        minimo = self.bar_min() if self.bar_min is not None else 0
        if lateral and ancho_total - lateral < minimo:
            # Ventana estrecha: si el panel estrechara la barra, no cabría ni compactada. El panel va ENCIMA del
            # texto (sin tapar la barra, que sigue entera con su botón de Ajustes para cerrarlo).
            ancho = ancho_total
            self._panel.setGeometry(ancho_total - lateral, 0, lateral, alto - alto_barra)
        else:
            ancho = max(1, ancho_total - lateral)
            if lateral:
                self._panel.setGeometry(ancho, 0, lateral, alto)
        reserva = alto_barra if self._reserve_bar else 0
        self._stack.setGeometry(0, 0, ancho, alto - reserva)
        self._bar.setGeometry(0, alto - alto_barra, ancho, alto_barra)
        if self.on_place is not None:
            self.on_place(alto_barra)     # siempre el alto de la barra, también con ella oculta (pantalla completa)
        self._aviso.adjustSize()
        self._aviso.move((ancho - self._aviso.width()) // 2, 16)
        self._aviso.raise_()
        self._bar.raise_()
        if lateral:
            self._panel.raise_()
        # La cuenta atrás va centrada sobre el texto (en el hueco de la vista, sin contar la barra al editar)
        self._cuenta.adjustSize()
        self._cuenta.move((ancho - self._cuenta.width()) // 2, (alto - reserva - self._cuenta.height()) // 2)
        self._cuenta.raise_()


class TeleprompterWindow(QMainWindow):
    """Ventana principal del teleprompter."""

    def __init__(self, settings: Settings, text_path: Path | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._tp = settings.teleprompter
        self._text_path = Path(text_path) if text_path is not None else paths.teleprompter_text_path()
        self._editing = False
        self._last_full = False
        self._puede_guardar_texto = True   # False si el texto guardado no se pudo leer (no lo sobrescribimos)
        self._reanclar_marca = False       # True tras restaurar la bandera al arrancar (se ancla al mostrarse)

        self.setWindowTitle(f"{APP_NAME} {__version__} — Teleprompter")
        pantalla = QGuiApplication.primaryScreen()
        disponible = pantalla.availableGeometry().width() if pantalla is not None else ANCHO_INICIAL
        self.resize(min(ANCHO_INICIAL, disponible), 720)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)

        self._timer_ajustes = self._un_disparo(SETTINGS_SAVE_MS, self._guardar_ajustes)
        self._timer_texto = self._un_disparo(TEXT_SAVE_MS, self._guardar_texto)
        self._timer_ocultar = self._un_disparo(IDLE_MS, self._on_idle)
        self._timer_aviso = self._un_disparo(HINT_MS, lambda: self._aviso.hide())
        self._timer_fps = QTimer(self)          # refresca la cifra de fps cada segundo, solo mientras se lee
        self._timer_fps.setInterval(1000)
        self._cuenta_timer = QTimer(self)       # cuenta atrás: un paso por segundo
        self._cuenta_timer.setInterval(1000)
        self._cuenta_timer.timeout.connect(self._tick_cuenta)
        self._cuenta_restante = 0
        self._timer_tiempo = QTimer(self)       # indicador de tiempo: se refresca solo mientras la ventana se ve
        self._timer_tiempo.setInterval(500)
        self._timer_tiempo.timeout.connect(self._actualizar_tiempo)

        fonts.load_bundled_fonts()          # antes de crear la vista y el desplegable: las letras de fonts/
        self._view = create_view(self._tp.use_gpu)   # tarjeta gráfica si hay OpenGL; si no, el procesador
        self._editor = QPlainTextEdit()
        self._editor.setPlaceholderText(PLACEHOLDER)
        self._editor.setFont(QFont("Segoe UI", 13))
        self._editor.setStyleSheet("QPlainTextEdit { font-size: 13pt; }")   # el tema global fija 10 pt
        # Las caídas de archivos van a la ventana (abrir), no al editor, que insertaría la ruta como texto
        self._editor.setAcceptDrops(False)
        self._editor.viewport().setAcceptDrops(False)
        self._stack = QStackedWidget()
        self._stack.setMouseTracking(True)
        self._stack.addWidget(self._view)       # PAGE_READ
        self._stack.addWidget(self._editor)     # PAGE_EDIT

        self._build_bar()
        self._aviso = QLabel(HINT_TEXT)
        self._aviso.setStyleSheet("QLabel { background: rgba(0, 0, 0, 170); color: #f2f2f2; "
                                  "padding: 10px 16px; border-radius: 8px; font-size: 12pt; }")
        self._aviso.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._cuenta_label = QLabel()          # número grande de la cuenta atrás, encima del texto
        self._cuenta_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cuenta_label.setStyleSheet("QLabel { background: rgba(0, 0, 0, 150); color: #ffffff; "
                                         "font-size: 150pt; font-weight: bold; padding: 4px 56px; "
                                         "border-radius: 28px; }")
        self._cuenta_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._canvas = _Canvas(self._stack, self._bar, self._aviso, self._cuenta_label, self._ajustes)
        self._ajustes.setVisible(self._tp.settings_panel)
        self._btn_ajustes.setChecked(self._tp.settings_panel)
        self.setCentralWidget(self._canvas)
        self._canvas.on_place = self._view.set_bottom_inset     # la vista deja libre el hueco de la barra
        self._canvas.bar_min = self._bar_minimo
        self.setMinimumWidth(self._bar_minimo() + 2)            # la barra, compactada, siempre cabe entera
        self._canvas.place()
        # La barra de estado se crea ya (si naciera con el primer mensaje, el texto daría un salto) y se oculta
        # en pantalla completa.
        self.statusBar()
        self.statusBar().setStyleSheet(f"QStatusBar {{ background: {diseno.FONDO_HONDO}; color: {diseno.TENUE}; "
                                       f"font-family: '{diseno.familia()}'; font-size: {diseno.LETRA_P}px; }} "
                                       "QStatusBar::item { border: none; }")

        # Valores de los controles y de la vista, tomados de los ajustes (los rangos los limita la vista)
        self._view.set_font_size(self._tp.font_size)
        self._view.set_speed(self._tp.speed)
        self._view.set_mirror(self._tp.mirror)
        self._view.set_centered(self._tp.center_text)
        self._view.set_phrase_breaks(self._tp.phrase_breaks)
        self._view.set_guide_centered(self._tp.guide_center)
        self._view.set_margin_pct(self._tp.margin_pct)
        familia = fonts.resolve_family(self._tp.font_family)
        self._tp.font_family = familia      # si la guardada ya no está instalada, se usa la de Windows (sin guardar)
        self._view.set_font_family(familia)
        self._view.set_letter_spacing(self._tp.letter_spacing)
        self._tp.letter_spacing = self._view.letter_spacing()
        self._spin.setValue(self._view.font_size())
        self._combo_fuente.setCurrentIndex(self._combo_fuente.findData(familia))
        self._estilo_combo_fuente(familia)
        self._spin_espaciado.setValue(self._view.letter_spacing())
        self._slider.setValue(self._view.speed())
        self._lbl_velocidad.setText(str(self._view.speed()))
        self._btn_espejo.setChecked(self._view.is_mirrored())
        self._btn_centrar.setChecked(self._view.is_centered())
        self._btn_frases.setChecked(self._view.has_phrase_breaks())
        self._btn_guia.setChecked(self._view.is_guide_centered())
        self._view.set_fps(self._tp.fps)
        self._tp.fps = self._view.fps()
        indice = self._combo_fps.findData(self._tp.fps)
        if indice < 0:                       # un valor guardado que no está en la lista: se añade tal cual
            self._combo_fps.addItem(f"{self._tp.fps} fps", self._tp.fps)
            indice = self._combo_fps.count() - 1
        self._combo_fps.setCurrentIndex(indice)
        self._actualizar_fps()
        self._tp.countdown_s = max(0, int(self._tp.countdown_s))
        indice = self._combo_cuenta.findData(self._tp.countdown_s)
        if indice < 0:                       # un valor guardado que no está en la lista: se añade tal cual
            self._combo_cuenta.addItem(f"{self._tp.countdown_s} s", self._tp.countdown_s)
            indice = self._combo_cuenta.count() - 1
        self._combo_cuenta.setCurrentIndex(indice)
        if self._tp.review_level not in ensayo.REVIEW_LEVELS:
            self._tp.review_level = "discreto"
        self._combo_revisar.setCurrentIndex(self._combo_revisar.findData(self._tp.review_level))
        self._view.set_view_angle(self._tp.view_angle)
        self._tp.view_angle = self._view.view_angle()
        self._slider_angulo.setValue(self._view.view_angle())
        self._lbl_angulo.setText(_texto_angulo(self._view.view_angle()))

        texto = self._leer_texto_guardado()
        self._editor.setPlainText(texto)
        self._view.set_text(texto)
        # Banderas de la sesión anterior: solo si el texto sigue siendo el mismo (la huella crc32 lo comprueba).
        # Van antes de conectar las señales: no son marcas nuevas, así que no avisan ni guardan nada.
        if texto.strip() and self._tp.bookmark_crc == zlib.crc32(texto.encode("utf-8")):
            self._view.set_marks(self._tp.bookmarks, self._tp.bookmark_pos)
            if self._view.pause_mark() >= 0:
                self._view.jump_to_mark(self._view.pause_mark())
                self._reanclar_marca = True       # al mostrarse, con el ancho real, se vuelve a llevar a la marca
                self._on_progreso(self._view.progress())
        else:
            self._tp.bookmark_pos = -1
            self._tp.bookmarks = []
            self._tp.bookmark_crc = 0
        self._view.set_progress_bar(self._tp.show_progress_bar)
        self._btn_barra.setChecked(self._tp.show_progress_bar)
        self._editing = not texto.strip()     # sin texto, arranca en la página de edición
        self._apply_editing_state()

        self._follower = None             # modo Smart: dónde va la voz en el guion
        self._follower_text = ""
        self._oyente = None               # VoiceListener (micrófono + Vosk), solo mientras se lee
        self._descarga = None
        self._camara_geo = None           # geometría normal, para volver del modo cámara
        self._view.on_manual_move = self._smart_reubicar
        self._oido_ultimo = 0.0            # cuándo llegó voz por última vez (para avisar «no te oigo»)
        self._oido_palabras = ""
        self._sesion = None               # ensayo.SessionLog: tomas malas y posibles errores de esta lectura
        self._oido_frase: list[str] = []  # palabras oídas desde que empezó la frase actual (para comparar)
        self._frase_actual: int | None = None
        self._revisor = None              # ensayo.ReadingChecker del texto actual (sabe lo bien que está oyendo)
        self._revisor_texto = ""
        self._llenar_micros()
        self._connect()
        if self._tp.smart:                # se recuerda; si falta el modelo, al pulsar Iniciar se ofrece bajarlo
            self._btn_smart.blockSignals(True)
            self._btn_smart.setChecked(True)
            self._btn_smart.blockSignals(False)
            self._view.set_follow(True)
            self._lbl_oido.setVisible(True)
            self._lbl_oido.setText("pulsa Iniciar y lee en voz alta")
        self._actualizar_boton_borrar()
        self._install_shortcuts()

    # ---- construcción -------------------------------------------------------
    def _un_disparo(self, ms: int, slot) -> QTimer:
        t = QTimer(self)
        t.setSingleShot(True)
        t.setInterval(ms)
        t.timeout.connect(slot)
        return t

    def _build_bar(self) -> None:
        """Abajo, una sola barra (lo de leer); a la derecha, el panel de ajustes por secciones (se abre y se cierra con
        el botón de Ajustes o la tecla A). Mismo sistema de diseño que Reclaimer (teleprompter/diseno.py)."""
        D = diseno
        estilo = D.qss()
        self._bar = QFrame()
        self._bar.setObjectName("controlBar")
        self._bar.setStyleSheet(estilo)
        self._bar.setMouseTracking(True)
        fila = QHBoxLayout(self._bar)
        fila.setContentsMargins(D.M3, D.M2, D.M3, D.M2)
        fila.setSpacing(D.M1)

        self._btn_editar = D.boton("Editar texto", "square-pen", "Editar o pegar el texto (Ctrl+E)", True, "icono")
        self._btn_abrir = D.boton("Abrir", "folder-open", "Abrir un archivo .txt, .md o .docx (Ctrl+O). También "
                                                          "puedes arrastrarlo a la ventana.", tipo="icono")
        self._btn_borrar = D.boton("Borrar texto", "trash-2", "Borrar todo el texto para escribir o pegar otro "
                                                              "(pide confirmación).", tipo="icono")
        self._btn_inicio = D.boton("Al principio", "skip-back", "Volver al principio del texto (Inicio)", tipo="icono")
        self._btn_play = D.boton("Iniciar", "play", "Iniciar o pausar (Espacio). Al iniciar hay una cuenta atrás "
                                                    "(ajuste «Cuenta atrás»); pulsar otra vez durante ella la cancela. "
                                                    "Al pausar queda una bandera roja donde paraste y Cronista la "
                                                    "recuerda para la próxima vez.", tipo="principal")
        # (con la letra de la barra, que se aplica después con la hoja de estilo: así «Cancelar» cabe siempre)
        self._fuente_barra = QFont(D.familia())
        self._fuente_barra.setPixelSize(D.LETRA)
        negrita = QFont(self._fuente_barra)
        negrita.setWeight(QFont.Weight.DemiBold)
        self._btn_play.setMinimumWidth(QFontMetrics(negrita).horizontalAdvance("Cancelar") + D.ICONO + 52)
        self._btn_smart = D.boton("Smart", "mic", SMART_TIP, checkable=True)
        self._lbl_oido = D.EtiquetaElidida("", 80)
        self._lbl_oido.setObjectName("dDato")
        self._lbl_oido.setMaximumWidth(320)
        self._lbl_oido.setToolTip("Modo Smart: el volumen que llega del micrófono y las últimas palabras que entendí. "
                                  "Si no se mueve al hablar, elige otro micrófono en Ajustes → Smart.")
        self._lbl_oido.hide()
        self._btn_marca = D.boton("Ir a la marca", "flag", "Ir a la siguiente bandera roja (B). Si hay varias, cada "
                                                           "pulsación va a la siguiente.", tipo="icono")
        self._btn_quitar_marcas = D.boton("Quitar marcas", "x", "Quitar todas las banderas rojas. (Para quitar una "
                                                                "sola, haz clic sobre ella.)", tipo="icono")
        self._btn_revisar = D.boton("Revisar", "triangle-alert", "Frases donde Smart cree que faltó algo al leer. Clic: "
                                    "ir a la siguiente. Es una sospecha: el reconocedor también se equivoca. Se "
                                    "configura en Ajustes → Smart → Revisar lectura.", tipo="aviso")
        self._btn_revisar.hide()

        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(SPEED_MIN, SPEED_MAX)
        self._slider.setFixedWidth(150)
        self._slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._slider.setToolTip("Velocidad del desplazamiento (flecha arriba: más rápido; abajo: más lento)")
        self._lbl_velocidad = QLabel()
        self._lbl_velocidad.setObjectName("dDato")
        self._lbl_velocidad.setFixedWidth(self._lbl_velocidad.fontMetrics().horizontalAdvance("30") + 6)
        self._lbl_velocidad.setToolTip("Velocidad actual (1 a 30)")
        self._lbl_tiempo = QLabel("—")
        self._lbl_tiempo.setObjectName("dDato")
        self._lbl_tiempo.setToolTip("Tiempo leído / duración total del texto a esta velocidad, y lo que falta")
        self._lbl_progreso = QLabel("0 %")
        self._lbl_progreso.setObjectName("dDato")
        self._lbl_progreso.setToolTip("Progreso de la lectura")
        self._btn_camara = D.boton("Junto a la cámara", "camera", "Junto a la cámara (K): la ventana se vuelve una "
                                   "franja siempre encima, arriba de la pantalla, bajo la webcam: lees mirando casi al "
                                   "lente. Arrastra la barra para moverla y la esquina para cambiar su tamaño. K o Esc: "
                                   "volver.", checkable=True, tipo="icono")
        self._btn_pantalla = D.boton("Pantalla completa", "maximize", "Pantalla completa (F11). En pantalla completa, "
                                                                      "Esc sale.", tipo="icono")
        self._btn_ajustes = D.boton("Ajustes", "sliders-horizontal", "Ajustes (A): letra, lectura, Smart y pantalla.",
                                    checkable=True, tipo="icono")
        from PySide6.QtWidgets import QSizeGrip
        self._grip = QSizeGrip(self._bar)
        self._grip.setToolTip("Arrastra para cambiar el tamaño de la franja")
        self._grip.hide()

        for w in (self._btn_editar, self._btn_abrir, self._btn_borrar):
            fila.addWidget(w)
        fila.addSpacing(D.M2)
        fila.addWidget(D.separador())
        fila.addSpacing(D.M2)
        for w in (self._btn_inicio, self._btn_play, self._btn_smart):
            fila.addWidget(w)
        fila.addWidget(self._lbl_oido, 1)
        fila.addSpacing(D.M2)
        fila.addWidget(D.separador())
        fila.addSpacing(D.M2)
        for w in (self._btn_marca, self._btn_quitar_marcas, self._btn_revisar):
            fila.addWidget(w)
        fila.addStretch(1)
        self._lbl_vel_titulo = D.etiqueta("Velocidad", None)
        fila.addWidget(self._lbl_vel_titulo)
        fila.addWidget(self._slider)
        fila.addWidget(self._lbl_velocidad)
        fila.addSpacing(D.M2)
        fila.addWidget(self._lbl_tiempo)
        fila.addSpacing(D.M2)
        fila.addWidget(self._lbl_progreso)
        fila.addSpacing(D.M2)
        fila.addWidget(D.separador())
        fila.addSpacing(D.M2)
        for w in (self._btn_camara, self._btn_pantalla, self._btn_ajustes):
            fila.addWidget(w)
        fila.addWidget(self._grip, 0, Qt.AlignmentFlag.AlignBottom)
        self._bar.setFixedHeight(D.ALTO_BARRA)
        self._bar_nivel = 0
        self._bar.installEventFilter(self)       # al cambiar de ancho, se compacta (nunca se corta nada)

        # ---- panel de ajustes ----------------------------------------------------------------------------------
        self._combo_fuente = diseno.ComboElidido()
        self._combo_fuente.setMaxVisibleItems(len(fonts.FONT_OPTIONS))     # que se vean todas sin desplazar la lista
        self._combo_fuente.setFocusPolicy(Qt.FocusPolicy.NoFocus)   # los atajos (L) siguen en la vista
        self._combo_fuente.setToolTip("Tipo de letra del texto (L: siguiente, Mayús+L: anterior)")
        for opt in fonts.available_options():
            self._combo_fuente.addItem(opt.label, opt.family)          # dato de usuario: la familia de Qt
            i = self._combo_fuente.count() - 1
            self._combo_fuente.setItemData(i, opt.note, Qt.ItemDataRole.ToolTipRole)
            self._combo_fuente.setItemData(i, QFont(opt.family, 12), Qt.ItemDataRole.FontRole)   # su nombre en su letra
        self._spin = QSpinBox()
        self._spin.setRange(FONT_MIN, FONT_MAX)
        self._spin.setSingleStep(FONT_STEP)
        self._spin.setSuffix(" pt")
        self._spin.setToolTip("Tamaño de la letra (+ y − en el teclado, o Ctrl + rueda del ratón)")
        self._spin_espaciado = QSpinBox()
        self._spin_espaciado.setRange(SPACING_MIN, SPACING_MAX)
        self._spin_espaciado.setSingleStep(SPACING_STEP)
        self._spin_espaciado.setSuffix(" %")
        self._spin_espaciado.setToolTip("Separación extra entre letras. A algunas personas les resulta más cómoda: "
                                        "pruébala.")
        self._btn_centrar = D.boton("Centrar líneas", "align-center", "Centra cada línea a lo ancho de la pantalla (C)",
                                    checkable=True)
        self._btn_frases = D.boton("Líneas por frases", "scissors", "Corta las líneas al terminar cada frase y en las "
                                   "comas, para leer en voz alta con pausas naturales. Solo cambia cómo se ve: tu "
                                   "texto no se modifica (F)", checkable=True)
        self._combo_cuenta = diseno.ComboElidido()
        self._combo_cuenta.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        for segundos, nombre in CUENTA_OPCIONES:
            self._combo_cuenta.addItem(nombre, segundos)
        self._combo_cuenta.setToolTip("Cuenta atrás antes de empezar a leer (al pulsar Iniciar o Espacio). "
                                      "Pulsar otra vez durante la cuenta la cancela. Pausar es inmediato.")
        self._combo_fps = diseno.ComboElidido()
        self._combo_fps.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        for valor, nombre in FPS_OPCIONES:
            self._combo_fps.addItem(nombre, valor)
        self._combo_fps.setToolTip("Fotogramas por segundo al desplazar el texto: más = más fluido, pero gasta algo "
                                   "más. Automático usa los Hz de tu monitor y baja solo si el equipo no da para más.")
        self._lbl_fps = QLabel()
        self._lbl_fps.setObjectName("dDato")
        self._lbl_fps.setToolTip("Fotogramas por segundo que se están usando")
        self._btn_guia = D.boton("Lectura al centro", "rows-3", "Pone la línea de lectura a media altura de la "
                                 "pantalla, a la altura del objetivo de la cámara si usas el cristal", checkable=True)
        self._btn_barra = D.boton("Barra de avance", "gauge", "Muestra a la derecha por dónde vas del texto. Púlsala "
                                                              "o arrástrala para saltar.", checkable=True)
        self._combo_mic = diseno.ComboElidido()
        self._combo_mic.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._combo_mic.setToolTip("Micrófono que escucha el modo Smart")
        self._combo_mic.addItem("Predeterminado de Windows", "")
        self._combo_revisar = diseno.ComboElidido()
        self._combo_revisar.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        for nivel, nombre in REVISAR_OPCIONES:
            self._combo_revisar.addItem(nombre, nivel)
        self._combo_revisar.setToolTip("Con Smart, Cronista compara lo que dices con el guion. Silencioso: solo al "
                                       "pausar. Discreto: además, una rayita ámbar en la barra de avance. Atento: "
                                       "además, un punto junto a la frase. Nunca te para ni muestra ventanas.")
        self._slider_angulo = QSlider(Qt.Orientation.Horizontal)
        self._slider_angulo.setRange(ANGLE_MIN, ANGLE_MAX)
        self._slider_angulo.setPageStep(5)
        self._slider_angulo.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        ayuda_angulo = ("Si miras la pantalla desde arriba (o desde abajo), inclina el texto para que desde tu "
                        "posición se vea recto, sin mover el monitor. Hacia la derecha: estás por encima; hacia "
                        "la izquierda: por debajo. Doble clic en el número: volver a 0°.")
        self._slider_angulo.setToolTip(ayuda_angulo)
        self._lbl_angulo = _EtiquetaDobleClic()
        self._lbl_angulo.setObjectName("dDato")
        self._lbl_angulo.setToolTip(ayuda_angulo)
        self._btn_espejo = D.boton("Espejo", "flip-horizontal-2", "Refleja el texto en horizontal para el cristal del "
                                                                  "teleprompter (M)", checkable=True)

        for combo in (self._combo_fuente, self._combo_cuenta, self._combo_fps, self._combo_mic, self._combo_revisar):
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(8)
            combo.view().setMinimumWidth(300)        # la lista desplegada, con los nombres enteros
            combo.setProperty("ayuda", combo.toolTip())
        contenido = QWidget()
        contenido.setObjectName("ajustesContenido")
        col = QVBoxLayout(contenido)
        col.setContentsMargins(D.M3, D.M3, D.M3, D.M3)
        col.setSpacing(D.M1 + 2)

        def seccion(nombre: str) -> None:
            if col.count():
                col.addSpacing(D.M2)
            col.addWidget(D.linea())
            col.addWidget(D.titulo(nombre))

        def fila_ajuste(nombre: str, *widgets) -> None:
            h = QHBoxLayout()
            h.setSpacing(D.M2)
            h.addWidget(D.etiqueta(nombre))
            for w in widgets:
                h.addWidget(w, 1 if w is widgets[0] else 0)
            col.addLayout(h)

        def debajo(w: QLabel) -> None:
            """Un dato bajo el control de la fila de arriba, alineado con él."""
            w.setWordWrap(True)
            h = QHBoxLayout()
            h.setSpacing(D.M2)
            h.addSpacing(D.ANCHO_ETIQUETA + D.M2)
            h.addWidget(w, 1)
            col.addLayout(h)

        def interruptores(*botones) -> None:
            from PySide6.QtWidgets import QGridLayout
            g = QGridLayout()
            g.setContentsMargins(0, 2, 0, 2)
            g.setSpacing(D.M1)
            for i, b in enumerate(botones):
                g.addWidget(b, i // 2, i % 2)
            col.addLayout(g)

        seccion("Texto")
        fila_ajuste("Tipo de letra", self._combo_fuente)
        fila_ajuste("Tamaño", self._spin)
        fila_ajuste("Espaciado", self._spin_espaciado)
        interruptores(self._btn_centrar, self._btn_frases)
        seccion("Lectura")
        fila_ajuste("Cuenta atrás", self._combo_cuenta)
        fila_ajuste("Fluidez", self._combo_fps)
        debajo(self._lbl_fps)
        interruptores(self._btn_guia, self._btn_barra)
        seccion("Smart")
        fila_ajuste("Micrófono", self._combo_mic)
        fila_ajuste("Revisar lectura", self._combo_revisar)
        seccion("Pantalla")
        fila_ajuste("Ángulo de visión", self._slider_angulo)
        debajo(self._lbl_angulo)
        interruptores(self._btn_espejo)
        col.addStretch(1)
        from cronista import SUPPORT_URL
        if SUPPORT_URL:
            b_apoyo = D.boton("Apoyar", "heart", "Cronista es gratis. Si te sirve, puedes invitarme un café (se abre "
                                                 "la página en tu navegador).", tipo="plano")
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices
            b_apoyo.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(SUPPORT_URL)))
            col.addWidget(b_apoyo, 0, Qt.AlignmentFlag.AlignLeft)

        from PySide6.QtWidgets import QScrollArea
        self._ajustes = QScrollArea()
        self._ajustes.setObjectName("ajustesScroll")
        self._ajustes.setStyleSheet(estilo)
        self._ajustes.setWidget(contenido)
        self._ajustes.setWidgetResizable(True)
        self._ajustes.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._ajustes.setFrameShape(QFrame.Shape.NoFrame)
        # Ancho: lo que pide el contenido (nada se corta), como mínimo 300 px; más la barra vertical si hiciera falta.
        self._ajustes.setFixedWidth(max(D.ANCHO_PANEL, contenido.minimumSizeHint().width() + 10))

    def _connect(self) -> None:
        self._btn_editar.toggled.connect(self._set_editing)
        self._btn_ajustes.toggled.connect(self._on_ajustes)
        self._btn_abrir.clicked.connect(lambda: self._abrir_dialogo())
        self._btn_play.clicked.connect(lambda: self._pulsar_play())
        self._btn_inicio.clicked.connect(lambda: self._rebobinar())
        self._combo_cuenta.currentIndexChanged.connect(self._on_cuenta)
        self._combo_revisar.currentIndexChanged.connect(self._on_revisar)
        self._btn_revisar.clicked.connect(lambda: self._ir_a_revisar())
        self._btn_smart.toggled.connect(self._on_smart)
        self._combo_mic.currentIndexChanged.connect(self._on_mic)
        self._btn_camara.toggled.connect(self._on_camara)
        self._btn_marca.clicked.connect(lambda: self._ir_a_marca())
        self._btn_quitar_marcas.clicked.connect(lambda: self._quitar_marcas())
        self._btn_borrar.clicked.connect(lambda: self._borrar_texto())
        self._slider.valueChanged.connect(self._on_velocidad)
        self._spin.valueChanged.connect(self._on_letra)
        self._spin.editingFinished.connect(lambda: self._devolver_foco())
        self._combo_fuente.currentIndexChanged.connect(self._on_fuente)
        self._spin_espaciado.valueChanged.connect(self._on_espaciado)
        self._spin_espaciado.editingFinished.connect(lambda: self._devolver_foco())
        self._btn_espejo.toggled.connect(self._on_espejo)
        self._btn_centrar.toggled.connect(self._on_centrar)
        self._btn_frases.toggled.connect(self._on_frases)
        self._btn_guia.toggled.connect(self._on_guia)
        self._btn_barra.toggled.connect(self._on_barra_avance)
        self._slider_angulo.valueChanged.connect(self._on_angulo)
        self._combo_fps.currentIndexChanged.connect(self._on_fps)
        self._view.playingChanged.connect(self._on_playing_fps)
        self._timer_fps.timeout.connect(self._actualizar_fps)
        self._lbl_angulo.doubleClicked.connect(lambda: self._slider_angulo.setValue(0))
        self._btn_pantalla.clicked.connect(lambda: self._alternar_pantalla())
        self._view.playingChanged.connect(self._on_playing)
        self._view.progressChanged.connect(self._on_progreso)
        self._view.finished.connect(lambda: self.statusBar().showMessage("Has llegado al final del texto.", 5000))
        self._view.fontSizeChanged.connect(self._on_letra_desde_vista)
        self._view.marksChanged.connect(self._on_marcas)
        self._editor.textChanged.connect(self._on_editor_changed)
        self._editor.textChanged.connect(self._actualizar_boton_borrar)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        # Al cambiar de ancho, o cuando algo de la barra aparece o cambia de texto («para revisar», lo que oye Smart),
        # se recalcula cuánto cabe. El cálculo no prueba niveles: así no entra en bucle con sus propios cambios.
        if obj is getattr(self, "_bar", None) and event.type() in (QEvent.Type.Resize, QEvent.Type.LayoutRequest):
            self._compactar_barra(self._bar.width())
        return super().eventFilter(obj, event)

    def _ahorro_nivel(self, nivel: int) -> int:
        """Cuántos px se ganan al pasar del nivel anterior a este (medido con la letra de la barra)."""
        fm = QFontMetrics(self._fuente_barra)
        if nivel == 1:
            return fm.horizontalAdvance("Velocidad") + fm.horizontalAdvance("100 %") + 2 * diseno.M2 + 8
        if nivel == 2:
            revisar = (fm.horizontalAdvance(" para revisar") if self._btn_revisar.isVisibleTo(self._bar) else 0)
            return fm.horizontalAdvance("Smart") + 12 + revisar + 50
        if nivel == 3:
            return fm.horizontalAdvance(self._lbl_tiempo.text() or "0:00 / 0:00 · faltan 0:00") + diseno.M2 + 80
        return 0

    def _bar_minimo(self) -> int:
        """Ancho mínimo de la barra en su nivel más compacto (sea cual sea el nivel en que esté ahora)."""
        actual = getattr(self, "_bar_nivel", 0)
        completo = self._bar.minimumSizeHint().width() + sum(self._ahorro_nivel(n) for n in range(1, actual + 1))
        return completo - sum(self._ahorro_nivel(n) for n in range(1, 4))

    def _compactar_barra(self, ancho: int) -> None:
        """Si la barra no cabe, se compacta por pasos: 1) sin «Velocidad» ni el %; 2) Smart y «para revisar» solo
        con icono y deslizador más corto; 3) sin el tiempo ni lo que oye Smart. Lo quitado sigue en los tooltips."""
        if ancho <= 0:
            return
        actual = getattr(self, "_bar_nivel", 0)
        completo = self._bar.minimumSizeHint().width() + sum(self._ahorro_nivel(n) for n in range(1, actual + 1))
        nivel, necesario = 0, completo
        while nivel < 3 and necesario > ancho:
            nivel += 1
            necesario -= self._ahorro_nivel(nivel)
        self._aplicar_nivel_barra(nivel)

    def _aplicar_nivel_barra(self, nivel: int) -> None:
        if nivel == getattr(self, "_bar_nivel", 0):
            return
        self._bar_nivel = nivel
        self._lbl_vel_titulo.setVisible(nivel < 1)
        self._lbl_progreso.setVisible(nivel < 1)
        solo_icono = nivel >= 2
        self._btn_smart.setObjectName("dIcono" if solo_icono else "")
        self._btn_smart.style().unpolish(self._btn_smart)
        self._btn_smart.style().polish(self._btn_smart)
        self._btn_smart.setText("" if solo_icono else "Smart")
        if solo_icono:
            self._btn_smart.setFixedWidth(diseno.ALTO_CONTROL + 2)
        else:
            self._btn_smart.setMinimumWidth(0)
            self._btn_smart.setMaximumWidth(16777215)
        self._texto_revisar()
        self._slider.setFixedWidth(100 if solo_icono else 150)
        self._lbl_tiempo.setVisible(nivel < 3)
        self._lbl_oido.setMinimumWidth(0 if nivel >= 3 else 80)
        self._lbl_oido.setMaximumWidth(0 if nivel >= 3 else 320)

    def _texto_revisar(self) -> None:
        n = getattr(self, "_revisar_n", 0)
        self._btn_revisar.setText(str(n) if getattr(self, "_bar_nivel", 0) >= 2 else f"{n} para revisar")

    def _on_ajustes(self, on: bool) -> None:
        """Abre o cierra el panel de ajustes. El texto se vuelve a maquetar al nuevo ancho sin perder la frase."""
        self._tp.settings_panel = bool(on)
        self._guardar_ajustes_pronto()
        self._ajustes.setVisible(on)
        self._canvas.place()
        self._devolver_foco()

    def _install_shortcuts(self) -> None:
        """Atajos de lectura: su padre es la vista y el contexto exige que la vista (o un hijo) tenga el foco.
        Así, en la página de edición (la vista está oculta) la barra espaciadora escribe espacios."""
        lectura = [
            ("Space", self._pulsar_play),
            ("Home", self._rebobinar),
            ("B", lambda: self._ir_a_marca()),
            ("Up", lambda: self._slider.setValue(self._slider.value() + 1)),
            ("Down", lambda: self._slider.setValue(self._slider.value() - 1)),
            ("PgUp", lambda: self._view.scroll_by_lines(-LINE_STEP)),
            ("PgDown", lambda: self._view.scroll_by_lines(LINE_STEP)),
            ("+", lambda: self._bump_letra(FONT_STEP)),
            ("=", lambda: self._bump_letra(FONT_STEP)),
            ("Ctrl++", lambda: self._bump_letra(FONT_STEP)),
            ("-", lambda: self._bump_letra(-FONT_STEP)),
            ("Ctrl+-", lambda: self._bump_letra(-FONT_STEP)),
            ("M", lambda: self._btn_espejo.toggle()),
            ("C", lambda: self._btn_centrar.toggle()),
            ("F", lambda: self._btn_frases.toggle()),
            ("L", lambda: self._cycle_font(1)),
            ("Shift+L", lambda: self._cycle_font(-1)),
            ("R", lambda: self._toma_mala()),
            ("A", lambda: self._btn_ajustes.toggle()),
        ]
        globales = [
            ("F11", lambda: self._alternar_pantalla()),
            ("Esc", lambda: self._salir_pantalla_completa()),
            ("K", lambda: self._btn_camara.toggle()),
            ("Ctrl+O", lambda: self._abrir_dialogo()),
            ("Ctrl+E", lambda: self._btn_editar.toggle()),
        ]
        self._atajos: list[QShortcut] = []
        for secuencia, accion in lectura:
            sc = QShortcut(QKeySequence(secuencia), self._view)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(accion)
            self._atajos.append(sc)
        for secuencia, accion in globales:
            sc = QShortcut(QKeySequence(secuencia), self)
            sc.activated.connect(accion)
            self._atajos.append(sc)

    # ---- modo edición / lectura ---------------------------------------------
    def _set_editing(self, on: bool) -> None:
        if on == self._editing:
            return
        if on:
            self._cancelar_cuenta()
            self._view.pause()
            self._editing = True
            self._apply_editing_state()
            self._editor.setFocus()
        else:
            self._editing = False
            texto = self._editor.toPlainText()
            if texto != self._view.text():
                self._view.set_text(texto)     # cambió el texto: empieza desde el principio
            self._apply_editing_state()
            self._devolver_foco()

    def _apply_editing_state(self) -> None:
        self._stack.setCurrentIndex(PAGE_EDIT if self._editing else PAGE_READ)
        self._btn_editar.blockSignals(True)
        self._btn_editar.setChecked(self._editing)
        self._btn_editar.blockSignals(False)
        self._btn_play.setEnabled(not self._editing)
        self._btn_inicio.setEnabled(not self._editing)
        self._actualizar_botones_marcas()
        self._canvas.set_reserve_bar(self._editing)
        self._actualizar_boton_play()
        self._actualizar_tiempo()
        self._aplicar_cromo()

    def _devolver_foco(self) -> None:
        (self._editor if self._editing else self._view).setFocus()

    # ---- controles ----------------------------------------------------------
    def _bump_letra(self, delta: int) -> None:
        self._spin.setValue(self._spin.value() + delta)

    def _on_velocidad(self, valor: int) -> None:
        self._view.set_speed(valor)
        self._lbl_velocidad.setText(str(self._view.speed()))
        self._tp.speed = self._view.speed()
        self._actualizar_tiempo()
        self._guardar_ajustes_pronto()

    def _on_letra(self, valor: int) -> None:
        self._view.set_font_size(valor)
        self._tp.font_size = self._view.font_size()
        self._actualizar_tiempo()
        self._guardar_ajustes_pronto()

    def _on_cuenta(self, indice: int) -> None:
        self._tp.countdown_s = int(self._combo_cuenta.itemData(indice) or 0)
        self._guardar_ajustes_pronto()
        self._devolver_foco()

    def _on_letra_desde_vista(self, valor: int) -> None:
        """Ctrl + rueda en la vista: actualiza el control sin volver a aplicar el valor."""
        self._spin.blockSignals(True)
        self._spin.setValue(valor)
        self._spin.blockSignals(False)
        self._tp.font_size = valor
        self._actualizar_tiempo()
        self._guardar_ajustes_pronto()

    def _on_fuente(self, indice: int) -> None:
        """Se eligió otra letra en el desplegable (o con L / Mayús+L)."""
        familia = self._combo_fuente.itemData(indice, Qt.ItemDataRole.UserRole)
        if not familia:
            return
        self._view.set_font_family(familia)
        self._tp.font_family = self._view.font_family()
        self._estilo_combo_fuente(familia)
        self._actualizar_tiempo()
        self._guardar_ajustes_pronto()
        self._devolver_foco()

    def _cycle_font(self, step: int) -> None:
        """Pasa a la letra siguiente (step 1) o anterior (step -1) de la lista; tras la última vuelve a la primera."""
        n = self._combo_fuente.count()
        if n == 0:
            return
        actual = self._combo_fuente.currentIndex()
        if actual < 0:          # ninguna seleccionada (la letra aplicada no está en la lista)
            actual = 0 if step > 0 else n
        self._combo_fuente.setCurrentIndex((actual + step) % n)

    def _estilo_combo_fuente(self, familia: str) -> None:
        """El desplegable cerrado muestra su nombre con su propia letra (la hoja del widget gana a la global)."""
        self._combo_fuente.setStyleSheet(f"QComboBox {{ font-family: '{familia}'; font-size: 11pt; }}")
        self._canvas.place()        # el desplegable cambia de alto con la letra: se recoloca la barra

    def _on_espaciado(self, valor: int) -> None:
        self._view.set_letter_spacing(valor)
        self._tp.letter_spacing = self._view.letter_spacing()
        self._actualizar_tiempo()
        self._guardar_ajustes_pronto()

    def _on_espejo(self, on: bool) -> None:
        self._view.set_mirror(on)
        self._tp.mirror = bool(on)
        self._guardar_ajustes_pronto()

    def _on_fps(self, indice: int) -> None:
        self._view.set_fps(self._combo_fps.itemData(indice) or 0)
        self._tp.fps = self._view.fps()
        self._actualizar_fps()
        self._guardar_ajustes_pronto()
        self._devolver_foco()

    def _on_playing_fps(self, playing: bool) -> None:
        """Mientras se lee, la cifra de fps se refresca cada segundo (el automático puede reajustarse)."""
        if playing:
            self._timer_fps.start()
        else:
            self._timer_fps.stop()
        self._actualizar_fps()

    def _actualizar_fps(self) -> None:
        texto = f"≈ {self._view.effective_fps()} fps"
        if self._view.fps() == 0:
            texto += f" (monitor {self._view.monitor_hz()} Hz)"
        self._lbl_fps.setText(texto)

    def _on_angulo(self, grados: int) -> None:
        self._view.set_view_angle(grados)
        self._tp.view_angle = self._view.view_angle()
        self._lbl_angulo.setText(_texto_angulo(self._tp.view_angle))
        self._guardar_ajustes_pronto()

    def _on_frases(self, on: bool) -> None:
        self._view.set_phrase_breaks(on)
        self._tp.phrase_breaks = bool(on)
        self._guardar_ajustes_pronto()

    def _on_centrar(self, on: bool) -> None:
        self._view.set_centered(on)
        self._tp.center_text = bool(on)
        self._guardar_ajustes_pronto()

    def _on_guia(self, on: bool) -> None:
        self._view.set_guide_centered(on)
        self._tp.guide_center = bool(on)
        self._guardar_ajustes_pronto()

    def _on_barra_avance(self, on: bool) -> None:
        self._view.set_progress_bar(on)
        self._tp.show_progress_bar = bool(on)
        self._guardar_ajustes_pronto()

    # ---- banderas rojas (marcas) --------------------------------------------
    def _ir_a_marca(self) -> None:
        """Lleva la siguiente bandera a la línea de lectura y se queda en pausa (botón y tecla B)."""
        self._view.jump_to_next_mark()
        self._devolver_foco()

    def _quitar_marcas(self) -> None:
        self._view.clear_marks()
        self._devolver_foco()

    def _actualizar_botones_marcas(self) -> None:
        """Las acciones de banderas solo sirven si hay alguna; al editar están apagadas, como «Al principio»."""
        activos = self._view.has_marks() and not self._editing
        self._btn_marca.setEnabled(activos)
        self._btn_quitar_marcas.setEnabled(activos)

    def _on_marcas(self) -> None:
        """La vista puso o quitó banderas: se guardan con la huella del texto al que pertenecen."""
        anterior = self._tp.bookmark_pos
        pausa = self._view.pause_mark()
        self._tp.bookmark_pos = pausa
        self._tp.bookmarks = self._view.manual_marks()
        self._tp.bookmark_crc = zlib.crc32(self._view.text().encode("utf-8")) if self._view.has_marks() else 0
        self._actualizar_botones_marcas()
        self._guardar_ajustes_pronto()
        if pausa >= 0 and pausa != anterior:       # el aviso es solo por la bandera de pausa, no por las del clic
            self.statusBar().showMessage("🚩 Marca puesta: la próxima vez seguirás desde aquí.", 5000)

    def _on_playing(self, playing: bool) -> None:
        self._actualizar_boton_play()
        if playing:
            self._asegurar_sesion().event("lectura", smart=self._btn_smart.isChecked())
        elif self._sesion is not None:
            self._evaluar_pendientes()
            self._sesion.event("pausa")
        self._actualizar_revisar()
        if self._btn_smart.isChecked():
            if playing:
                self._smart_escuchar()
            else:
                self._smart_callar()

    def _on_progreso(self, p: float) -> None:
        self._lbl_progreso.setText(f"{round(p * 100)} %")
        self._actualizar_tiempo()

    # ---- toma mala, registro de la sesión y frases para revisar ------------------
    def _asegurar_sesion(self) -> ensayo.SessionLog:
        """El registro de esta lectura (uno nuevo si el texto cambió). Va a %APPDATA%\\Cronista\\sesiones."""
        texto = self._view.text()
        if self._sesion is None or not self._sesion.matches(texto):
            self._sesion = ensayo.SessionLog(paths.data_dir() / "sesiones", texto)
            self._oido_frase, self._frase_actual = [], None
        return self._sesion

    def _toma_mala(self) -> None:
        """R: la frase salió mal. Se apunta, el texto vuelve al principio de la frase y, tras una cuenta atrás
        corta, se sigue leyendo. Sin preguntas ni ventanas: solo un «↺» que se desvanece."""
        texto = self._view.text()
        if not texto.strip() or self._editing:
            return
        sesion = self._asegurar_sesion()
        smart_activo = self._btn_smart.isChecked() and self._follower is not None
        pos = self._follower.position() if smart_activo else self._view.reading_pos()
        i = ensayo.sentence_at(sesion.spans, pos)
        if i < 0:
            return
        if smart_activo and i > 0 and pos - sesion.spans[i][0] < 3:
            i -= 1                       # acaba de terminar la frase anterior: esa es la que salió mal
        self._cancelar_cuenta()
        self._view.jump_to_mark(sesion.spans[i][0])
        self._smart_reubicar()
        sesion.event("toma_mala", frase=i)
        self._mostrar_aviso_breve("↺ Toma mala")
        self.start_reading(segundos=int(self._tp.retake_countdown_s))

    def _mostrar_aviso_breve(self, texto: str) -> None:
        self._aviso.setText(texto)
        self._canvas.place()
        self._aviso.show()
        self._aviso_breve_n = getattr(self, "_aviso_breve_n", 0) + 1
        QTimer.singleShot(1000, lambda n=self._aviso_breve_n: self._ocultar_aviso_breve(n))

    def _ocultar_aviso_breve(self, n: int) -> None:
        if n == self._aviso_breve_n:             # (si entretanto salió otro aviso, ese manda)
            self._aviso.hide()
            self._aviso.setText(HINT_TEXT)

    def _evaluar_frase(self, texto: str) -> None:
        """Con Smart: al pasar a otra frase, compara lo oído en la anterior con lo escrito (en silencio)."""
        if self._tp.review_level == "apagado" or self._follower is None:
            return
        sesion = self._asegurar_sesion()
        i = ensayo.sentence_at(sesion.spans, self._follower.position())
        if self._frase_actual is None or i < self._frase_actual:
            self._frase_actual = i
            return
        if i == self._frase_actual:
            return
        if self._revisor is None or self._revisor_texto != texto:
            desconocidas = getattr(self._oyente, "unknown_words", set()) if self._oyente is not None else set()
            formas = getattr(self._oyente, "spoken_forms", {}) if self._oyente is not None else {}
            self._revisor = ensayo.ReadingChecker(texto, self._tp.review_sensitivity, self._smart_idioma(),
                                                  desconocidas, formas)
            self._revisor_texto = texto
        # Las frases terminadas, menos la última: esa se juzga cuando termine la siguiente, porque hasta entonces no
        # se sabe si lo oído era de ella o de otra casi igual que venía después. (La marca llega una frase más tarde.)
        if i - self._frase_actual < 2:
            return
        # La frase en la que va ahora también entra, sin juzgarse: lo que ya se oyó de ella no se atribuye a una
        # anterior que acabe igual (saltarse una frase y leer «…para ver el MP3» completaba «para ver el
        # juego» de la anterior, y a la actual le faltaba el principio).
        frases = list(range(self._frase_actual, i + 1))
        sospechosas, usadas = self._revisor.evaluate(frases, self._oido_frase, pending=2)
        frases = frases[:-2]
        for k in frases:
            if k in sospechosas and sospechosas[k] == [ensayo.NOT_READ] and sesion.read_ok_before(k):
                continue                                 # ya se leyó bien antes: saltársela al repasar no es error
            if k in sospechosas:
                sesion.event("posible_error", frase=k, faltan=sospechosas[k], oido=self._oido_frase[-30:])
            else:
                sesion.event("frase_bien", frase=k)
        self._oido_frase = self._oido_frase[usadas:]    # lo que sobra es de la frase pendiente o de la actual
        self._frase_actual = i - 1
        self._actualizar_revisar()

    def _evaluar_pendientes(self, pos: int | None = None) -> None:
        """Al pausar, terminar o volver atrás: se juzgan las frases ya terminadas que esperaban a la siguiente.
        pos: hasta dónde se había leído (por defecto, donde va el seguidor)."""
        if (self._tp.review_level == "apagado" or self._follower is None or self._frase_actual is None
                or self._revisor is None or self._sesion is None):
            return
        pos = self._follower.position() if pos is None else pos
        i = ensayo.sentence_at(self._sesion.spans, pos)
        if i >= 0 and pos >= self._sesion.spans[i][1] - 2:
            i += 1                                       # la frase en la que va también está terminada
        frases = list(range(self._frase_actual, min(i, len(self._sesion.spans))))
        if not frases:
            return
        actual = [i] if i < len(self._sesion.spans) else []     # la que está a medias ayuda a repartir, sin juzgarse
        sospechosas, usadas = self._revisor.evaluate(frases + actual, self._oido_frase, pending=len(actual))
        for k in frases:
            if k in sospechosas and sospechosas[k] == [ensayo.NOT_READ] and self._sesion.read_ok_before(k):
                continue                                 # ya se leyó bien antes: saltársela al repasar no es error
            if k in sospechosas:
                self._sesion.event("posible_error", frase=k, faltan=sospechosas[k], oido=self._oido_frase[-30:])
            else:
                self._sesion.event("frase_bien", frase=k)
        self._oido_frase = self._oido_frase[usadas:]
        self._frase_actual = frases[-1] + 1

    def _actualizar_revisar(self) -> None:
        nivel = self._tp.review_level
        frases = self._sesion.to_review() if self._sesion is not None and nivel != "apagado" else []
        posiciones = [self._sesion.spans[i][0] for i in frases] if frases else []
        self._view.set_review(posiciones, nivel)
        visible = bool(frases) and (nivel != "silencioso" or not self._view.is_playing())
        self._revisar_n = len(frases)
        self._texto_revisar()
        self._btn_revisar.setVisible(visible)

    def _ir_a_revisar(self) -> None:
        """Lleva a la siguiente frase para revisar (tras la línea de lectura; tras la última, la primera)."""
        marcas = self._view.review_marks()
        if not marcas:
            return
        actual = self._view.reading_pos()
        siguientes = [p for p in marcas if p > actual]
        self._view.jump_to_mark(siguientes[0] if siguientes else marcas[0])
        self._smart_reubicar()
        self._devolver_foco()

    def _on_revisar(self, indice: int) -> None:
        self._tp.review_level = self._combo_revisar.itemData(indice) or "discreto"
        self._actualizar_revisar()
        self._guardar_ajustes_pronto()

    # ---- inicio con cuenta atrás ---------------------------------------------
    def start_reading(self, segundos: int | None = None) -> None:
        """Empieza a leer. Con cuenta atrás (ajuste «Cuenta atrás») muestra 3, 2, 1 antes de mover el texto."""
        if self._view.is_playing() or self.is_counting_down():
            return
        if self._btn_smart.isChecked() and not self._smart_listo():
            return                       # (se ofrece bajar el modelo; al terminar, el usuario pulsa Iniciar)
        segundos = int(self._tp.countdown_s if segundos is None else segundos)
        if segundos <= 0:
            self._view.play()
            return
        self._cuenta_restante = segundos
        self._mostrar_cuenta()
        self._cuenta_timer.start()
        self._actualizar_boton_play()

    def is_counting_down(self) -> bool:
        return self._cuenta_timer.isActive()

    def _pulsar_play(self) -> None:
        """Botón Iniciar y Espacio: cancela la cuenta si está en marcha, pausa si se lee, y si no, empieza."""
        if self.is_counting_down():
            self._cancelar_cuenta()
        elif self._view.is_playing():
            self._view.toggle()          # pausa y deja la bandera roja
        else:
            self.start_reading()

    def _tick_cuenta(self) -> None:
        if not self.is_counting_down():      # tick que llega tras cancelar: no hace nada
            return
        self._cuenta_restante -= 1
        if self._cuenta_restante > 0:
            self._mostrar_cuenta()
            return
        self._cuenta_timer.stop()
        self._cuenta_label.hide()
        self._view.play()
        self._actualizar_boton_play()

    def _mostrar_cuenta(self) -> None:
        self._cuenta_label.setText(str(self._cuenta_restante))
        self._canvas.place()             # centra el número con su tamaño actual y lo deja encima
        self._cuenta_label.show()

    def _cancelar_cuenta(self) -> None:
        if not self.is_counting_down():
            return
        self._cuenta_timer.stop()
        self._cuenta_restante = 0
        self._cuenta_label.hide()
        self._actualizar_boton_play()

    def _rebobinar(self) -> None:
        self._cancelar_cuenta()
        self._view.rewind()

    def _actualizar_boton_play(self) -> None:
        if self.is_counting_down():
            texto, nombre = "Cancelar", "x"
        elif self._view.is_playing():
            texto, nombre = "Pausar", "pause"
        else:
            texto, nombre = "Iniciar", "play"
        self._btn_play.setText(texto)
        if self._btn_play.property("icono_nombre") != nombre:
            diseno.poner_icono(self._btn_play, nombre, diseno.SOBRE_ACENTO)

    # ---- indicador de tiempo -------------------------------------------------
    def _actualizar_tiempo(self) -> None:
        texto = self._texto_tiempo()
        if self._lbl_tiempo.text() != texto:      # solo se toca la etiqueta si cambia
            self._lbl_tiempo.setText(texto)

    def _texto_tiempo(self) -> str:
        """Tiempo leído / total a la velocidad actual, y lo que falta. Sin texto (o sin medida) muestra «—»."""
        px_s = tp_text.speed_to_px_per_s(self._view.speed(), self._view.line_height())
        if not self._view.text().strip() or px_s <= 0:
            return "—"
        transcurrido = self._view.offset() / px_s
        total = self._view.max_offset() / px_s
        falta = max(0.0, total - transcurrido)
        return (f"{formato_tiempo(transcurrido)} / {formato_tiempo(total)} · "
                f"faltan {formato_tiempo(falta)}")

    def _on_editor_changed(self) -> None:
        self._puede_guardar_texto = True
        self._timer_texto.start()

    # ---- archivos -----------------------------------------------------------
    def _leer_texto_guardado(self) -> str:
        if not self._text_path.exists():
            return ""
        try:
            return tp_text.load_text(self._text_path)
        except (OSError, ValueError):
            log.exception("No se pudo leer el texto guardado en %s", self._text_path)
            self._puede_guardar_texto = False
            self.statusBar().showMessage("No se pudo leer el texto guardado; no se sobrescribirá hasta que escribas.",
                                         10000)
            return ""

    def _abrir_dialogo(self) -> None:
        inicio = ""
        previo = self._tp.last_file
        if previo and Path(previo).exists():
            inicio = str(Path(previo).parent)
        ruta, _ = QFileDialog.getOpenFileName(self, "Abrir texto", inicio, FILE_FILTER)
        if ruta:
            self._cargar_archivo(ruta)

    def _cargar_archivo(self, ruta: str) -> bool:
        try:
            texto = tp_text.load_text(ruta)
        except ValueError as exc:
            QMessageBox.warning(self, "No se pudo abrir el archivo", str(exc))
            return False
        except OSError as exc:
            log.exception("No se pudo leer el archivo %s", ruta)
            QMessageBox.warning(self, "No se pudo abrir el archivo", f"No se pudo leer el archivo:\n{exc}")
            return False
        self._editor.setPlainText(texto)
        self._view.set_text(texto)
        self._puede_guardar_texto = True
        self._actualizar_tiempo()
        self._set_editing(False)
        self._devolver_foco()
        self._tp.last_file = ruta
        self._guardar_ajustes_pronto()
        self._timer_texto.start()
        self.statusBar().showMessage(f"Abierto: {Path(ruta).name}", 5000)
        return True

    def _borrar_texto(self) -> None:
        """Borra todo el texto (pide confirmación). Se borra con el cursor del editor: así Ctrl+Z en el editor
        lo recupera mientras no se cierre Cronista."""
        respuesta = QMessageBox.question(
            self, "Borrar texto",
            "¿Borrar todo el texto del teleprompter?\n\n"
            "Mientras no cierres Cronista puedes recuperarlo con Ctrl+Z en el editor.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if respuesta != QMessageBox.StandardButton.Yes:
            return
        cursor = self._editor.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.removeSelectedText()
        self._view.set_text("")          # quita también las banderas
        self._set_editing(True)          # página de edición; el temporizador de texto lo guarda

    def _actualizar_boton_borrar(self) -> None:
        self._btn_borrar.setEnabled(bool(self._editor.toPlainText()))

    def _guardar_texto(self) -> None:
        if not self._puede_guardar_texto:
            return
        try:
            self._text_path.parent.mkdir(parents=True, exist_ok=True)
            self._text_path.write_text(self._editor.toPlainText(), encoding="utf-8")
        except OSError as exc:
            log.exception("No se pudo guardar el texto del teleprompter en %s", self._text_path)
            self.statusBar().showMessage(f"No se pudo guardar el texto: {exc}", 10000)

    def _guardar_ajustes_pronto(self) -> None:
        self._timer_ajustes.start()

    def _guardar_ajustes(self) -> None:
        try:
            self._settings.save()
        except OSError as exc:
            log.exception("No se pudieron guardar los ajustes")
            self.statusBar().showMessage(f"No se pudieron guardar los ajustes: {exc}", 10000)

    # ---- pantalla completa --------------------------------------------------
    def _alternar_pantalla(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _salir_pantalla_completa(self) -> None:
        if self._btn_camara.isChecked():
            self._btn_camara.setChecked(False)
        elif self.isFullScreen():
            self.showNormal()

    def _on_estado_cambiado(self) -> None:
        full = self.isFullScreen()
        if full == self._last_full:
            return
        self._last_full = full
        diseno.poner_icono(self._btn_pantalla, "minimize" if full else "maximize")
        self._btn_pantalla.setToolTip("Salir de pantalla completa (F11 o Esc)" if full else
                                      "Pantalla completa (F11). En pantalla completa, Esc sale.")
        self.statusBar().setVisible(not full)
        if full:
            self._aviso.show()
            self._timer_aviso.start()
        else:
            self._aviso.hide()
            self._timer_aviso.stop()
        self._aplicar_cromo()

    def _aplicar_cromo(self) -> None:
        """Pantalla completa leyendo: la barra y el cursor se ocultan tras IDLE_MS sin mover el ratón.
        En cualquier otro caso, todo visible."""
        automatico = self._compacto() and not self._editing
        self._timer_ocultar.stop()
        self._bar.show()
        self._view.set_cursor_hidden(False)
        if automatico:
            self._timer_ocultar.start()

    def _actividad(self) -> None:
        if not self._compacto() or self._editing:
            return
        self._bar.show()
        self._view.set_cursor_hidden(False)
        self._timer_ocultar.start()

    def _on_idle(self) -> None:
        if not self._compacto() or self._editing:
            return
        if self._bar.rect().contains(self._bar.mapFromGlobal(QCursor.pos())):
            self._timer_ocultar.start()     # el ratón está encima de la barra: no se oculta
            return
        self._bar.hide()
        self._view.set_cursor_hidden(True)

    def _compacto(self) -> bool:
        """Pantalla completa o modo cámara: la barra y el cursor se esconden solos."""
        return self.isFullScreen() or self._btn_camara.isChecked()

    # ---- 📷 junto a la cámara ------------------------------------------------
    def _on_camara(self, on: bool) -> None:
        """Franja siempre encima, arriba y al centro de la pantalla (bajo la webcam), sin bordes."""
        if on:
            if self.isFullScreen():
                self.showNormal()
            self._camara_geo = self.geometry()
            area = (self.screen() or self.windowHandle().screen()).availableGeometry()
            ancho, alto = int(area.width() * 0.6), max(220, int(area.height() * 0.3))
            self.setWindowFlags(self.windowFlags() | Qt.WindowType.FramelessWindowHint
                                | Qt.WindowType.WindowStaysOnTopHint)
            self.setGeometry(area.x() + (area.width() - ancho) // 2, area.y(), ancho, alto)
            self.statusBar().hide()
            self._grip.show()
            self.show()
            self._btn_camara.setToolTip("Volver a la ventana (K o Esc)")
            self._ajustes.hide()                 # en la franja no cabe: vuelve al salir si estaba abierto
        else:
            self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.FramelessWindowHint
                                & ~Qt.WindowType.WindowStaysOnTopHint)
            self._grip.hide()
            self.statusBar().show()
            if self._camara_geo is not None:
                self.setGeometry(self._camara_geo)
            self.show()
            self._btn_camara.setToolTip("Junto a la cámara (K): la ventana se vuelve una franja siempre encima, "
                                        "arriba de la pantalla, bajo la webcam. K o Esc: volver.")
            self._ajustes.setVisible(self._btn_ajustes.isChecked())
        self._aplicar_cromo()
        self._canvas.place()
        self._devolver_foco()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        # Sin bordes no hay barra de título: en modo cámara, arrastrar la barra de controles mueve la franja
        if (self._btn_camara.isChecked() and event.button() == Qt.MouseButton.LeftButton
                and self.windowHandle() is not None):
            self.windowHandle().startSystemMove()
            return
        super().mousePressEvent(event)

    # ---- 🎙 modo Smart (el texto sigue la voz) -------------------------------------
    def _on_smart(self, on: bool) -> None:
        """Encender o apagar Smart pausa la lectura: se vuelve a pulsar Iniciar (con o sin voz)."""
        self._tp.smart = bool(on)
        self._guardar_ajustes_pronto()
        self._cancelar_cuenta()
        if self._view.is_playing():
            self._view.pause()
        self._smart_callar()
        self._view.set_follow(on)
        if not on:
            self._view.set_hard_words([])                # el subrayado solo tiene sentido con Smart
        self._lbl_oido.setVisible(on)
        self._lbl_oido.setText("pulsa Iniciar y lee en voz alta" if on else "")
        if on:
            if not self._smart_listo():
                return
            self.statusBar().showMessage("Smart: pulsa Iniciar y lee en voz alta; el texto te sigue.", 6000)
        else:
            self.statusBar().showMessage("Smart apagado: pulsa Iniciar y el texto sube a la velocidad elegida.", 5000)
        log.info("Smart %s", "encendido" if on else "apagado")
        self._devolver_foco()

    def _llenar_micros(self) -> None:
        from cronista.teleprompter import smart
        self._combo_mic.blockSignals(True)
        for nombre in smart.input_devices():
            self._combo_mic.addItem(nombre, nombre)
        i = self._combo_mic.findData(self._tp.smart_mic)
        self._combo_mic.setCurrentIndex(max(0, i))
        self._combo_mic.blockSignals(False)
        self._lbl_oido.setVisible(self._btn_smart.isChecked())
        if self._btn_smart.isChecked():
            self._lbl_oido.setText("pulsa Iniciar y lee en voz alta")

    def _on_mic(self, _i: int) -> None:
        self._tp.smart_mic = self._combo_mic.currentData() or ""
        self._guardar_ajustes_pronto()
        if self._oyente is not None and self._oyente.is_running():   # cambia en caliente
            self._smart_callar()
            self._smart_escuchar()
        self._devolver_foco()

    def _smart_nivel(self, nivel: float) -> None:
        import time as _t
        ahora = _t.monotonic()
        if nivel > 0.02:
            self._oido_ultimo = ahora
        if not self._view.is_playing():
            return
        if ahora - self._oido_ultimo > 4.0:
            self._lbl_oido.setText("no te oigo: revisa el micrófono")
            return
        barras = "▁▂▃▄▅▆▇█"
        barra = barras[min(len(barras) - 1, int(nivel * 3 * len(barras)))]
        self._lbl_oido.setText(f"{barra} {self._oido_palabras}")

    def _smart_idioma(self) -> str:
        from cronista.teleprompter import smart
        return smart.guess_language(self._view.text())

    def _smart_listo(self) -> bool:
        """¿Está el modelo de voz del idioma del texto? Si no, ofrece bajarlo (una sola vez, ~40 MB)."""
        from cronista.teleprompter import smart
        idioma = self._smart_idioma()
        if smart.model_path(idioma) is not None:
            return True
        if self._descarga is not None:
            return False
        nombre = "español" if idioma == "es" else "inglés"
        r = QMessageBox.question(self, "Modo Smart",
                                 f"Para seguir tu voz, Cronista necesita el modelo de voz en {nombre} "
                                 "(unos 40 MB, se baja una sola vez y después funciona sin internet).\n\n"
                                 "¿Lo bajo?")
        if r != QMessageBox.StandardButton.Yes:
            self._btn_smart.setChecked(False)
            return False
        self._smart_bajar(idioma)
        return False

    def _smart_bajar(self, idioma: str) -> None:
        import threading
        from PySide6.QtCore import QObject
        from PySide6.QtWidgets import QProgressDialog
        from cronista.teleprompter import smart

        class _Avisos(QObject):
            avance = Signal(int, int)
            fin = Signal(str)

        avisos = _Avisos(self)
        dlg = QProgressDialog("Bajando el modelo de voz…", "Cancelar", 0, 100, self)
        dlg.setWindowTitle("Modo Smart")
        dlg.setMinimumDuration(0)
        cancelado = threading.Event()
        dlg.canceled.connect(cancelado.set)

        def avance(hecho: int, total: int) -> None:
            if total > 0:
                dlg.setValue(int(hecho * 100 / total))
                dlg.setLabelText(f"Bajando el modelo de voz… {hecho / 1e6:.0f} de {total / 1e6:.0f} MB")

        def fin(error: str) -> None:
            dlg.close()
            self._descarga = None
            if error:
                self._btn_smart.setChecked(False)
                if not cancelado.is_set():
                    QMessageBox.warning(self, "Modo Smart", f"No se pudo bajar el modelo de voz:\n{error}")
                return
            self.statusBar().showMessage("✓ Modelo de voz listo. Pulsa Iniciar y lee en voz alta.", 8000)

        avisos.avance.connect(avance)
        avisos.fin.connect(fin)

        def trabajo() -> None:
            try:
                smart.download_model(idioma, progress=lambda h, t: avisos.avance.emit(h, t),
                                     cancelled=cancelado.is_set)
                avisos.fin.emit("")
            except Exception as exc:  # noqa: BLE001 - se le cuenta al usuario
                avisos.fin.emit(str(exc) or exc.__class__.__name__)

        self._descarga = threading.Thread(target=trabajo, daemon=True)
        self._descarga.start()

    def _smart_reubicar(self) -> None:
        """El usuario movió el texto a mano: la voz sigue desde la línea de lectura."""
        self._oido_frase, self._frase_actual = [], None      # se vuelve a empezar a comparar desde ahí
        if self._follower is not None:
            self._follower.reset(self._view.reading_pos())

    def _smart_escuchar(self) -> None:
        from cronista.teleprompter import smart
        if self._oyente is not None and self._oyente.is_running():
            return
        texto = self._view.text()
        if self._follower is None or self._follower_text != texto:
            self._follower, self._follower_text = smart.ScriptFollower(texto), texto
        self._follower.reset(self._view.reading_pos())
        if self._oyente is None:
            self._oyente = smart.VoiceListener(self)
            self._oyente.heard.connect(self._smart_oido)
            self._oyente.heard_final.connect(self._smart_oido_final)     # (método: Qt lo trae al hilo de la ventana)
            self._oyente.error.connect(self._smart_error)
            self._oyente.analysed.connect(self._smart_analizado)
            self._oyente.level.connect(self._smart_nivel)
        import time as _t
        self._oido_ultimo = _t.monotonic()
        self._oido_palabras = ""
        idioma = self._smart_idioma()
        mic = self._tp.smart_mic or None
        log.info("Smart: escuchando (idioma %s, micrófono %s)", idioma, mic or "predeterminado")
        self._oyente.start(idioma, mic, smart.script_vocabulary(texto, idioma), smart.acronyms(texto))
        self._lbl_oido.setText("escuchando…")
        self.statusBar().showMessage("Escuchando… lee en voz alta.", 4000)

    def _smart_analizado(self) -> None:
        """El oyente ya sabe qué palabras del guion no conoce el modelo: el seguidor y el revisor aprenden a juntar
        las piezas con que se oyen («in jugable», «o ene u»), y las que no tienen arreglo se subrayan (discreto)."""
        from cronista.teleprompter import smart
        if self._oyente is None:
            return
        formas = getattr(self._oyente, "spoken_forms", {})
        if self._follower is not None:
            self._follower.set_spoken_forms(formas)
        self._revisor = None                             # se rehace con las piezas al juzgar la próxima frase
        sin_arreglo = {w for w in getattr(self._oyente, "unknown_words", set()) if w not in formas}
        texto = self._view.text()
        spans = [(a, b) for _, a, b in smart.tokenize(texto) if texto[a:b].lower() in sin_arreglo]
        self._view.set_hard_words(spans if self._btn_smart.isChecked() else [])
        if spans:
            nombres = sorted({texto[a:b] for a, b in spans}, key=str.lower)
            self._btn_smart.setToolTip(SMART_TIP + "\n\nNo puede oír (subrayadas): " + ", ".join(nombres[:12])
                                       + (" …" if len(nombres) > 12 else ""))
        else:
            self._btn_smart.setToolTip(SMART_TIP)

    def _smart_callar(self) -> None:
        if self._oyente is not None and self._oyente.is_running():
            self._oyente.stop()
            if self._btn_smart.isChecked():
                self._lbl_oido.setText("en pausa")

    def _smart_oido_final(self, palabras: list) -> None:
        """Un trozo que el reconocedor dio por cerrado: el seguidor lo recuerda para enlazarlo con el siguiente, y se
        guarda para comparar la frase con el guion cuando se pase a la siguiente."""
        self._smart_oido(palabras, final=True)
        if self._follower is not None and self._view.is_playing():
            self._oido_frase.extend(str(p) for p in palabras)
            self._evaluar_frase(self._view.text())

    def _smart_oido(self, palabras: list, final: bool = False) -> None:
        if self._follower is None or not self._view.is_playing():
            return
        self._oido_palabras = "«" + " ".join(str(p) for p in palabras[-4:]) + "»"
        import time as _t
        self._oido_ultimo = _t.monotonic()          # (si entiende palabras, el micrófono funciona)
        self._lbl_oido.setText(f"{self._oido_palabras}")
        antes = self._follower.position()
        pos = self._follower.feed([str(p) for p in palabras], final=final)
        if pos is not None:
            if self._follower.jumped and pos < antes:
                # Volvió atrás a releer: lo leído hasta ahí se juzga ya (si no, la frase pendiente se perdía y
                # luego salía como «sin leer») y se empieza a comparar de nuevo desde donde está ahora.
                self._evaluar_pendientes(antes)
                self._oido_frase, self._frase_actual = [], None
                self._actualizar_revisar()
            self._view.follow_to(pos, back=self._follower.jumped)

    def _smart_error(self, mensaje: str) -> None:
        log.warning("Smart: %s", mensaje)
        self._lbl_oido.setText("✗ " + mensaje[:60])
        self._view.pause()
        self.statusBar().showMessage(f"{mensaje}", 10000)
        QMessageBox.warning(self, "Modo Smart", mensaje)

    # ---- eventos de ventana -------------------------------------------------
    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self._on_estado_cambiado()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self._actividad()
        super().mouseMoveEvent(event)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._canvas.place()        # al mostrarse, la barra ya tiene su alto definitivo (con la hoja del desplegable)
        self._timer_tiempo.start()  # el indicador de tiempo se refresca mientras la ventana se ve
        self._actualizar_tiempo()
        if self._reanclar_marca:
            # Al arrancar, el texto se maquetó con el ancho provisional de la vista: con otro ancho cambian las
            # líneas. Ahora que tiene su tamaño real, se vuelve a llevar la bandera a la línea de lectura.
            self._reanclar_marca = False
            self._view.jump_to_mark(self._view.pause_mark())
        self._devolver_foco()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._timer_tiempo.stop()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._smart_callar()
        self._cancelar_cuenta()
        self._timer_tiempo.stop()
        if self._view.is_playing():
            self._view.pause()
            self._view.mark_pause_here()        # cerrar a mitad de lectura también deja la marca para la próxima vez
        self._timer_ajustes.stop()
        self._timer_texto.stop()
        self._guardar_texto()
        self._guardar_ajustes()
        super().closeEvent(event)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        for url in event.mimeData().urls():
            ruta = url.toLocalFile()
            if ruta:
                event.acceptProposedAction()
                self._cargar_archivo(ruta)
                return
        event.ignore()

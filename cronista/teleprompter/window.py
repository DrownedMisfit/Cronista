"""Ventana del teleprompter: lectura, edición del texto, velocidad, tipo y tamaño de letra, espaciado, espejo y
pantalla completa.

Los ajustes se guardan en settings.teleprompter (settings.json) y el texto en teleprompter.txt.
"""
from __future__ import annotations

import logging
import zlib
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import (QCloseEvent, QCursor, QDragEnterEvent, QDropEvent, QFont, QKeySequence, QMouseEvent,
                           QShortcut, QShowEvent, QTextCursor)
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
                               QPlainTextEdit, QPushButton, QSlider, QSpinBox, QStackedWidget, QVBoxLayout,
                               QWidget)

from cronista import APP_NAME, __version__
from cronista.core import paths
from cronista.core.settings import Settings
from cronista.teleprompter import fonts
from cronista.teleprompter import text as tp_text
from cronista.teleprompter.text import (ANGLE_MAX, ANGLE_MIN, FONT_MAX, FONT_MIN, SPACING_MAX, SPACING_MIN,
                                        SPACING_STEP, SPEED_MAX, SPEED_MIN)
from cronista.teleprompter.view import create_view

log = logging.getLogger(__name__)

PAGE_READ = 0
PAGE_EDIT = 1
FILE_FILTER = "Textos (*.txt *.md *.docx);;Todos los archivos (*)"
PLACEHOLDER = "Escribe o pega aquí tu texto, o abre un archivo (.txt, .md o .docx)…"
HINT_TEXT = "Esc: salir de pantalla completa · Espacio: iniciar/pausar"
IDLE_MS = 3000             # pantalla completa leyendo: sin mover el ratón, se oculta la barra y el cursor
HINT_MS = 3000             # cuánto tiempo se ve el aviso al entrar en pantalla completa
SETTINGS_SAVE_MS = 800     # los ajustes se guardan tras una pausa, no en cada paso del control
TEXT_SAVE_MS = 1000
FONT_STEP = 4              # puntos por pulsación de + / -
LINE_STEP = 3              # líneas por pulsación de RePág / AvPág
FPS_OPCIONES = ((0, "Automático"), (60, "60 fps"), (120, "120 fps"), (144, "144 fps"), (240, "240 fps"))


def _boton(texto: str, ayuda: str, checkable: bool = False) -> QPushButton:
    """Botón que no roba el foco: el foco se queda en la vista para que funcionen los atajos."""
    b = QPushButton(texto)
    b.setToolTip(ayuda)
    b.setCheckable(checkable)
    b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    return b


def _separador() -> QFrame:
    linea = QFrame()
    linea.setFrameShape(QFrame.Shape.VLine)
    linea.setFrameShadow(QFrame.Shadow.Sunken)
    return linea


def _texto_angulo(grados: int) -> str:
    if grados > 0:
        return f"{grados}° desde arriba"
    if grados < 0:
        return f"{-grados}° desde abajo"
    return "0° (de frente)"


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

    def __init__(self, stack: QWidget, bar: QWidget, aviso: QWidget) -> None:
        super().__init__()
        self._stack = stack
        self._bar = bar
        self._aviso = aviso
        self._reserve_bar = False
        self.on_place = None          # callable(alto_barra): la ventana avisa a la vista del hueco que tapa la barra
        for hijo in (stack, bar, aviso):
            hijo.setParent(self)    # setParent oculta el widget: hay que volver a mostrarlo
        stack.show()
        bar.show()
        aviso.hide()
        self.setMouseTracking(True)
        self.place()

    def set_reserve_bar(self, on: bool) -> None:
        self._reserve_bar = on
        self.place()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.place()

    def place(self) -> None:
        ancho, alto = self.width(), self.height()
        alto_barra = self._bar.sizeHint().height()
        reserva = alto_barra if self._reserve_bar else 0
        self._stack.setGeometry(0, 0, ancho, alto - reserva)
        self._bar.setGeometry(0, alto - alto_barra, ancho, alto_barra)
        if self.on_place is not None:
            self.on_place(alto_barra)     # siempre el alto de la barra, también con ella oculta (pantalla completa)
        self._aviso.adjustSize()
        self._aviso.move((ancho - self._aviso.width()) // 2, 16)
        self._aviso.raise_()
        self._bar.raise_()


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
        self.resize(1240, 720)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)

        self._timer_ajustes = self._un_disparo(SETTINGS_SAVE_MS, self._guardar_ajustes)
        self._timer_texto = self._un_disparo(TEXT_SAVE_MS, self._guardar_texto)
        self._timer_ocultar = self._un_disparo(IDLE_MS, self._on_idle)
        self._timer_aviso = self._un_disparo(HINT_MS, lambda: self._aviso.hide())
        self._timer_fps = QTimer(self)          # refresca la cifra de fps cada segundo, solo mientras se lee
        self._timer_fps.setInterval(1000)

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
        self._canvas = _Canvas(self._stack, self._bar, self._aviso)
        self.setCentralWidget(self._canvas)
        self._canvas.on_place = self._view.set_bottom_inset     # la vista deja libre el hueco de la barra
        self._canvas.place()
        # La barra de estado se crea ya (si naciera con el primer mensaje, el texto daría un salto) y se oculta
        # en pantalla completa.
        self.statusBar()

        # Valores de los controles y de la vista, tomados de los ajustes (los rangos los limita la vista)
        self._view.set_font_size(self._tp.font_size)
        self._view.set_speed(self._tp.speed)
        self._view.set_mirror(self._tp.mirror)
        self._view.set_centered(self._tp.center_text)
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
        self._btn_guia.setChecked(self._view.is_guide_centered())
        self._view.set_fps(self._tp.fps)
        self._tp.fps = self._view.fps()
        indice = self._combo_fps.findData(self._tp.fps)
        if indice < 0:                       # un valor guardado que no está en la lista: se añade tal cual
            self._combo_fps.addItem(f"{self._tp.fps} fps", self._tp.fps)
            indice = self._combo_fps.count() - 1
        self._combo_fps.setCurrentIndex(indice)
        self._actualizar_fps()
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

        self._connect()
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
        self._bar = QFrame()
        self._bar.setObjectName("controlBar")
        self._bar.setStyleSheet("QFrame#controlBar { background: #23262d; }"
                                "QPushButton:disabled { background: #2b2f38; border-color: #3a3f4b; color: #6e7681; }")
        self._bar.setMouseTracking(True)
        capas = QVBoxLayout(self._bar)          # tres filas: reproducción y archivo, letra, y vista
        capas.setContentsMargins(12, 8, 12, 8)
        capas.setSpacing(6)
        fila = QHBoxLayout()
        fila.setSpacing(8)
        aspecto = QHBoxLayout()
        aspecto.setSpacing(8)
        vista = QHBoxLayout()
        vista.setSpacing(8)

        self._btn_editar = _boton("✎ Editar texto", "Editar o pegar el texto (Ctrl+E)", checkable=True)
        self._btn_abrir = _boton("📂 Abrir…", "Abrir un archivo .txt, .md o .docx (Ctrl+O). "
                                             "También puedes arrastrarlo a la ventana.")
        self._btn_borrar = _boton("🗑 Borrar texto", "Borra todo el texto para escribir o pegar otro "
                                                    "(pide confirmación).")
        self._btn_play = _boton("▶ Iniciar", "Iniciar o pausar (Espacio). Al pausar queda una bandera roja donde "
                                             "paraste y Cronista la recuerda para la próxima vez.")
        self._btn_play.setObjectName("primary")
        self._btn_inicio = _boton("⏮ Al principio", "Volver al principio del texto (Inicio)")
        self._btn_marca = _boton("🚩 Ir a la marca", "Va a la siguiente bandera roja (B). "
                                                    "Si hay varias, cada pulsación va a la siguiente.")
        self._btn_quitar_marcas = _boton("✕ Quitar marcas", "Quita todas las banderas rojas. "
                                                            "(Para quitar una sola, haz clic sobre ella.)")

        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(SPEED_MIN, SPEED_MAX)
        self._slider.setFixedWidth(160)
        self._slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._slider.setToolTip("Velocidad del desplazamiento (flecha arriba: más rápido; abajo: más lento)")
        self._lbl_velocidad = QLabel()
        self._lbl_velocidad.setToolTip("Velocidad actual (1 a 30)")

        self._combo_fuente = QComboBox()
        self._combo_fuente.setMinimumWidth(220)
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
        self._spin_espaciado.setToolTip("Separación extra entre letras. "
                                        "Separarlas ayuda a que no se amontonen al leer.")

        self._btn_espejo = _boton("⇋ Espejo", "Refleja el texto en horizontal para el cristal del "
                                              "teleprompter (M)", checkable=True)
        self._btn_centrar = _boton("↔ Centrar líneas", "Centra cada línea a lo ancho de la pantalla (C)",
                                   checkable=True)
        self._btn_guia = _boton("↕ Lectura al centro", "Pone la línea de lectura a media altura de la pantalla, a la "
                                                       "altura del objetivo de la cámara si usas el cristal",
                                checkable=True)
        self._btn_barra = _boton("▮ Barra de avance", "Muestra a la derecha por dónde vas del texto. "
                                                      "Púlsala o arrástrala para saltar.", checkable=True)
        self._combo_fps = QComboBox()
        self._combo_fps.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        for valor, nombre in FPS_OPCIONES:
            self._combo_fps.addItem(nombre, valor)
        ayuda_fps = ("Fotogramas por segundo al desplazar el texto: más = más fluido, pero gasta algo más. "
                     "Automático usa los Hz de tu monitor y baja solo si el equipo no da para más.")
        self._combo_fps.setToolTip(ayuda_fps)
        self._lbl_fps = QLabel()
        self._lbl_fps.setMinimumWidth(90)
        self._lbl_fps.setToolTip("Fotogramas por segundo que se están usando")
        self._slider_angulo = QSlider(Qt.Orientation.Horizontal)
        self._slider_angulo.setRange(ANGLE_MIN, ANGLE_MAX)
        self._slider_angulo.setPageStep(5)
        self._slider_angulo.setTickPosition(QSlider.TickPosition.TicksBelow)
        self._slider_angulo.setTickInterval(10)
        self._slider_angulo.setFixedWidth(180)
        self._slider_angulo.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        ayuda_angulo = ("Si miras la pantalla desde arriba (o desde abajo), inclina el texto para que desde tu "
                        "posición se vea recto, sin mover el monitor. Hacia la derecha: estás por encima; hacia "
                        "la izquierda: por debajo. Doble clic en el número: volver a 0°.")
        self._slider_angulo.setToolTip(ayuda_angulo)
        self._lbl_angulo = _EtiquetaDobleClic()
        self._lbl_angulo.setMinimumWidth(130)
        self._lbl_angulo.setToolTip(ayuda_angulo)
        self._btn_pantalla = _boton("⛶ Pantalla completa", "Pantalla completa (F11). En pantalla completa, "
                                                           "Esc sale.")
        self._lbl_progreso = QLabel("0 %")
        self._lbl_progreso.setToolTip("Progreso de la lectura")

        for w in (self._btn_editar, self._btn_abrir, self._btn_borrar, _separador(), self._btn_play,
                  self._btn_inicio, self._btn_marca, self._btn_quitar_marcas, _separador(), QLabel("Velocidad"),
                  self._slider, self._lbl_velocidad):
            fila.addWidget(w)
        fila.addStretch(1)
        fila.addWidget(self._lbl_progreso)
        for w in (QLabel("Tipo de letra"), self._combo_fuente, QLabel("Tamaño"), self._spin,
                  QLabel("Espaciado"), self._spin_espaciado, _separador(), QLabel("Fluidez"), self._combo_fps,
                  self._lbl_fps):
            aspecto.addWidget(w)
        aspecto.addStretch(1)
        for w in (self._btn_espejo, self._btn_centrar, self._btn_guia, self._btn_barra, _separador(),
                  QLabel("Ángulo de visión"), self._slider_angulo, self._lbl_angulo):
            vista.addWidget(w)
        vista.addStretch(1)
        from cronista import SUPPORT_URL
        if SUPPORT_URL:
            b_apoyo = _boton("♥ Apoyar", "Cronista es gratis. Si te sirve, puedes invitarme un café (se abre "
                                         "la página en tu navegador).")
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices
            b_apoyo.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(SUPPORT_URL)))
            vista.addWidget(b_apoyo)
        vista.addWidget(self._btn_pantalla)
        capas.addLayout(fila)
        capas.addLayout(aspecto)
        capas.addLayout(vista)

    def _connect(self) -> None:
        self._btn_editar.toggled.connect(self._set_editing)
        self._btn_abrir.clicked.connect(lambda: self._abrir_dialogo())
        self._btn_play.clicked.connect(lambda: self._view.toggle())
        self._btn_inicio.clicked.connect(lambda: self._view.rewind())
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

    def _install_shortcuts(self) -> None:
        """Atajos de lectura: su padre es la vista y el contexto exige que la vista (o un hijo) tenga el foco.
        Así, en la página de edición (la vista está oculta) la barra espaciadora escribe espacios."""
        lectura = [
            ("Space", self._view.toggle),
            ("Home", self._view.rewind),
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
            ("L", lambda: self._cycle_font(1)),
            ("Shift+L", lambda: self._cycle_font(-1)),
        ]
        globales = [
            ("F11", lambda: self._alternar_pantalla()),
            ("Esc", lambda: self._salir_pantalla_completa()),
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
        self._guardar_ajustes_pronto()

    def _on_letra(self, valor: int) -> None:
        self._view.set_font_size(valor)
        self._tp.font_size = self._view.font_size()
        self._guardar_ajustes_pronto()

    def _on_letra_desde_vista(self, valor: int) -> None:
        """Ctrl + rueda en la vista: actualiza el control sin volver a aplicar el valor."""
        self._spin.blockSignals(True)
        self._spin.setValue(valor)
        self._spin.blockSignals(False)
        self._tp.font_size = valor
        self._guardar_ajustes_pronto()

    def _on_fuente(self, indice: int) -> None:
        """Se eligió otra letra en el desplegable (o con L / Mayús+L)."""
        familia = self._combo_fuente.itemData(indice, Qt.ItemDataRole.UserRole)
        if not familia:
            return
        self._view.set_font_family(familia)
        self._tp.font_family = self._view.font_family()
        self._estilo_combo_fuente(familia)
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
        self._btn_play.setText("⏸ Pausar" if playing else "▶ Iniciar")

    def _on_progreso(self, p: float) -> None:
        self._lbl_progreso.setText(f"{round(p * 100)} %")

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
        if self.isFullScreen():
            self.showNormal()

    def _on_estado_cambiado(self) -> None:
        full = self.isFullScreen()
        if full == self._last_full:
            return
        self._last_full = full
        self._btn_pantalla.setText("⛶ Salir de pantalla completa" if full else "⛶ Pantalla completa")
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
        automatico = self.isFullScreen() and not self._editing
        self._timer_ocultar.stop()
        self._bar.show()
        self._view.set_cursor_hidden(False)
        if automatico:
            self._timer_ocultar.start()

    def _actividad(self) -> None:
        if not self.isFullScreen() or self._editing:
            return
        self._bar.show()
        self._view.set_cursor_hidden(False)
        self._timer_ocultar.start()

    def _on_idle(self) -> None:
        if not self.isFullScreen() or self._editing:
            return
        if self._bar.rect().contains(self._bar.mapFromGlobal(QCursor.pos())):
            self._timer_ocultar.start()     # el ratón está encima de la barra: no se oculta
            return
        self._bar.hide()
        self._view.set_cursor_hidden(True)

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
        if self._reanclar_marca:
            # Al arrancar, el texto se maquetó con el ancho provisional de la vista: con otro ancho cambian las
            # líneas. Ahora que tiene su tamaño real, se vuelve a llevar la bandera a la línea de lectura.
            self._reanclar_marca = False
            self._view.jump_to_mark(self._view.pause_mark())
        self._devolver_foco()

    def closeEvent(self, event: QCloseEvent) -> None:
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

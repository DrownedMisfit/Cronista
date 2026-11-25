"""Sistema de diseño de Cronista: el mismo de Reclaimer (editoria/ui/diseno.py), en pequeño.

  Colores    los del tema «oscuro» de Reclaimer y su acento «Acero». Capas de gris: fondo → controles → menús, con
             «profundidad sutil» (botones con un degradado leve y una línea de luz, campos hundidos).
  Letra      Inter (OFL, assets/fuentes) de 12 px en la barra y el panel; 11 px para etiquetas y títulos.
  Medidas    controles de 24 px, el botón principal de 26, márgenes múltiplos de 4, columna de etiquetas de 96 px.
  Iconos     SVG monocromos de Lucide (ISC, assets/iconos), teñidos: gris tenue normal, acento si está activo.
  Reglas     UN solo botón con el color de acento (Iniciar). Los interruptores activos llevan el acento al 15 % (no
             azul fuerte). Nada de emojis en los controles. La explicación, en el tooltip. Ningún texto cortado.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QFontDatabase, QFontMetrics, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QAbstractButton, QComboBox, QFrame, QLabel, QPushButton, QSizePolicy, QStyle,
                               QStyleOptionComboBox, QStylePainter)

ASSETS = Path(__file__).resolve().parent / "assets"
DIR_ICONOS = ASSETS / "iconos"

# ---- colores (Reclaimer: theme.py con el tema oscuro y diseno.fichas) --------------------------------------------
FONDO = "#1d1e22"          # barra y panel
FONDO_HONDO = "#141518"    # líneas de separación entre zonas
CAMPO = "#16171a"
BORDE = "#3b3e45"
BORDE_HI = "#565a63"
TEXTO = "#eceef2"
TENUE = "#c3c6ce"          # iconos y etiquetas (24/11: más claro, pidió más claridad)
APAGADO = "#80838c"        # deshabilitado
TITULO = "#aeb2ba"         # títulos de sección
CONTROL_ARRIBA, CONTROL_ABAJO, CONTROL_BORDE = "#3b3d43", "#323439", "#101114"
CONTROL_HOVER_ARRIBA, CONTROL_HOVER_ABAJO, CONTROL_PULSADO = "#464950", "#3c3f45", "#26282c"
MENU, MENU_BORDE = "#2b2c31", "#141518"
ACENTO = "#c9d1da"         # «Acero»
ACENTO_ARRIBA, ACENTO_ABAJO, ACENTO_BORDE = "#e3e8ee", "#b4bdc8", "#78838f"
ACENTO_TINTE = "#dfe5ec"   # texto e icono de un interruptor activo
SOBRE_ACENTO = "#16171a"
AMBAR = "#d6a24a"          # «para revisar» (igual que el ámbar de Reclaimer)

# ---- medidas -----------------------------------------------------------------------------------------------------
# (24/11: más grandes que en Reclaimer: «los botones están muy pequeños»; se lee de lejos, con la cámara delante)
ALTO_CONTROL = 32
ALTO_PRINCIPAL = 36
ALTO_BARRA = 56
M1, M2, M3 = 6, 10, 14
ANCHO_ETIQUETA = 120
ANCHO_PANEL = 390
ICONO = 18
LETRA, LETRA_P = 14, 13

_familia: str | None = None
_cache: dict = {}


def familia() -> str:
    """Inter (se carga una vez); si faltara, Segoe UI."""
    global _familia
    if _familia is None:
        _familia = "Segoe UI"
        archivo = ASSETS / "fuentes" / "InterVariable.ttf"
        if archivo.exists():
            fid = QFontDatabase.addApplicationFont(str(archivo))
            nombres = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
            if nombres:
                _familia = nombres[0]
    return _familia


def pixmap_icono(nombre: str, color: str = TENUE, tam: int = ICONO) -> QPixmap:
    """El icono pintado a doble resolución (nítido a 125 % y 150 %) y teñido de un color. Cada uno, una vez."""
    clave = (nombre, color, tam)
    pm = _cache.get(clave)
    if pm is not None:
        return pm
    pm = QPixmap(tam * 2, tam * 2)
    pm.setDevicePixelRatio(2.0)
    pm.fill(Qt.GlobalColor.transparent)
    try:
        texto = (DIR_ICONOS / f"{nombre}.svg").read_text(encoding="utf-8")
    except OSError:
        texto = ""
    if texto:
        from PySide6.QtSvg import QSvgRenderer
        r = QSvgRenderer(texto.replace("currentColor", color).encode("utf-8"))
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r.render(p, QRectF(0, 0, tam, tam))
        p.end()
    _cache[clave] = pm
    return pm


def icono(nombre: str, color: str = TENUE, tam: int = ICONO) -> QIcon:
    """Icono con su versión «activa» (acento) para los botones que se marcan y una apagada para deshabilitado."""
    ic = QIcon()
    ic.addPixmap(pixmap_icono(nombre, color, tam), QIcon.Mode.Normal, QIcon.State.Off)
    ic.addPixmap(pixmap_icono(nombre, ACENTO_TINTE, tam), QIcon.Mode.Normal, QIcon.State.On)
    ic.addPixmap(pixmap_icono(nombre, APAGADO, tam), QIcon.Mode.Disabled, QIcon.State.Off)
    ic.addPixmap(pixmap_icono(nombre, APAGADO, tam), QIcon.Mode.Disabled, QIcon.State.On)
    return ic


def poner_icono(boton: QAbstractButton, nombre: str, color: str = TENUE) -> None:
    boton.setIcon(icono(nombre, color))
    boton.setIconSize(QSize(ICONO, ICONO))
    boton.setProperty("icono_nombre", nombre)


def boton(texto: str, nombre_icono: str, ayuda: str, checkable: bool = False, tipo: str = "") -> QPushButton:
    """Botón del diseño. tipo: "" (normal, icono + texto), "icono" (solo icono, cuadrado), "principal" (el único con
    el acento), "aviso" (ámbar) o "plano" (sin fondo). No roba el foco: los atajos siguen en la vista."""
    b = QPushButton("" if tipo == "icono" else texto)
    b.setToolTip(ayuda)
    b.setAccessibleName(texto or ayuda.split(".")[0])
    b.setCheckable(checkable)
    b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    if tipo:
        b.setObjectName({"icono": "dIcono", "principal": "dPrincipal", "aviso": "dAviso", "plano": "dPlano"}[tipo])
    color = SOBRE_ACENTO if tipo == "principal" else AMBAR if tipo == "aviso" else TENUE
    if nombre_icono:
        poner_icono(b, nombre_icono, color)
    if tipo == "icono":
        b.setFixedSize(ALTO_CONTROL + 2, ALTO_CONTROL + 2)
    else:
        b.setFixedHeight(ALTO_PRINCIPAL if tipo == "principal" else ALTO_CONTROL)
    return b


def separador() -> QFrame:
    """Línea vertical de 1 px entre grupos de la barra."""
    s = QFrame()
    s.setObjectName("dSeparador")
    s.setFixedSize(1, 18)
    return s


def linea() -> QFrame:
    """Línea horizontal de 1 px encima de cada título de sección."""
    s = QFrame()
    s.setObjectName("dLinea")
    s.setFixedHeight(1)
    return s


def titulo(texto: str) -> QLabel:
    t = QLabel(texto.upper())
    t.setObjectName("dTitulo")
    return t


def etiqueta(texto: str, ancho: int | None = ANCHO_ETIQUETA) -> QLabel:
    """Etiqueta gris de la columna fija (o suelta, ancho=None). Si no cabe, se parte en dos líneas: nunca se corta."""
    e = QLabel(texto)
    e.setObjectName("dEtiqueta")
    if ancho:
        e.setFixedWidth(ancho)
        e.setWordWrap(True)
    return e


class EtiquetaElidida(QLabel):
    """Texto de una línea que, si no cabe, acaba en «…» (y se lee entero en el tooltip): nunca se corta a medias."""

    def __init__(self, texto: str = "", minimo: int = 60) -> None:
        super().__init__(texto)
        self._minimo = minimo
        self._ayuda = ""
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    def setToolTip(self, texto: str) -> None:  # noqa: N802
        self._ayuda = texto
        super().setToolTip(texto)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(self._minimo, super().minimumSizeHint().height())

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setPen(self.palette().color(self.foregroundRole()))
        p.setFont(self.font())
        fm = QFontMetrics(self.font())
        texto = fm.elidedText(self.text(), Qt.TextElideMode.ElideRight, self.width())
        p.drawText(self.rect(), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), texto)
        if texto != self.text():
            super().setToolTip(self.text() + ("\n\n" + self._ayuda if self._ayuda else ""))
        elif self.toolTip() != self._ayuda:
            super().setToolTip(self._ayuda)
        p.end()


class ComboElidido(QComboBox):
    """Desplegable cuyo texto, si no cabe, acaba en «…» (Qt lo cortaba a medias) y se lee entero en el tooltip."""

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QStylePainter(self)
        opt = QStyleOptionComboBox()
        self.initStyleOption(opt)
        p.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, opt)
        campo = self.style().subControlRect(QStyle.ComplexControl.CC_ComboBox, opt,
                                            QStyle.SubControl.SC_ComboBoxEditField, self)
        completo = opt.currentText
        opt.currentText = self.fontMetrics().elidedText(completo, Qt.TextElideMode.ElideRight, campo.width() - 4)
        p.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, opt)
        ayuda = self.property("ayuda") or ""
        if opt.currentText != completo:
            texto = completo + ("\n\n" + ayuda if ayuda else "")
            if self.toolTip() != texto:
                self.setToolTip(texto)
        elif self.toolTip() != ayuda and ayuda:
            self.setToolTip(ayuda)


def _grad(arriba: str, abajo: str, luz: str | None = None) -> str:
    if luz:
        return f"qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 {luz},stop:0.06 {arriba},stop:1 {abajo})"
    return f"qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 {arriba},stop:1 {abajo})"


def qss() -> str:
    """La hoja de estilo de la barra y del panel de ajustes (no toca el resto del programa)."""
    flecha = (DIR_ICONOS / "flecha-abajo.svg").as_posix()
    arriba = (DIR_ICONOS / "flecha-arriba.svg").as_posix()
    luz = "#4a4c52"
    return f"""
QWidget {{ font-family: '{familia()}'; font-size: {LETRA}px; color: {TEXTO}; }}
QFrame#controlBar {{ background: {FONDO}; border-top: 1px solid {FONDO_HONDO}; }}
QWidget#ajustes, QWidget#ajustesContenido {{ background: {FONDO}; }}
QScrollArea#ajustesScroll {{ background: {FONDO}; border: none; border-left: 1px solid {FONDO_HONDO}; }}
QToolTip {{ background: {MENU}; color: {TEXTO}; border: 1px solid {MENU_BORDE}; padding: 4px 6px; }}

QPushButton {{ background: {_grad(CONTROL_ARRIBA, CONTROL_ABAJO, luz)}; border: 1px solid {CONTROL_BORDE};
    border-radius: 5px; padding: 0 12px; color: {TEXTO}; text-align: center; }}
QPushButton:hover:!checked {{ background: {_grad(CONTROL_HOVER_ARRIBA, CONTROL_HOVER_ABAJO, luz)}; }}
QPushButton:pressed:!checked {{ background: {CONTROL_PULSADO}; }}
QPushButton:checked {{ background: rgba(201,209,218,0.24); border-color: rgba(220,226,233,0.90);
    color: #ffffff; }}
QPushButton:checked:hover {{ background: rgba(201,209,218,0.32); }}
QPushButton:disabled {{ background: {FONDO}; border-color: {BORDE}; color: {APAGADO}; }}
QPushButton#dIcono {{ padding: 0; }}
QPushButton#dPrincipal {{ background: {_grad(ACENTO_ARRIBA, ACENTO_ABAJO, "#f4f6f9")}; border: 1px solid {ACENTO_BORDE};
    color: {SOBRE_ACENTO}; font-weight: 600; padding: 0 18px; }}
QPushButton#dPrincipal:hover {{ background: {_grad("#eef1f5", "#c4ccd5", "#f8f9fb")}; }}
QPushButton#dPrincipal:pressed {{ background: #a9b3bf; }}
QPushButton#dAviso {{ background: rgba(214,162,74,0.10); border-color: rgba(214,162,74,0.55); color: {AMBAR}; }}
QPushButton#dAviso:hover {{ background: rgba(214,162,74,0.18); }}
QPushButton#dPlano {{ background: transparent; border: none; color: {TENUE}; }}
QPushButton#dPlano:hover {{ color: {TEXTO}; }}

QComboBox, QSpinBox {{ background: {CAMPO}; border: 1px solid #222327; border-top-color: #0d0e10;
    border-bottom-color: #44474e; border-radius: 4px; min-height: {ALTO_CONTROL - 2}px; max-height: {ALTO_CONTROL}px;
    padding: 0 6px; color: {TEXTO}; }}
QComboBox:hover, QSpinBox:hover {{ border-color: {BORDE_HI}; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox::down-arrow {{ image: url({flecha}); width: 12px; height: 12px; }}
QComboBox QAbstractItemView {{ background: {MENU}; border: 1px solid {MENU_BORDE}; color: {TEXTO};
    selection-background-color: #3a3d44; outline: none; }}
QSpinBox::up-button, QSpinBox::down-button {{ border: none; width: 22px; background: transparent; }}
QSpinBox::up-arrow {{ image: url({arriba}); width: 11px; height: 11px; }}
QSpinBox::down-arrow {{ image: url({flecha}); width: 11px; height: 11px; }}

QSlider {{ min-height: 24px; }}
QSlider::groove:horizontal {{ height: 4px; background: {BORDE}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACENTO_ABAJO}; height: 4px; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {ACENTO_ARRIBA}; border: 1px solid {ACENTO_BORDE}; width: 16px; height: 16px;
    margin: -7px 0; border-radius: 9px; }}
QSlider::handle:horizontal:hover {{ background: #ffffff; }}

QLabel {{ background: transparent; }}
QLabel#dEtiqueta, QLabel#dDato {{ color: {TENUE}; font-size: {LETRA_P}px; }}
QLabel#dTitulo {{ color: {TITULO}; font-size: {LETRA_P}px; font-weight: 600; letter-spacing: 0.6px; }}
QFrame#dSeparador, QFrame#dLinea {{ background: {BORDE}; border: none; }}
QScrollBar:vertical {{ background: {FONDO}; width: 8px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {BORDE_HI}; border-radius: 4px; min-height: 24px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
"""

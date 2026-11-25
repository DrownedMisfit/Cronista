"""Icono de Cronista y ventana de arranque (en la línea de Reclaimer).

- icon(): el icono (assets/cronista.ico, generado con tools/icono.py) para la ventana y la barra de tareas.
- set_windows_app_id() (ANTES de la QApplication) y set_app_identity(): identificador propio en la barra de tareas
  de Windows (si no, la agrupa con Python y enseña el icono de Python) e icono de la aplicación.
- ArranqueWindow: ventana oscura con el símbolo en relieve metálico, «Cronista» y «Abriendo…». Cuando la ventana
  principal ya está montada pasa UNA onda de luz cálida por el símbolo (como la de Reclaimer: un resplandor que sale
  de detrás y un reflejo que cruza su metal) y se funde. Cronista abre en menos de un segundo, así que la onda va al
  final y es corta (~0,9 s); un clic o una tecla la saltan, y teleprompter.intro_animation = false la quita.

Nunca impide abrir: cualquier fallo y se abre la ventana principal sin más.
"""
from __future__ import annotations

import logging
import math
import sys
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QByteArray, QEasingCurve, QPointF, QRect, QRectF, Qt, QVariantAnimation
from PySide6.QtGui import (QColor, QFont, QGuiApplication, QIcon, QImage, QLinearGradient, QPainter, QPalette,
                           QPixmap, QRadialGradient)
from PySide6.QtWidgets import QApplication, QWidget

from cronista import APP_NAME

log = logging.getLogger(__name__)

ASSETS = Path(__file__).resolve().parent / "assets"
APP_ID = "Cronista.Teleprompter"
TAMANO = (440, 300)              # ventana de arranque (px lógicos), como la de Reclaimer
LOGO_ALTO = 108                  # alto del símbolo (px lógicos)
LOGO_Y = 56                      # dónde empieza el símbolo
# La forma del símbolo dentro de su caja de 100 × 100 (ver tools/icono.py) y el margen del viewBox (-3 … 103).
_BBOX = (4.0, 12.0, 96.0, 88.0)
_VIEWBOX = (-3.0, 106.0)
BRILLO_COLOR = (255, 222, 150)   # resplandor cálido (el de Reclaimer)
ONDA_MS = 900                    # lo que tarda la onda en cruzar el símbolo
FUNDIDO_MS = 180


def icon() -> QIcon:
    ruta = ASSETS / "cronista.ico"
    return QIcon(str(ruta)) if ruta.exists() else QIcon()


def set_windows_app_id() -> None:
    """En Windows, el identificador propio en la barra de tareas. Tiene que ir ANTES de crear la QApplication (si no,
    sale el icono de Python: aprendido en Reclaimer)."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except Exception:  # noqa: BLE001 - sin esto solo cambia el agrupado en la barra de tareas
            log.debug("No se pudo fijar el AppUserModelID", exc_info=True)


def set_app_identity(app: QApplication) -> None:
    """Icono de la aplicación (todas sus ventanas). (El identificador de Windows: set_windows_app_id, antes.)"""
    set_windows_app_id()                                        # (por si no se llamó antes; sin efecto si ya estaba)
    app.setWindowIcon(icon())


# ---------------------------------------------------------------- dibujo (se pinta una vez, ~decenas de ms)

def _mascara(alto_px: int) -> QImage | None:
    """La forma del símbolo a alto_px de alto, desde el vectorial (assets/cronista.svg), recortada a lo que ocupa."""
    from PySide6.QtSvg import QSvgRenderer
    r = QSvgRenderer(QByteArray((ASSETS / "cronista.svg").read_bytes()))
    if not r.isValid():
        return None
    x0, y0, x1, y1 = _BBOX
    escala = alto_px / (y1 - y0)
    lado = math.ceil(_VIEWBOX[1] * escala)
    im = QImage(lado, lado, QImage.Format.Format_ARGB32_Premultiplied)
    im.fill(Qt.GlobalColor.transparent)
    p = QPainter(im)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    r.render(p, QRectF(0, 0, lado, lado))
    p.end()
    return im.copy(QRect(math.floor((x0 - _VIEWBOX[0]) * escala), math.floor((y0 - _VIEWBOX[0]) * escala),
                         math.ceil((x1 - x0) * escala) + 1, alto_px + 1))


def _difuminar(im: QImage, r: float, pasos: int = 2) -> QImage:
    """Desenfoque suave: reducir y volver a ampliar."""
    for _ in range(pasos):
        w, h = max(1, int(im.width() / r)), max(1, int(im.height() / r))
        im = im.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation).scaled(
            im.width(), im.height(), Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
        r = max(1.0, r * 0.6)
    return im


def _tenir(mask: QImage, brocha, dx: float = 0.0, dy: float = 0.0, lienzo: tuple[int, int] | None = None,
           origen: tuple[float, float] = (0.0, 0.0)) -> QImage:
    """La forma (desplazada) rellena con una brocha."""
    w, h = lienzo or (mask.width(), mask.height())
    im = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
    im.fill(Qt.GlobalColor.transparent)
    p = QPainter(im)
    p.drawImage(QPointF(origen[0] + dx, origen[1] + dy), mask)      # primero la forma, luego el color solo en ella
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    p.fillRect(im.rect(), brocha)
    p.end()
    return im


def _recortar(im: QImage, m: QImage, modo) -> QImage:
    p = QPainter(im)
    p.setCompositionMode(modo)
    p.drawImage(0, 0, m)
    p.end()
    return im


def logo_en_relieve(alto_px: int, dpr: float) -> tuple[QImage, int] | None:
    """El símbolo en metal acero/cromo (claro arriba, banda de reflejo, más oscuro abajo), con bisel de luz, sombra
    interior y sombra proyectada difusa: el mismo relieve sutil que el de Reclaimer. (imagen, margen) en px físicos."""
    mask = _mascara(alto_px)
    if mask is None:
        return None
    w, h = mask.width(), mask.height()
    metal = QLinearGradient(0, 0, 0, h)
    for pos, col in ((0.0, "#f3f5f8"), (0.38, "#cdd2d9"), (0.52, "#a3a9b2"), (0.60, "#b9bfc7"),
                     (0.82, "#9399a3"), (1.0, "#7d838d")):
        metal.setColorAt(pos, QColor(col))
    b = 1.25 * dpr
    fuera = QPainter.CompositionMode.CompositionMode_DestinationOut
    luz = _recortar(_tenir(mask, QColor(255, 255, 255, 165)), _tenir(mask, QColor(0, 0, 0), b, b), fuera)
    sombra_int = _recortar(_difuminar(_recortar(_tenir(mask, QColor(0, 0, 0, 150)),
                                                _tenir(mask, QColor(0, 0, 0), -b, -b), fuera), 1.6 * dpr),
                           mask, QPainter.CompositionMode.CompositionMode_DestinationIn)
    m = int(16 * dpr)
    proyectada = _difuminar(_tenir(mask, QColor(0, 0, 0), lienzo=(w + 2 * m, h + 2 * m), origen=(m, m)), 5.0 * dpr)
    out = QImage(w + 2 * m, h + 2 * m, QImage.Format.Format_ARGB32_Premultiplied)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setOpacity(0.55)
    p.drawImage(QPointF(0, 4 * dpr), proyectada)
    p.setOpacity(1.0)
    p.drawImage(m, m, _tenir(mask, metal))
    p.drawImage(m, m, sombra_int)
    p.drawImage(m, m, luz)
    p.end()
    return out, m


def pintar_arranque(fondo: QColor, texto: QColor, dpr: float) -> tuple[QPixmap, QRect, QImage, QImage]:
    """(imagen de la ventana, recuadro del símbolo en px físicos, resplandor, reflejo) para la onda."""
    ancho, alto = TAMANO
    img = QImage(round(ancho * dpr), round(alto * dpr), QImage.Format.Format_ARGB32_Premultiplied)
    img.setDevicePixelRatio(dpr)
    img.fill(fondo)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    halo = QRadialGradient(QPointF(ancho / 2, LOGO_Y + LOGO_ALTO * 0.5), ancho * 0.62)   # leve foco tras el símbolo
    halo.setColorAt(0.0, QColor(fondo).lighter(124))
    halo.setColorAt(1.0, QColor(fondo))
    p.fillRect(QRectF(0, 0, ancho, alto), halo)
    caja = QRect()
    mask = _mascara(round(LOGO_ALTO * dpr))
    r = logo_en_relieve(round(LOGO_ALTO * dpr), dpr)
    if r is not None and mask is not None:
        logo, m = r
        x = round((ancho * dpr - logo.width()) / 2)
        y = round(LOGO_Y * dpr) - m
        p.save()
        p.scale(1 / dpr, 1 / dpr)
        p.drawImage(QPointF(x, y), logo)
        p.restore()
        g = int(22 * dpr)                                       # margen para el resplandor
        caja = QRect(x + m - g, y + m - g, mask.width() + 2 * g, mask.height() + 2 * g).intersected(
            QRect(0, 0, img.width(), img.height()))
    f = QFont()
    f.setFamilies(["Segoe UI Variable Display", "Segoe UI"])
    f.setPointSizeF(16)
    f.setBold(True)
    f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    p.setFont(f)
    linea = QRectF(0, 196, ancho, 34)
    p.setPen(QColor(0, 0, 0, 120))                              # bisel muy leve, como el del símbolo
    p.drawText(linea.translated(0, 1.0), Qt.AlignmentFlag.AlignCenter, APP_NAME)
    p.setPen(QColor(255, 255, 255, 34))
    p.drawText(linea.translated(0, -0.6), Qt.AlignmentFlag.AlignCenter, APP_NAME)
    p.setPen(texto)
    p.drawText(linea, Qt.AlignmentFlag.AlignCenter, APP_NAME)
    f.setPointSizeF(9.5)
    f.setBold(False)
    p.setFont(f)
    p.setPen(QColor(texto).darker(150))
    p.drawText(QRectF(0, 234, ancho, 24), Qt.AlignmentFlag.AlignCenter, "Abriendo…")
    p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    p.setPen(QColor(fondo).lighter(150))                        # borde sutil de 1 px físico
    p.scale(1 / dpr, 1 / dpr)
    p.drawRect(0, 0, img.width() - 1, img.height() - 1)
    p.end()

    resplandor = reflejo = QImage()
    if not caja.isNull() and mask is not None:
        g = int(22 * dpr)
        tam = (caja.width(), caja.height())
        lejos = _difuminar(_tenir(mask, QColor(*BRILLO_COLOR), lienzo=tam, origen=(g, g)), 9.0 * dpr, 3)
        cerca = _difuminar(_tenir(mask, QColor(*BRILLO_COLOR), lienzo=tam, origen=(g, g)), 3.0 * dpr, 3)
        resplandor = QImage(*tam, QImage.Format.Format_ARGB32_Premultiplied)
        resplandor.fill(Qt.GlobalColor.transparent)
        q = QPainter(resplandor)
        q.drawImage(0, 0, lejos)
        q.drawImage(0, 0, lejos)
        q.setOpacity(0.85)
        q.drawImage(0, 0, cerca)
        q.setOpacity(1.0)
        q.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationOut)   # sale de DETRÁS del símbolo
        q.drawImage(g, g, mask)
        q.end()
        reflejo = _tenir(mask, QColor(255, 244, 214), lienzo=tam, origen=(g, g))
    pm = QPixmap.fromImage(img)
    pm.setDevicePixelRatio(dpr)
    return pm, caja, resplandor, reflejo


def _franja(capa: QImage, t: float, banda: float, alfa: float) -> QImage:
    """La capa vista solo en una franja vertical que cruza de izquierda a derecha (t de 0 a 1), más fuerte en medio."""
    w = capa.width()
    fuerza = math.sin(math.pi * t) * alfa
    cx = -banda + (w + 2 * banda) * t
    out = capa.copy()
    p = QPainter(out)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
    grad = QLinearGradient(cx - banda, 0, cx + banda, 0)
    grad.setColorAt(0.0, QColor(0, 0, 0, 0))
    grad.setColorAt(0.5, QColor(0, 0, 0, round(255 * max(0.0, min(1.0, fuerza)))))
    grad.setColorAt(1.0, QColor(0, 0, 0, 0))
    p.fillRect(out.rect(), grad)
    p.end()
    return out


def pintar_cuadro(painter: QPainter, pm: QPixmap, caja: QRect, resplandor: QImage, reflejo: QImage,
                  t: float | None, dpr: float) -> None:
    """Un cuadro de la ventana de arranque: la imagen y, si t no es None, la onda en ese punto (0 → 1)."""
    painter.drawPixmap(0, 0, pm)
    if t is None or caja.isNull() or resplandor.isNull():
        return
    painter.save()
    painter.scale(1 / dpr, 1 / dpr)
    w = resplandor.width()
    painter.drawImage(caja.topLeft(), _franja(resplandor, t, w * 0.42, 1.0))
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)              # el reflejo suma luz
    painter.drawImage(caja.topLeft(), _franja(reflejo, t, w * 0.16, 0.32))
    painter.restore()


class ArranqueWindow(QWidget):
    """La ventana de arranque. play(listo) pasa la onda una vez, llama a listo() y se funde."""

    def __init__(self, fondo: QColor, texto: QColor) -> None:
        # (sin «siempre encima», como en Reclaimer: un aviso al arrancar no puede quedar tapado detrás)
        super().__init__(None, Qt.WindowType.SplashScreen | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("ventana_de_arranque")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setFixedSize(*TAMANO)
        self.setWindowTitle(APP_NAME)
        pantalla = QGuiApplication.primaryScreen()
        self._dpr = max(1.0, float(pantalla.devicePixelRatio())) if pantalla is not None else 1.0
        self._pm, self._caja, self._resplandor, self._reflejo = pintar_arranque(fondo, texto, self._dpr)
        self._t: float | None = None
        self._listo: Callable[[], None] | None = None
        self._anim: QVariantAnimation | None = None
        if pantalla is not None:
            g = self.frameGeometry()
            g.moveCenter(pantalla.availableGeometry().center())
            self.move(g.topLeft())

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        pintar_cuadro(p, self._pm, self._caja, self._resplandor, self._reflejo, self._t, self._dpr)

    def play(self, listo: Callable[[], None]) -> None:
        self._listo = listo
        anim = QVariantAnimation(self)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(ONDA_MS)
        anim.setEasingCurve(QEasingCurve.Type.InOutSine)       # suave al entrar y al salir
        anim.valueChanged.connect(self._en_onda)
        anim.finished.connect(self._terminar)
        self._anim = anim
        anim.start()

    def _en_onda(self, valor) -> None:
        self._t = float(valor)
        self.update()

    def _terminar(self) -> None:
        listo, self._listo = self._listo, None
        if listo is not None:
            try:
                listo()
            finally:
                self.raise_()                                   # se funde por encima de la ventana ya abierta
                fundido = QVariantAnimation(self)
                fundido.setStartValue(1.0)
                fundido.setEndValue(0.0)
                fundido.setDuration(FUNDIDO_MS)
                fundido.valueChanged.connect(lambda v: self.setWindowOpacity(float(v)))
                fundido.finished.connect(self.close)
                fundido.start()

    def mousePressEvent(self, _event) -> None:  # noqa: N802 - un clic la salta
        self._saltar()

    def keyPressEvent(self, _event) -> None:  # noqa: N802 - una tecla también
        self._saltar()

    def _saltar(self) -> None:
        if self._anim is not None:
            self._anim.stop()
        listo, self._listo = self._listo, None
        if listo is not None:
            listo()
        self.close()


def mostrar_arranque(app: QApplication) -> ArranqueWindow | None:
    """Enseña la ventana de arranque ya pintada (con los colores del tema). None si no se pudo o si no hay pantalla
    de verdad (pruebas sin ventanas)."""
    if QGuiApplication.platformName() in ("offscreen", "minimal"):
        return None
    try:
        pal = app.palette()
        ventana = ArranqueWindow(pal.color(QPalette.ColorRole.Window), pal.color(QPalette.ColorRole.WindowText))
        ventana.show()
        ventana.repaint()
        app.processEvents()
        return ventana
    except Exception:  # noqa: BLE001 - la ventana de arranque nunca impide abrir
        log.warning("No se pudo enseñar la ventana de arranque", exc_info=True)
        return None

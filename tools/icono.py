"""Genera el icono de Cronista (estilo Reclaimer): símbolo geométrico de un solo color, simétrico, con contorno oscuro.

El símbolo: un teleprompter visto de frente (idea «A», elegida el 24/11): el cristal con las líneas del guion y la
lente de la cámara detrás, cortando las líneas del medio (sin pie: elegido así). Escribe en cronista/teleprompter/assets:
  cronista.svg          el símbolo, claro, sin contorno (para la ventana de arranque)
  cronista_icono.svg    con contorno oscuro (el del icono)
  cronista_256.png      vista previa
  cronista.ico          16, 20, 24, 32, 40, 48, 64, 128 y 256 px (PNG dentro del .ico)

Uso: .venv\\Scripts\\python.exe tools\\icono.py   (sin ventanas)
"""
from __future__ import annotations

import math
import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

CLARO = "#ECECEA"
OSCURO = "#151A1D"
CARPETA = Path(__file__).resolve().parents[1] / "cronista" / "teleprompter" / "assets"
TAMANOS = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def _anillo(cx: float, cy: float, r_ext: float, r_int: float) -> str:
    """Un anillo como trazado (par/impar): círculo exterior + interior."""
    def circulo(r: float) -> str:
        return (f"M{cx - r:.2f} {cy:.2f} A{r:.2f} {r:.2f} 0 1 0 {cx + r:.2f} {cy:.2f} "
                f"A{r:.2f} {r:.2f} 0 1 0 {cx - r:.2f} {cy:.2f} Z ")
    return circulo(r_ext) + circulo(r_int)


def _arco(cx: float, cy: float, r_ext: float, r_int: float, desde: float, hasta: float) -> str:
    """Un trozo de anillo entre dos ángulos (grados; 0 = derecha, 90 = abajo)."""
    def punto(r: float, a: float) -> str:
        return f"{cx + r * math.cos(math.radians(a)):.2f} {cy + r * math.sin(math.radians(a)):.2f}"
    largo = 1 if (hasta - desde) % 360 > 180 else 0
    return (f"M{punto(r_ext, desde)} A{r_ext} {r_ext} 0 {largo} 1 {punto(r_ext, hasta)} "
            f"L{punto(r_int, hasta)} A{r_int} {r_int} 0 {largo} 0 {punto(r_int, desde)} Z")


def _rect(x: float, y: float, w: float, h: float) -> str:
    return f"M{x:.2f} {y:.2f} H{x + w:.2f} V{y + h:.2f} H{x:.2f} Z "


def formas(simple: bool = False) -> list[tuple[str, str]]:
    """Las piezas del símbolo (nombre, trazado), en una caja de 100 × 100: el cristal de un teleprompter visto de
    frente (marco) con las líneas del guion y, detrás, la lente de la cámara, que corta las líneas del medio.
    simple: para 32 px o menos (más grueso, sin las líneas cortadas ni el punto)."""
    cx, cy = 50.0, 50.0
    if simple:
        return [
            ("marco", _rect(2, 11, 96, 78) + _rect(14, 23, 72, 54)),
            ("linea", _rect(21, 28.5, 58, 8)), ("linea", _rect(21, 64, 44, 8)),
            ("lente", _anillo(cx, cy, 12.5, 6.0)),
        ]
    piezas = [("marco", _rect(4, 12, 92, 76) + _rect(13, 21, 74, 58))]  # el cristal (y su marco)
    # Líneas del guion: arriba y abajo enteras; las del medio, cortadas por la lente.
    piezas += [("linea", _rect(20, 26.5, 60, 7.5)), ("linea", _rect(20, 67, 44, 7.5))]
    for y in (40.0, 53.0):
        piezas += [("linea", _rect(20, y, 13, 7)), ("linea", _rect(67, y, 13, 7))]
    # La lente de la cámara, detrás del cristal: un aro grueso con su punto (como el círculo del centro de Reclaimer).
    piezas.append(("lente", _anillo(cx, cy, 14.5, 8.5)))
    piezas.append(("punto", _anillo(cx, cy, 4.0, 0.01)))
    return piezas


def svg(contorno: bool, simple: bool = False) -> str:
    """El símbolo en SVG. Con contorno: cada pieza con borde oscuro (el icono, legible sobre cualquier fondo)."""
    cuerpo = "".join(f'<path d="{d}" fill-rule="evenodd"/>' for _, d in formas(simple))
    trazo = (f' stroke="{OSCURO}" stroke-width="{5.0 if simple else 3.4}" stroke-linejoin="miter" paint-order="stroke"'
             if contorno else "")
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="-3 -3 106 106">'
            f'<g fill="{CLARO}"{trazo}>' + cuerpo + "</g></svg>")


def _render(texto_svg: str, lado: int):
    from PySide6.QtCore import QByteArray, QRectF, Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer
    r = QSvgRenderer(QByteArray(texto_svg.encode("utf-8")))
    im = QImage(lado, lado, QImage.Format.Format_ARGB32_Premultiplied)
    im.fill(Qt.GlobalColor.transparent)
    p = QPainter(im)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    r.render(p, QRectF(0, 0, lado, lado))
    p.end()
    return im


def _png(im) -> bytes:
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    datos = QByteArray()
    buf = QBuffer(datos)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    im.save(buf, "PNG")
    buf.close()
    return bytes(datos)


def escribir_ico(ruta: Path, texto_svg: str, texto_simple: str) -> None:
    """.ico con varias resoluciones, cada una un PNG (Windows Vista y posteriores lo leen así); hasta 32 px, el
    símbolo simplificado."""
    imagenes = [_png(_render(texto_simple if n <= 32 else texto_svg, n)) for n in TAMANOS]
    cabecera = struct.pack("<HHH", 0, 1, len(TAMANOS))
    directorio, cuerpo = b"", b""
    desplazamiento = 6 + 16 * len(TAMANOS)
    for n, datos in zip(TAMANOS, imagenes):
        directorio += struct.pack("<BBBBHHII", n % 256, n % 256, 0, 0, 1, 32, len(datos), desplazamiento)
        cuerpo += datos
        desplazamiento += len(datos)
    ruta.write_bytes(cabecera + directorio + cuerpo)


def main() -> int:
    from PySide6.QtGui import QGuiApplication
    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])  # noqa: F841
    CARPETA.mkdir(parents=True, exist_ok=True)
    claro, icono = svg(False), svg(True)
    (CARPETA / "cronista.svg").write_text(claro, encoding="utf-8")
    (CARPETA / "cronista_icono.svg").write_text(icono, encoding="utf-8")
    _render(icono, 256).save(str(CARPETA / "cronista_256.png"))
    escribir_ico(CARPETA / "cronista.ico", icono, svg(True, simple=True))
    print("Icono escrito en", CARPETA)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

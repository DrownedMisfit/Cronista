"""Tipos de letra del teleprompter: una lista corta de los más legibles según la investigación.

La letra que mejor se lee cambia de una persona a otra (Wallace et al., ACM TOCHI 2022: de media, un 35 % más de
velocidad entre la más rápida y la más lenta de 5 letras por lector), así que se ofrecen varias para probarlas con
el texto real. Las de la carpeta fonts/ (licencias libres, ver fonts/LICENCIAS.md) se cargan solo para la aplicación,
sin instalarlas en Windows; el resto son de Windows y solo aparecen si el equipo las tiene.

No hay estudios de qué letra ayuda cuando la vista se adelanta a la voz: las de ancho fijo y demás añadidas en la
0.9 son candidatas para probar, no una solución demostrada.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from PySide6.QtGui import QFontDatabase

from cronista.core.paths import app_root

log = logging.getLogger(__name__)

DEFAULT_FAMILY = "Segoe UI"


@dataclass(frozen=True)
class FontOption:
    family: str        # nombre de familia que entiende Qt
    label: str         # nombre que ve el usuario
    note: str          # por qué está en la lista (ayuda emergente)
    file: str = ""     # archivo en fonts/ si viene con Cronista; vacío = letra de Windows


FONT_OPTIONS: tuple[FontOption, ...] = (
    FontOption("Atkinson Hyperlegible Next", "Atkinson Hyperlegible",
               "Del Braille Institute, para baja visión: cada letra se distingue de las parecidas (I l 1, O 0).",
               "AtkinsonHyperlegibleNext.ttf"),
    FontOption("Verdana", "Verdana",
               "De las más rápidas de leer en estudios con muchos lectores, también con dislexia."),
    FontOption("Luciole", "Luciole",
               "Creada con un centro francés de discapacidad visual, para personas con baja visión.",
               "Luciole-Regular.ttf"),
    FontOption("Lexend", "Lexend",
               "Letras anchas y separadas, pensada para cansar menos la vista al leer.", "Lexend.ttf"),
    FontOption("Tahoma", "Tahoma", "Como Verdana, pero más estrecha: caben más palabras por línea."),
    FontOption("Segoe UI", "Segoe UI", "La letra de Windows (la que usaba Cronista hasta ahora)."),
    FontOption("Arial", "Arial", "Clásica; legible en estudios con lectores con dislexia."),
    FontOption("Calibri", "Calibri", "Redondeada y compacta."),
    FontOption("Sitka Text", "Sitka",
               "Con remates (serifa); Microsoft la diseñó con estudios de legibilidad."),
    # Candidatas para leer en voz alta sin que la vista se adelante. De Courier Prime solo se incluye la negrita:
    # es la variante parecida a la única con datos en voz alta (Courier-Bold, Mansfield et al. 1996).
    FontOption("Courier Prime", "Courier Prime Negrita",
               "De máquina de escribir, todas las letras ocupan lo mismo. Una Courier negrita se leyó en voz alta "
               "un 5 % más lento que Times en un estudio: puede ayudar a no correr. Hay que probarla.",
               "CourierPrime-Bold.ttf"),
    FontOption("Georgia", "Georgia", "Con remates y formas amplias."),
    FontOption("Andika", "Andika", "Hecha para quien aprende a leer: las letras parecidas se distinguen bien.",
               "Andika-Regular.ttf"),
    FontOption("CMU Serif", "CMU (Computer Modern)",
               "Con remates, clásica de libros científicos. Salió bien en un estudio con lectores con dislexia.",
               "cmunrm.otf"),
    FontOption("Courier New", "Courier New", "De máquina de escribir, ancho fijo y trazo fino."),
    FontOption("Consolas", "Consolas", "Ancho fijo, más compacta que Courier."),
    FontOption("Cascadia Mono", "Cascadia Mono", "Ancho fijo, moderna y sin remates."),
    FontOption("Intel One Mono", "Intel One Mono", "Ancho fijo, pensada para que se lea con claridad.",
               "IntelOneMono-Regular.ttf"),
    FontOption("Literata", "Literata", "Con remates, diseñada para leer textos largos.", "Literata.ttf"),
    FontOption("B612", "B612", "Diseñada para las pantallas de cabina de los aviones.", "B612-Regular.ttf"),
)

_loaded = False


def load_bundled_fonts() -> None:
    """Carga en Qt las letras de la carpeta fonts/ (solo para Cronista). Requiere QApplication; idempotente."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    carpeta = app_root() / "fonts"
    for opt in FONT_OPTIONS:
        if not opt.file:
            continue
        ruta = carpeta / opt.file
        if not ruta.exists():
            log.warning("Falta la letra %s (%s)", opt.label, ruta)
            continue
        if QFontDatabase.addApplicationFont(str(ruta)) < 0:
            log.warning("Qt no pudo cargar la letra %s (%s)", opt.label, ruta)


def available_options() -> list[FontOption]:
    """Las opciones de FONT_OPTIONS que se pueden usar en este equipo, en el mismo orden."""
    load_bundled_fonts()
    return [opt for opt in FONT_OPTIONS if QFontDatabase.hasFamily(opt.family)]


def resolve_family(family: str) -> str:
    """La familia pedida si está disponible; si no (p. ej. falta la carpeta fonts/), la de Windows por defecto."""
    disponibles = {opt.family for opt in available_options()}
    if family in disponibles:
        return family
    if family:
        log.warning("La letra «%s» no está disponible; se usa %s", family, DEFAULT_FAMILY)
    return DEFAULT_FAMILY

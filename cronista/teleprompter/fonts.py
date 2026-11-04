"""Tipos de letra del teleprompter: una lista corta de los más legibles según la investigación.

La letra que mejor se lee cambia de una persona a otra (Wallace et al., ACM TOCHI 2022: hasta un 35 % más de
velocidad entre la mejor y la peor letra de cada lector), así que se ofrecen varias para probarlas con el texto
real. Tres vienen con Cronista (carpeta fonts/, licencias libres) y se cargan solo para la aplicación, sin
instalarlas en Windows; el resto son de Windows.
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

"""Texto del teleprompter: carga de .txt, .md y .docx, limpieza de Markdown y conversión de la velocidad.

Módulo sin Qt: todo lo que hay aquí se puede probar sin ventana.
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path

log = logging.getLogger(__name__)

SPEED_MIN = 1
SPEED_MAX = 30
FONT_MIN = 20
FONT_MAX = 200
MARGIN_MIN = 0
MARGIN_MAX = 30
SPACING_MIN = 0                 # % extra entre letras
SPACING_MAX = 50
SPACING_STEP = 5
SUPPORTED_EXTENSIONS = (".txt", ".md", ".docx")
GUIDE_FRACTION = 1 / 3          # altura de la línea de lectura, como fracción del alto de la vista
GUIDE_CENTER_FRACTION = 0.5     # ...con «Lectura al centro» (a la altura del objetivo de la cámara con el cristal)
ANGLE_MIN = -40                 # ángulo de visión (grados): + = miras la pantalla desde arriba, − = desde abajo
ANGLE_MAX = 40
VIEW_DISTANCE = 2.5             # distancia supuesta del ojo a la pantalla, en alturas de pantalla

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W_P = f"{{{_W_NS}}}p"
_W_R = f"{{{_W_NS}}}r"
_W_T = f"{{{_W_NS}}}t"
_W_TAB = f"{{{_W_NS}}}tab"
_W_BR = f"{{{_W_NS}}}br"
_W_CR = f"{{{_W_NS}}}cr"

# Markdown: las reglas se aplican en este orden (bloques primero, luego el texto de cada línea).
_FENCE = re.compile(r"^[ \t]*`{3,}.*$", re.MULTILINE)                 # vallas de código ```
_HR = re.compile(r"^[ \t]*(?:-{3,}|\*{3,})[ \t]*$", re.MULTILINE)      # --- o *** solos: línea vacía
_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+", re.MULTILINE)       # # Título
_QUOTE = re.compile(r"^[ \t]{0,3}>[ \t]?", re.MULTILINE)              # > cita
_BULLET = re.compile(r"^([ \t]*)[-*+][ \t]+", re.MULTILINE)           # - * + viñeta → •
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")                          # ![alt](url) → nada
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")                          # [texto](url) → texto
_CODE = re.compile(r"`([^`\n]+)`")                                    # `código` → código
_BOLD_STAR = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")                # **negrita**
_BOLD_UNDER = re.compile(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)")      # __negrita__
_ITALIC_STAR = re.compile(r"(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])")  # *cursiva*
_ITALIC_UNDER = re.compile(r"(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)")      # _cursiva_ (no nombre_archivo)


def clamp(value: float, lo: float, hi: float) -> float:
    """Limita value al intervalo [lo, hi]."""
    return max(lo, min(hi, value))


def speed_to_px_per_s(level: int, line_height_px: float) -> float:
    """Convierte el nivel de velocidad (1-30) en píxeles por segundo.

    Nivel 10 = un tercio de línea por segundo (ritmo de habla normal); nivel 30 = una línea por segundo.
    """
    lvl = clamp(level, SPEED_MIN, SPEED_MAX)
    return lvl * line_height_px / SPEED_MAX


def load_text(path: str | Path) -> str:
    """Lee un archivo .txt, .md o .docx y devuelve su texto listo para mostrar.

    Lanza ValueError si el formato no se admite o el .docx está dañado, y OSError si no se puede leer.
    """
    p = Path(path)
    ext = p.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Formato no admitido: «{p.suffix or 'sin extensión'}». Usa .txt, .md o .docx.")
    if ext == ".docx":
        raw = _read_docx(p)
    else:
        raw = _decode(p.read_bytes())
        if ext == ".md":
            raw = strip_markdown(raw)
    return _normalize(raw)


def strip_markdown(text: str) -> str:
    """Quita la sintaxis de Markdown que no tiene sentido leída en voz alta. El resto del texto no se toca."""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = _FENCE.sub("", t)
    t = _HR.sub("", t)
    t = _HEADING.sub("", t)
    t = _QUOTE.sub("", t)
    t = _BULLET.sub(r"\1• ", t)
    t = _IMAGE.sub("", t)
    t = _LINK.sub(r"\1", t)
    t = _CODE.sub(r"\1", t)
    t = _BOLD_STAR.sub(r"\1", t)
    t = _BOLD_UNDER.sub(r"\1", t)
    t = _ITALIC_STAR.sub(r"\1", t)
    t = _ITALIC_UNDER.sub(r"\1", t)
    return t


def _decode(raw: bytes) -> str:
    """UTF-8 (con o sin BOM); si no es UTF-8 válido, Windows cp1252 (lo que suele salir del Bloc de notas)."""
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        log.info("El texto no es UTF-8; se lee como cp1252")
        return raw.decode("cp1252", errors="replace")


def _read_docx(path: Path) -> str:
    """Extrae el texto de word/document.xml: un párrafo por línea, pestañas y saltos de línea respetados."""
    try:
        with zipfile.ZipFile(path) as zf:
            xml_data = zf.read("word/document.xml")
        root = ET.fromstring(xml_data)
    except (zipfile.BadZipFile, KeyError, ET.ParseError, zlib.error) as exc:
        log.warning("Documento de Word no válido: %s (%s)", path, exc)
        raise ValueError(f"El archivo «{path.name}» está dañado o no es un documento de Word válido.") from exc

    parrafos: list[str] = []
    for parrafo in root.iter(_W_P):
        partes: list[str] = []
        # Solo los w:r (carreras de texto) contienen w:t, w:tab, w:br: así no se cuentan las pestañas
        # de definición (w:pPr/w:tabs/w:tab) como si fueran tabuladores escritos.
        for run in parrafo.iter(_W_R):
            for hijo in run:
                if hijo.tag == _W_T:
                    partes.append(hijo.text or "")
                elif hijo.tag == _W_TAB:
                    partes.append("\t")
                elif hijo.tag in (_W_BR, _W_CR):
                    partes.append("\n")
        parrafos.append("".join(partes))
    return "\n".join(parrafos)


def _normalize(text: str) -> str:
    """Finales de línea «\\n» y sin espacios ni saltos sobrantes al final del texto."""
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip()

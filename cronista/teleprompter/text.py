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

LINE_SEPARATOR = chr(0x2028)    # salto dentro del párrafo: ocupa una posición, igual que el espacio al que sustituye
PHRASE_MIN_WORDS = 3            # tras coma o dos puntos, solo se corta si el trozo tiene ya estas palabras
PHRASE_MAX_CHARS = 60           # un trozo más largo se parte; la vista lo ajusta a lo que cabe en una línea
PHRASE_MIN_SIDE = 3             # al partir un trozo largo, palabras mínimas a cada lado
PHRASE_BREATH_CHARS = 45        # con análisis gramatical, a partir de aquí dos oraciones se separan aunque quepan
PHRASE_CLAUSE_COST = 0.5        # coste máximo de un corte que no hace falta por espacio (entre oraciones)
_WORD = re.compile(r"\S+")
_CLOSERS = "\"'”’»)]*"
_OPENERS = "¿¡\"«“("
_ABBREVIATIONS = {"sr.", "sra.", "srta.", "dr.", "dra.", "ee.", "uu.", "vs.", "etc.", "ej.", "p.", "pág.", "núm."}
_CLAUSE_WORDS = {"pero", "porque", "aunque", "mientras", "cuando", "donde", "sino", "si", "que"}
_COORDINATORS = {"y", "e", "o", "u", "ni"}
_PREPOSITIONS = {"para", "con", "sin", "por", "desde", "hasta", "en", "sobre", "según", "entre", "tras"}
# Palabras que necesitan a la siguiente: una línea nunca debe terminar en ellas.
_NEEDS_NEXT = {"el", "la", "los", "las", "un", "una", "unos", "unas", "lo", "al", "del", "de", "a", "en", "por",
               "para", "con", "sin", "y", "e", "o", "u", "ni", "que", "su", "sus", "mi", "mis", "tu", "tus", "se",
               "me", "te", "le", "les", "no", "muy", "más", "tan", "este", "esta", "ese", "esa", "pero", "porque",
               "es", "son", "era", "eran", "fue", "fueron"}

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


def phrase_breaks(text: str, max_chars: int = PHRASE_MAX_CHARS,
                  costes: dict[int, float | None] | None = None) -> str:
    """El mismo texto con saltos de línea entre frases, para leer en voz alta (pausas naturales para respirar).

    Cambia un espacio por un separador de línea (U+2028): la longitud no varía, así que las banderas y el modo
    Smart, que trabajan con posiciones en el texto, siguen valiendo. Dos pasos: primero se corta por la puntuación
    y luego, los trozos de más de max_chars caracteres (lo que cabe en una línea) se parten por su mejor punto.
    costes viene del análisis gramatical (gramatica.pause_costs); sin él se usan reglas con listas de palabras.
    """
    letras = list(text)
    inicio_linea = 0
    for linea in text.split("\n"):
        palabras = list(_WORD.finditer(linea))
        cortes = _punctuation_cuts([p.group(0) for p in palabras])
        limites = [0, *cortes, len(palabras)]
        for a, b in zip(limites, limites[1:]):
            _split_long(palabras, a, b, max_chars, cortes, costes, inicio_linea)
        for i in cortes:
            hueco = inicio_linea + palabras[i].start() - 1
            if letras[hueco] in " \t":
                letras[hueco] = LINE_SEPARATOR
        inicio_linea += len(linea) + 1
    return "".join(letras)


def _bare(palabra: str) -> str:
    """La palabra en minúsculas y sin signos alrededor."""
    return palabra.strip(_CLOSERS + _OPENERS + ",.;:!?…").lower()


def _punctuation_cuts(palabras: list[str]) -> list[int]:
    """Índices de las palabras que empiezan línea por la puntuación que las precede."""
    cortes: list[int] = []
    ultimo = 0
    for k in range(len(palabras) - 1):
        if _phrase_ends(palabras, k, k - ultimo + 1):
            cortes.append(k + 1)
            ultimo = k + 1
    return cortes


def _phrase_ends(palabras: list[str], k: int, llevadas: int) -> bool:
    """¿Termina una frase tras la palabra k? llevadas cuenta las palabras del trozo actual."""
    limpia = palabras[k].rstrip(_CLOSERS)
    siguiente = palabras[k + 1]
    final = limpia[-1:]
    if final in ("!", "?", ";", "…") or limpia.endswith(".."):
        return llevadas >= 2
    if final == ".":
        if limpia.lower() in _ABBREVIATIONS or not (siguiente[0].isupper() or siguiente[0] in _OPENERS):
            return False
        return llevadas >= 2
    if final == ":":                              # «Rise of Iron: D-Day» (mayúscula a los dos lados) es un título
        return llevadas >= PHRASE_MIN_WORDS and not (limpia[0].isupper() and siguiente[0].isupper())
    if final == ",":
        return _comma_cuts(palabras, k, llevadas)
    return False


def _comma_cuts(palabras: list[str], k: int, llevadas: int) -> bool:
    if llevadas < PHRASE_MIN_WORDS or len(palabras) - 1 - k < 2 or _bare(palabras[k]) in _NEEDS_NEXT:
        return False
    if palabras[k + 1].rstrip(_CLOSERS).endswith(","):          # enumeración: «Ana, Lucas, Mara y Tomás»
        return False
    return _bare(palabras[k + 2]) not in _COORDINATORS          # «experiencia, confianza y hermandad»


def _split_long(palabras: list, a: int, b: int, max_chars: int, cortes: list[int],
                costes: dict[int, float | None] | None = None, base: int = 0) -> None:
    """Parte el trozo [a, b) por el mejor punto cerca del medio. Se parte si no cabe en max_chars o, con análisis
    gramatical, si es largo para decirlo de un tirón y contiene dos oraciones."""
    if b - a < 2 * PHRASE_MIN_SIDE:
        return                                    # (también las líneas vacías: no tienen palabras)
    largo = palabras[b - 1].end() - palabras[a].start()
    cabe = largo <= max_chars
    if cabe and (costes is None or largo <= PHRASE_BREATH_CHARS):
        return
    mejor, mejor_nota = None, 0.0
    for i in range(a + PHRASE_MIN_SIDE, b - PHRASE_MIN_SIDE + 1):
        posicion = base + palabras[i].start()
        if costes is not None and posicion in costes:
            castigo = costes[posicion]
        else:
            castigo = _split_penalty(palabras, i, b)
        if castigo is None or (cabe and castigo > PHRASE_CLAUSE_COST):
            continue
        nota = abs((i - a) - (b - a) / 2) + castigo
        if mejor is None or nota < mejor_nota:
            mejor, mejor_nota = i, nota
    if mejor is None:
        return                                    # sin un buen punto: mejor que la pantalla lo ajuste
    cortes.append(mejor)
    cortes.sort()
    _split_long(palabras, a, mejor, max_chars, cortes, costes, base)
    _split_long(palabras, mejor, b, max_chars, cortes, costes, base)


def _split_penalty(palabras: list, i: int, b: int) -> float | None:
    """Lo malo que es empezar línea en la palabra i (None = no se puede): conjunción, coordinación o preposición."""
    if _bare(palabras[i - 1].group(0)) in _NEEDS_NEXT:
        return None
    palabra = _bare(palabras[i].group(0))
    if palabra in _CLAUSE_WORDS:
        return 0.0
    if palabra in _COORDINATORS:
        if i + 1 < b and palabras[i + 1].group(0)[0].isupper():      # «Lucas y Ana», «Iron 4 y Iron 5»
            return None
        return 0.5
    if palabra in _PREPOSITIONS:
        return 3.0
    return None


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

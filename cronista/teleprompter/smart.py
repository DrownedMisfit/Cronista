"""Modo Smart del teleprompter: el texto avanza siguiendo la voz de quien lee.

Reconocimiento de voz LOCAL con Vosk (sin internet, salvo para bajar el modelo una vez).

Partes:
- Funciones de texto puras (normalize, tokenize, guess_language).
- ScriptFollower: sigue el guion a partir de las palabras reconocidas. Sin Qt.
- Modelos de Vosk: rutas y descarga (download_model).
- VoiceListener: escucha el micrófono en un hilo de trabajo y emite señales Qt.

Solo la parte de VoiceListener usa Qt, y solo PySide6.QtCore.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import threading
import unicodedata
import urllib.request
import zipfile
from array import array
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Signal

from cronista.core.paths import data_dir

log = logging.getLogger(__name__)

RATE = 16000            # frecuencia del reconocedor (los modelos small de Vosk van a 16 kHz)
BLOCK = 2000                    # 125 ms por bloque: el texto reacciona rápido a la voz
WINDOW_AHEAD = 40       # palabras del guion que se buscan hacia adelante
WINDOW_BEHIND = 12      # palabras del guion que se buscan hacia atrás (solo para encajar lo ya dicho)
HEARD_TAIL = 10         # últimas palabras oídas que se usan para encajar: cuantas más, menos se confunde
MATCH_RATIO = 0.75      # similitud mínima entre dos palabras para considerarlas iguales
GLOBAL_AFTER = 6        # tras N intentos sin encajar cerca, se busca en TODO el guion (empezó en otra parte)
GLOBAL_MIN_CHAIN = 5    # para saltar a otra parte del guion hacen falta al menos 5 palabras seguidas
# Cuanto más lejos de donde va el lector empieza lo oído, más palabras seguidas hacen falta. Antes bastaban 2
# palabras parecidas en cualquier punto de las 40 siguientes, y se saltaba a frases parecidas de más adelante.
NEAR_GAP = 3            # hasta aquí es seguir leyendo (o haberse comido alguna palabra)
MID_GAP = 12            # hasta aquí, haberse saltado un trozo: 3 palabras seguidas
MID_MIN_CHAIN = 3

MODELS = {
    "es": ("vosk-model-small-es-0.42", "https://alphacephei.com/vosk/models/vosk-model-small-es-0.42.zip"),
    "en": ("vosk-model-small-en-us-0.15", "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"),
}

# Archivos que debe tener un modelo descomprimido para considerarlo completo.
_MODEL_REQUIRED = ("am/final.mdl", "graph/Gr.fst", "graph/HCLr.fst", "conf/model.conf")

_WORD_RE = re.compile(r"(?:[^\W_]|[̀-ͯ])+")

_STOP_ES = {"de", "la", "que", "el", "y", "en", "los", "las", "un", "una", "es", "por",
            "con", "para", "del", "se", "no", "al", "lo", "su", "como", "pero", "mas"}
_STOP_EN = {"the", "and", "to", "of", "is", "it", "that", "for", "with", "on", "are",
            "this", "you", "be", "in", "was", "at"}


# ---------------------------------------------------------------- texto

def normalize(word: str) -> str:
    """Minúsculas, sin tildes ni diéresis (ñ→n) y sin signos de puntuación."""
    decomposed = unicodedata.normalize("NFD", word.lower())
    out = []
    for ch in decomposed:
        cat = unicodedata.category(ch)
        if cat == "Mn" or cat[0] in "PSZC":   # marcas, puntuación, símbolos, espacios, controles
            continue
        out.append(ch)
    return "".join(out)


def tokenize(text: str) -> list[tuple[str, int, int]]:
    """Palabras del guion como (normalizada, inicio, fin) en caracteres del texto original."""
    tokens = []
    for m in _WORD_RE.finditer(text):
        norm = normalize(m.group(0))
        if norm:
            tokens.append((norm, m.start(), m.end()))
    return tokens


FILLERS = {"es": ["eh", "este", "pues", "bueno", "o", "sea", "mm"], "en": ["uh", "um", "so", "well"]}


def script_vocabulary(text: str, lang: str = "es") -> list[str]:
    """Las palabras del guion (y unas muletillas) para limitar el reconocedor a ellas: así entiende mucho mejor lo
    que se lee, también con la voz cansada. «[unk]» recoge lo demás (improvisar, toser) sin inventar palabras."""
    vistas: dict[str, None] = {}
    for m in _WORD_RE.finditer(text.lower()):
        vistas.setdefault(m.group(0), None)
    for palabra in FILLERS.get(lang, []):
        vistas.setdefault(palabra, None)
    return [*vistas, "[unk]"]


# ---------------------------------------------------------------- palabras que el modelo no conoce

_UNIDADES = ("cero uno dos tres cuatro cinco seis siete ocho nueve diez once doce trece catorce quince dieciséis "
             "diecisiete dieciocho diecinueve veinte veintiuno veintidós veintitrés veinticuatro veinticinco "
             "veintiséis veintisiete veintiocho veintinueve").split()
_DECENAS = "treinta cuarenta cincuenta sesenta setenta ochenta noventa".split()
_CENTENAS = ("ciento doscientos trescientos cuatrocientos quinientos seiscientos setecientos ochocientos "
             "novecientos").split()


def number_words(n: int) -> list[str]:
    """Un número (0 a 999 999) dicho en español, en palabras: 2026 → dos mil veintiséis."""
    if n < 30:
        return [_UNIDADES[n]]
    if n < 100:
        d, u = divmod(n, 10)
        return [_DECENAS[d - 3]] + (["y", _UNIDADES[u]] if u else [])
    if n < 1000:
        c, r = divmod(n, 100)
        return (["cien"] if n == 100 else [_CENTENAS[c - 1]]) + (number_words(r) if r else [])
    m, r = divmod(n, 1000)
    return ([] if m == 1 else number_words(m)) + ["mil"] + (number_words(r) if r else [])


def _sin_tildes(w: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFD", w) if unicodedata.category(ch) != "Mn")


def spoken_pieces(word: str, knows: Callable[[str], bool], lang: str = "es") -> list[str] | None:
    """Palabras que el modelo SÍ conoce y que suenan como `word` (que no conoce), o None si no hay.

    El reconocedor limitado al guion no devuelve «[unk]» ante una palabra que no conoce: la cambia por otras del
    guion («las cinemáticas» → «las tiene eh») y el seguidor perdía el hilo justo ahí. Con estas piezas la oye:
    - cifras en palabras: «6» → seis; «mp3» → mp tres; «2026» → dos mil veintiséis;
    - la misma palabra casi igual: «cinemáticas» → cinemática; sin tilde;
    - partida en trozos conocidos: «injugable» → in jugable."""
    w = word.lower()
    if any(ch.isdigit() for ch in w):
        partes: list[str] = []
        for grupo in re.findall(r"\d+|[^\W\d_]+", w):
            if grupo.isdigit():
                if lang != "es" or len(grupo) > 6:
                    return None
                partes += number_words(int(grupo))
            else:
                partes.append(grupo)
        return partes if partes and all(knows(x) for x in partes) else None
    parecidas = [_sin_tildes(w), w + "s"]
    if w.endswith("es"):
        parecidas.append(w[:-2])
    if w.endswith("s"):
        parecidas.append(w[:-1])
    for v in parecidas:
        if len(v) >= 3 and v != w and knows(v):
            return [v]
    if len(w) < 5:
        return None
    # En dos trozos, cuanto más igualados mejor (el más corto, lo más largo posible); si no, en tres.
    mejor: tuple[int, list[str]] | None = None
    for i in range(2, len(w) - 1):
        a, b = w[:i], w[i:]
        corto = min(len(a), len(b))
        if corto >= 2 and (mejor is None or corto > mejor[0]) and knows(a) and knows(b):
            mejor = (corto, [a, b])
    if mejor is not None:
        return mejor[1]
    for i in range(2, len(w) - 3):
        for j in range(i + 2, len(w) - 1):
            trozos = [w[:i], w[i:j], w[j:]]
            if all(knows(x) for x in trozos):
                return trozos
    return None


_LETRAS_ES = {"a": "a", "b": "be", "c": "ce", "d": "de", "e": "e", "f": "efe", "g": "ge", "h": "hache", "i": "i",
              "j": "jota", "k": "ka", "l": "ele", "m": "eme", "n": "ene", "ñ": "eñe", "o": "o", "p": "pe", "q": "cu",
              "r": "erre", "s": "ese", "t": "te", "u": "u", "v": "uve", "w": "uve doble", "x": "equis", "y": "ye",
              "z": "zeta"}
_SIGLA_RE = re.compile(r"\b(?=[A-ZÑ0-9]*[A-ZÑ][A-ZÑ0-9]*[A-ZÑ0-9]\b)(?=(?:[^\W\d_]*[A-ZÑ]){2}|[A-ZÑ]+\d)[A-ZÑ0-9]{2,6}\b")


def acronyms(text: str) -> set[str]:
    """Siglas escritas en mayúsculas (ONU, MP3, NASA; hasta 4 letras), en minúsculas: se dicen letra a letra."""
    siglas = {m.group(0).lower() for m in _SIGLA_RE.finditer(text)}
    return {s for s in siglas if sum(ch.isalpha() for ch in s) <= 4}       # «NUNCA» en mayúsculas no es una sigla


def spelled(word: str, knows: Callable[[str], bool], lang: str = "es") -> list[str] | None:
    """La sigla dicha letra a letra (y las cifras en palabras): onu → o ene u; mp3 → eme pe tres."""
    if lang != "es":
        return None
    partes: list[str] = []
    for grupo in re.findall(r"\d+|[^\W\d_]+", word.lower()):
        if grupo.isdigit():
            if len(grupo) > 6:
                return None
            partes += number_words(int(grupo))
        else:
            for letra in grupo:
                if letra not in _LETRAS_ES:
                    return None
                partes += _LETRAS_ES[letra].split()
    return partes if partes and all(knows(x) for x in partes) else None


def spoken_forms(words, knows: Callable[[str], bool], lang: str = "es",
                 acronyms_: set[str] | None = None) -> dict[str, list[list[str]]]:
    """{palabra: [formas de decirla con palabras que el modelo SÍ conoce]} para las que hace falta y tienen arreglo:
    las que no conoce (spoken_pieces) y las siglas, que se deletrean («ONU» → o ene u) aunque las conozca."""
    formas: dict[str, list[list[str]]] = {}
    for w in words:
        if w == "[unk]":
            continue
        variantes = []
        if not knows(w):
            piezas = spoken_pieces(w, knows, lang)
            if piezas:
                variantes.append(piezas)
        if acronyms_ and w in acronyms_:
            letras = spelled(w, knows, lang)
            if letras and letras not in variantes:
                variantes.append(letras)
        if variantes:
            formas[w] = variantes
    return formas


class SpokenForms:
    """Vuelve a juntar en la palabra del guion las piezas que el reconocedor oyó por separado («in jugable» →
    injugable, «seis» → 6). Una pieza suelta que también está escrita en el guion se deja como está."""

    def __init__(self, forms: dict[str, list[list[str]]] | None = None,
                 script_words: set[str] | None = None) -> None:
        self._map: dict[tuple[str, ...], str] = {}
        for palabra, variantes in (forms or {}).items():
            for piezas in variantes:
                clave = tuple(normalize(x) for x in piezas)
                if not all(clave) or (len(clave) == 1 and clave[0] in (script_words or set())):
                    continue
                self._map.setdefault(clave, normalize(palabra))
        self._max = max((len(k) for k in self._map), default=0)

    def __bool__(self) -> bool:
        return bool(self._map)

    def rewrite(self, words: list[str]) -> list[str]:
        """Palabras ya normalizadas → las mismas con las piezas juntadas."""
        if not self._map:
            return words
        out, i = [], 0
        while i < len(words):
            for largo in range(min(self._max, len(words) - i), 0, -1):
                destino = self._map.get(tuple(words[i:i + largo]))
                if destino is not None:
                    out.append(destino)
                    i += largo
                    break
            else:
                out.append(words[i])
                i += 1
        return out


def guess_language(text: str) -> str:
    """'es' o 'en' según las palabras vacías más frecuentes. Por defecto 'es'."""
    words = [w for w, _, _ in tokenize(text)]
    es = sum(1 for w in words if w in _STOP_ES)
    en = sum(1 for w in words if w in _STOP_EN)
    return "en" if en > es else "es"


# ---------------------------------------------------------------- seguimiento del guion

def _same_word(a: str, b: str) -> bool:
    if a == b:
        return True
    if abs(len(a) - len(b)) > 3:
        return False
    return SequenceMatcher(None, a, b).ratio() >= MATCH_RATIO


def _qualifies(chain_words: list[str]) -> bool:
    """Una cadena vale si son 2 o más palabras, con alguna de al menos 4 letras,
    o si es una sola palabra de 7 letras o más."""
    if len(chain_words) == 1:
        return len(chain_words[0]) >= 7
    return len(chain_words) >= 2 and any(len(w) >= 4 for w in chain_words)


class ScriptFollower:
    """Sigue la lectura del guion a partir de las palabras que reconoce la voz.

    Estado: `_cur` es el índice de la palabra en la que va el lector (-1 si aún no
    ha leído nada) y `_pos` el carácter donde termina. Nunca retrocede.
    """

    def __init__(self, text: str):
        self._text = text
        self._words = tokenize(text)
        self._cur = -1
        self._pos = 0
        self._misses = 0         # intentos seguidos sin encajar cerca
        self._next = 0           # índice de la siguiente palabra que se espera oír
        self._history: list[str] = []   # palabras ya cerradas por el reconocedor que aún no sirvieron para avanzar
        self._ya_dicho = 0       # en el último intento, la cadena más larga que encajaba con lo ya leído
        self.jumped = False      # True si el último encaje fue un salto lejano (puede ser hacia atrás)
        self._formas = SpokenForms()

    def set_spoken_forms(self, forms: dict[str, list[list[str]]] | None) -> None:
        """Piezas con que se oyen las palabras que el modelo no conoce (spoken_forms): se juntan al oírlas."""
        self._formas = SpokenForms(forms, {w for w, _, _ in self._words})

    def reset(self, char_pos: int) -> None:
        """Coloca el lector en la palabra que contiene o sigue a `char_pos`
        (cuando el usuario mueve el texto a mano)."""
        n = len(self._words)
        char_pos = max(0, min(char_pos, len(self._text)))
        self._pos = char_pos
        if n == 0:
            self._cur = -1
            return
        idx = n - 1
        for i, (_, _, end) in enumerate(self._words):
            if end > char_pos:
                idx = i
                break
        self._cur = idx
        self._next = idx
        self._misses = 0
        self._history = []                  # lo oído antes era de otro sitio del guion

    def position(self) -> int:
        """Carácter actual del lector (fin de la última palabra encajada)."""
        return self._pos

    def progress_words(self) -> tuple[int, int]:
        """(índice de la palabra actual, total de palabras)."""
        return max(self._cur, 0), len(self._words)

    def feed(self, heard: list[str], final: bool = False) -> int | None:
        """Recibe las últimas palabras reconocidas. Si encajan hacia adelante,
        avanza y devuelve el carácter del final de la última palabra encajada.
        Si no, devuelve None y no mueve nada.

        final=True: el reconocedor cerró ese trozo; sus palabras se recuerdan y se usan delante de los trozos
        siguientes. Con la voz lenta o cansada el reconocedor corta en trozos de una o dos palabras, y mirándolos
        sueltos nunca había bastantes palabras seguidas para estar seguro (se atascaba varios segundos)."""
        nuevas = [n for n in (normalize(w) for w in heard) if n]
        heard_norm = self._formas.rewrite(self._history + nuevas)[-HEARD_TAIL:]
        n = len(self._words)
        if not nuevas or n == 0:
            return None
        encontrado = self._feed_norm(heard_norm)
        # La memoria solo guarda lo que aún no ha servido para avanzar: si ya se usó, al enlazarla con palabras
        # nuevas encajaba en una frase parecida de más adelante («el combate siente» + «lento»).
        if encontrado is not None or (final and self._ya_dicho >= max(1, len(heard_norm) - 1)):
            self._history = []
        elif final:
            self._history = heard_norm
        return encontrado

    def _feed_norm(self, heard_norm: list[str]) -> int | None:
        n = len(self._words)

        cur = self._cur
        lo = max(0, cur - WINDOW_BEHIND)
        hi = min(n - 1, max(cur, 0) + WINDOW_AHEAD)
        self.jumped = False
        found = self._near(heard_norm, lo, hi)
        if found is not None:
            self._misses = 0
            return found
        self._misses += 1
        if self._misses >= GLOBAL_AFTER and len(heard_norm) >= GLOBAL_MIN_CHAIN:
            return self._anywhere(heard_norm)
        return None

    def _chains(self, heard_norm: list[str], lo: int, hi: int) -> dict:
        """Cadenas de coincidencias (palabra oída → palabra del guion) en el tramo [lo, hi] del guion."""
        by_heard: list[list[int]] = []
        for h in heard_norm:
            by_heard.append([j for j in range(lo, hi + 1) if _same_word(h, self._words[j][0])])

        # Programación dinámica: cadenas de pares en orden. Entre dos pares seguidos
        # puede haber como mucho una palabra oída sin encaje (muletillas) y entre
        # dos palabras del guion de 1 a 3 posiciones (cifras o palabras omitidas).
        best: dict[tuple[int, int], tuple[int, tuple[int, int] | None]] = {}
        for i, js in enumerate(by_heard):
            for j in js:
                length, pred = 1, None
                for pi in (i - 1, i - 2):
                    if pi < 0:
                        continue
                    for pj in by_heard[pi]:
                        if 1 <= j - pj <= 3 and (pi, pj) in best:
                            cand = best[(pi, pj)][0] + 1
                            if cand > length:
                                length, pred = cand, (pi, pj)
                best[(i, j)] = (length, pred)
        return best

    @staticmethod
    def _chain_words(best: dict, key) -> list[int]:
        chain = []
        while key is not None:
            chain.append(key[1])
            key = best[key][1]
        return chain

    def _near(self, heard_norm: list[str], lo: int, hi: int) -> int | None:
        """Busca lo oído por tramos: primero junto a donde va; más lejos solo si no encaja cerca y con más
        palabras seguidas. Así una frase parecida de más adelante no le gana a la que se está leyendo."""
        cur = self._cur
        best = self._chains(heard_norm, lo, hi)
        ya_dicho = self._ya_dicho = 0                 # la cadena más larga que coincide con lo ya leído
        tramos: tuple[list, list, list] = ([], [], [])
        for (i, j), (length, _) in best.items():
            if j < cur or self._words[j][2] <= self._pos:
                ya_dicho = self._ya_dicho = max(ya_dicho, length)
                continue
            cadena = self._chain_words(best, (i, j))      # de la última palabra a la primera
            hueco = cadena[-1] - self._next               # palabras del guion saltadas antes de lo oído
            tramo = 0 if hueco <= NEAR_GAP else 1 if hueco <= MID_GAP else 2
            if self._vale_en_tramo(tramo, cadena):
                tramos[tramo].append((length, j, hueco))

        elegido = None
        # Seguir justo donde va siempre vale; saltarse palabras, solo si lo oído no es lo que se acaba de leer (el
        # reconocedor repite trozos: «combate siente» dos veces saltaba a la frase casi igual que venía después).
        cerca = [c for c in tramos[0] if c[2] <= 0 or c[0] > ya_dicho]
        if cerca:
            elegido = max(cerca, key=lambda c: (c[0], -c[1]))             # la más larga; si empatan, la más cercana
        elif not tramos[0]:
            for candidatos in tramos[1:]:
                candidatos = [c for c in candidatos if c[0] > ya_dicho]   # lo oído encaja igual aquí: no se salta
                if not candidatos:
                    continue
                larga = max(c[0] for c in candidatos)
                lugares = {c[1] for c in candidatos if c[0] == larga}
                if len({j // 8 for j in lugares}) > 1:                    # la misma frase en dos sitios: no se salta
                    return None
                elegido = (larga, min(lugares))
                break
        if elegido is None:
            return None
        self._cur = elegido[1]
        self._next = elegido[1] + 1
        self._pos = self._words[elegido[1]][2]
        return self._pos

    def _vale_en_tramo(self, tramo: int, cadena: list[int]) -> bool:
        """¿La cadena (índices del guion) basta para mover el texto hasta ese tramo?"""
        palabras = [self._words[k][0] for k in cadena]
        if tramo == 0:
            return _qualifies(palabras)
        minimo, largas = (MID_MIN_CHAIN, 2) if tramo == 1 else (GLOBAL_MIN_CHAIN, 3)
        seguidas = cadena[0] - cadena[-1] + 1 <= len(cadena) + 1      # como mucho una palabra del guion sin oír
        return len(cadena) >= minimo and seguidas and sum(1 for w in palabras if len(w) >= 4) >= largas

    def _anywhere(self, heard_norm: list[str]) -> int | None:
        """Lo dicho no está cerca: se busca en todo el guion (empezó a leer en otra parte, o volvió atrás).
        Hace falta una cadena de al menos 5 palabras con 3 de 4 letras o más, y que sea la mejor sin empate."""
        best = self._chains(heard_norm, 0, len(self._words) - 1)
        cands = []
        cur = self._cur
        for key, (length, _) in best.items():
            if length < GLOBAL_MIN_CHAIN:
                continue
            if cur - 12 <= key[1] <= cur:          # un poco más atrás de donde va: no es «otra parte», no se salta
                continue
            words = [self._words[k][0] for k in self._chain_words(best, key)]
            if sum(1 for w in words if len(w) >= 4) >= 3:
                cands.append((length, key[1]))
        if not cands:
            return None
        cands.sort(reverse=True)
        top = cands[0][0]
        lugares = {j for length, j in cands if length == top}
        if len({j // 8 for j in lugares}) > 1:      # la misma frase en dos sitios: mejor no saltar
            return None
        j = max(lugares)
        self._cur, self._pos, self._misses, self._next = j, self._words[j][2], 0, j + 1
        self.jumped = True
        return self._pos


# ---------------------------------------------------------------- modelos

def models_dir() -> Path:
    """%APPDATA%\\Cronista\\modelos: modelos de voz de Vosk."""
    d = data_dir() / "modelos"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _model_complete(folder: Path) -> bool:
    return folder.is_dir() and all((folder / rel).is_file() for rel in _MODEL_REQUIRED)


def model_path(lang: str) -> Path | None:
    """Carpeta del modelo de ese idioma si está descargado y completo; si no, None."""
    if lang not in MODELS:
        return None
    folder = models_dir() / MODELS[lang][0]
    return folder if _model_complete(folder) else None


class DescargaCancelada(Exception):
    """La descarga del modelo se canceló."""


def download_model(lang: str,
                   progress: Callable[[int, int], None] | None = None,
                   cancelled: Callable[[], bool] | None = None) -> Path:
    """Descarga y descomprime el modelo de voz de ese idioma.

    progress(bytes_hechos, bytes_totales) se llama al avanzar (total 0 si se desconoce).
    cancelled() se consulta en cada bloque; si devuelve True se aborta y se limpia.
    La descompresión va a una carpeta temporal y luego se renombra (atómica).
    """
    if lang not in MODELS:
        raise ValueError(f"No hay modelo de voz para el idioma «{lang}».")
    name, url = MODELS[lang]
    existing = model_path(lang)
    if existing is not None:
        return existing

    target = models_dir()
    zip_part = target / f"{name}.zip.part"
    zip_file = target / f"{name}.zip"
    tmp_dir = Path(tempfile.mkdtemp(prefix=".tmp_", dir=str(target)))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Cronista"})
        with urllib.request.urlopen(req, timeout=30) as resp, open(zip_part, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            if progress:
                progress(0, total)
            while True:
                if cancelled and cancelled():
                    raise DescargaCancelada("Descarga cancelada.")
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        os.replace(zip_part, zip_file)

        if cancelled and cancelled():
            raise DescargaCancelada("Descarga cancelada.")
        with zipfile.ZipFile(zip_file) as zf:
            zf.extractall(tmp_dir)
        extracted = tmp_dir / name
        if not extracted.is_dir():
            raise RuntimeError(f"El archivo descargado no contiene la carpeta «{name}».")
        final = target / name
        if final.exists():
            shutil.rmtree(final, ignore_errors=True)
        os.replace(extracted, final)
        if not _model_complete(final):
            raise RuntimeError(f"El modelo «{name}» descargado está incompleto.")
        return final
    except DescargaCancelada:
        raise
    except Exception as e:
        raise RuntimeError(f"No se pudo descargar el modelo de voz: {e}") from e
    finally:
        for p in (zip_part, zip_file):
            try:
                p.unlink()
            except OSError:
                pass
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------- micrófono

def _peak(raw: bytes) -> float:
    samples = array("h")
    samples.frombytes(raw)
    if not samples:
        return 0.0
    peak = max(max(samples), -min(samples))
    return min(1.0, peak / 32768.0)


def _words(result: dict, key: str) -> list[str]:
    return str(result.get(key, "")).split()


_MODEL_CACHE: dict = {}


def _model_lacks(modelo, palabra: str) -> bool:
    """¿El modelo de Vosk no conoce esa palabra? (Si no se puede saber, se supone que sí la conoce.)"""
    buscar = getattr(modelo, "vosk_model_find_word", None) or getattr(modelo, "find_word", None)
    try:
        return buscar is not None and buscar(palabra) < 0
    except Exception:  # noqa: BLE001
        return False


def input_devices() -> list[str]:
    """Nombres de los micrófonos (los de MME, la lista clásica de Windows, sin repetidos)."""
    try:
        import sounddevice as sd
        apis = sd.query_hostapis()
        nombres = []
        for d in sd.query_devices():
            if d["max_input_channels"] > 0 and apis[d["hostapi"]]["name"] == "MME" \
                    and "Asignador" not in d["name"] and d["name"] not in nombres:
                nombres.append(d["name"])
        return nombres
    except Exception:  # noqa: BLE001 - sin sounddevice: solo «predeterminado»
        return []


def device_index(nombre: str | int | None) -> int | None:
    """El número del micrófono con ese nombre en MME. Por el nombre solo, sounddevice falla si Windows lo enseña en
    varios sistemas de sonido («Micrófono (D1)» sale en MME, DirectSound, WASAPI y WDM-KS: «Multiple input devices
    found»). None = el predeterminado; si no está (se desconectó), también el predeterminado."""
    if nombre is None or nombre == "" or isinstance(nombre, int):
        return nombre or None
    try:
        import sounddevice as sd
        apis = sd.query_hostapis()
        candidatos = [i for i, d in enumerate(sd.query_devices())
                      if d["max_input_channels"] > 0 and d["name"] == nombre]
        for i in candidatos:
            if apis[sd.query_devices(i)["hostapi"]]["name"] == "MME":
                return i
        return candidatos[0] if candidatos else None
    except Exception:  # noqa: BLE001
        return None


class VoiceListener(QObject):
    """Escucha el micrófono y emite las palabras reconocidas.

    Señales:
      heard(list)  palabras del parcial actual (solo cuando cambian)
      heard_final(list) palabras de un trozo que el reconocedor dio por cerrado
      analysed()   ya están unknown_words y spoken_forms
      level(float) volumen de entrada 0–1
      error(str)   mensaje en español
      started() / stopped()

    El reconocimiento corre en un hilo de trabajo; Qt entrega las señales al
    hilo principal. Uso: listener.start("es") ... listener.stop().
    """

    heard = Signal(list)
    heard_final = Signal(list)
    analysed = Signal()          # ya se sabe qué palabras del guion no conoce el modelo (unknown_words, spoken_forms)
    level = Signal(float)
    error = Signal(str)
    started = Signal()
    stopped = Signal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None
        self.unknown_words: set[str] = set()   # palabras del guion que el modelo no conoce (se rellena al empezar)
        self.spoken_forms: dict[str, list[list[str]]] = {}   # cómo oír esas (y las siglas) con palabras que conoce

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, lang: str, device: str | int | None = None, vocabulary: list[str] | None = None,
              acronyms_: set[str] | None = None) -> None:
        """device: nombre (o número) del micrófono; None = el predeterminado de Windows.
        vocabulary: palabras a las que limitar el reconocedor (script_vocabulary del guion).
        acronyms_: siglas del guion (acronyms): también se esperan deletreadas."""
        if self.is_running():
            return
        self._device = device_index(device)      # (por número: el nombre se repite en cada sistema de sonido)
        self._vocabulary = vocabulary
        self._lang = lang
        self._acronyms = set(acronyms_ or ())
        path = model_path(lang)
        if path is None:
            self.error.emit(f"No está descargado el modelo de voz para «{lang}». "
                            "Descárgalo antes de usar el modo Smart.")
            return
        stop_event = threading.Event()
        self._stop_event = stop_event
        self._thread = threading.Thread(target=self._run, args=(path, stop_event),
                                        name="cronista-voz", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout)

    # -- hilo de trabajo ---------------------------------------------------

    def _run(self, path: Path, stop_event: threading.Event) -> None:
        try:
            self._listen(path, stop_event)
        finally:
            self.stopped.emit()

    def _listen(self, path: Path, stop_event: threading.Event) -> None:
        try:
            import sounddevice as sd
            import vosk
        except ImportError as e:
            self.error.emit(f"Falta una librería de voz ({e.name}). No se puede escuchar.")
            return

        try:
            vosk.SetLogLevel(-1)
            modelo = _MODEL_CACHE.get(str(path))
            if modelo is None:                     # (cargarlo tarda ~1 s: se guarda para la próxima lectura)
                modelo = _MODEL_CACHE[str(path)] = vosk.Model(str(path))
            recognizer = None
            vocabulario = getattr(self, "_vocabulary", None)
            # Palabras del guion que el modelo no conoce: no podrá oírlas nunca, así que no se exigen al revisar.
            self.unknown_words = {w for w in (vocabulario or []) if w != "[unk]" and _model_lacks(modelo, w)}
            # Y de esas, las que se pueden oír como otras que sí conoce («in jugable», «seis»): se añaden esas piezas.
            siglas = getattr(self, "_acronyms", set()) or set()
            self.spoken_forms = spoken_forms(sorted(self.unknown_words | siglas), lambda w: not _model_lacks(modelo, w),
                                             getattr(self, "_lang", "es"), siglas)
            if vocabulario and self.spoken_forms:
                extra = [x for variantes in self.spoken_forms.values() for piezas in variantes for x in piezas
                         if x not in vocabulario]
                vocabulario = [*vocabulario[:-1], *dict.fromkeys(extra), vocabulario[-1]]
            self.analysed.emit()
            if vocabulario:
                try:                               # limitado al guion: entiende mucho mejor lo que se lee
                    recognizer = vosk.KaldiRecognizer(modelo, RATE, json.dumps(vocabulario, ensure_ascii=False))
                except Exception:  # noqa: BLE001 - un modelo sin esa opción: se escucha sin limitar
                    log.warning("El modelo de voz no admite limitar el vocabulario; se escucha sin limitar")
            if recognizer is None:
                recognizer = vosk.KaldiRecognizer(modelo, RATE)
        except Exception as e:
            self.error.emit(f"No se pudo cargar el modelo de voz: {e}")
            return

        try:
            sd.query_devices(kind="input")
        except Exception:
            self.error.emit("No hay micrófono disponible.")
            return

        try:
            stream = sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16",
                                       blocksize=BLOCK, device=getattr(self, "_device", None))
        except Exception as e:
            self.error.emit(f"No se pudo abrir el micrófono: {e}")
            return

        last: list[str] = []
        try:
            with stream:
                self.started.emit()
                while not stop_event.is_set():
                    data, _overflow = stream.read(BLOCK)
                    raw = bytes(data)
                    self.level.emit(_peak(raw))
                    if recognizer.AcceptWaveform(raw):
                        words = _words(json.loads(recognizer.Result()), "text")
                        last = []
                        if words:
                            self.heard_final.emit(words)
                    else:
                        words = _words(json.loads(recognizer.PartialResult()), "partial")
                        if words and words != last:
                            last = words
                            self.heard.emit(words)
                final = _words(json.loads(recognizer.FinalResult()), "text")
                if final:
                    self.heard_final.emit(final)
        except Exception as e:
            self.error.emit(f"Se perdió el micrófono: {e}")

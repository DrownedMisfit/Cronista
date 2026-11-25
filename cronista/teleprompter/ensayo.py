"""Ensayo y grabación: frases del guion, posibles errores de lectura y registro de la sesión. Sin Qt.

- sentence_spans: el guion dividido en frases (posiciones en el texto).
- ReadingChecker: compara lo que se oyó con lo escrito y dice qué frases parecen tener palabras de menos. Es una
  sospecha, no un juicio: el reconocedor también se equivoca (palabras que no conoce, nombres en inglés, voz
  cansada). Por eso no exige esas palabras, mide lo bien que está oyendo en la sesión y, si oye mal, exige mucho más.
- SessionLog: un archivo JSON por sesión con cada toma mala y cada posible error, para revisarlo después (y, más
  adelante, para que Reclaimer quite las tomas malas).
"""
from __future__ import annotations

import json
import logging
import os
import re
import statistics
import time
import zlib
from difflib import SequenceMatcher
from pathlib import Path

from cronista.teleprompter.smart import FILLERS, SpokenForms, normalize, tokenize

log = logging.getLogger(__name__)

REVIEW_LEVELS = ("apagado", "silencioso", "discreto", "atento")
NOT_READ = "(frase sin leer)"
_SENTENCE_END = re.compile(r"[.!?…]+[\"'”’»)\]]*(?=\s|$)|\n+")

# Por sensibilidad: palabras con contenido (4 letras o más) seguidas que tienen que faltar, y cuánto peor que lo
# normal en la sesión tiene que haberse oído la frase para sospechar.
_SENSITIVITY = {"baja": (3, 0.5), "media": (2, 0.35), "alta": (2, 0.2)}
_WARMUP = 3                     # frases que hacen falta para conocer cómo oye en esta sesión (antes no se compara)
_POOR_HEARING = 0.6             # si de normal oye menos que esto de cada frase, el reconocedor va mal: más exigencia
_VERY_POOR_HEARING = 0.4        # y por debajo de esto solo se avisa de frases enteras sin leer


def sentence_spans(text: str) -> list[tuple[int, int]]:
    """(inicio, fin) de cada frase del texto, sin espacios alrededor. Una frase termina en . ! ? … o salto de línea."""
    spans: list[tuple[int, int]] = []
    inicio = 0
    for m in _SENTENCE_END.finditer(text):
        _add_span(text, inicio, m.end() if m.group(0)[0] != "\n" else m.start(), spans)
        inicio = m.end()
    _add_span(text, inicio, len(text), spans)
    return spans


def _add_span(text: str, a: int, b: int, spans: list[tuple[int, int]]) -> None:
    trozo = text[a:b]
    if trozo.strip():
        a += len(trozo) - len(trozo.lstrip())
        b -= len(trozo) - len(trozo.rstrip())
        spans.append((a, b))


def sentence_at(spans: list[tuple[int, int]], pos: int) -> int:
    """Índice de la frase que contiene pos (o la siguiente si pos cae entre dos); -1 si no hay frases."""
    for i, (a, b) in enumerate(spans):
        if pos < b:
            return i
    return len(spans) - 1


def _hard_to_hear(original: str) -> bool:
    """Nombres propios (mayúscula), cifras y palabras con letras poco españolas: el reconocedor los entiende mal."""
    limpia = original.strip("¿¡\"'«“(")
    if not limpia:
        return True
    return limpia[0].isupper() or any(c.isdigit() for c in limpia) or bool(re.search(r"[wk]|sh|th|ee|oo", limpia.lower()))


class ReadingChecker:
    """Compara lo oído con el guion, frase a frase, y recuerda lo bien que oye en esta sesión."""

    def __init__(self, text: str, sensitivity: str = "media", lang: str = "es",
                 unknown_words: set[str] | None = None, spoken_forms: dict[str, list[list[str]]] | None = None) -> None:
        self.spans = sentence_spans(text)
        # Las palabras que el modelo no conoce pero oye en piezas («in jugable»): se juntan antes de comparar.
        self._formas = SpokenForms(spoken_forms, {n for n, _, _ in tokenize(text)})
        self._consecutive, self._drop = _SENSITIVITY.get(sensitivity, _SENSITIVITY["media"])
        self._fillers = set(FILLERS.get(lang, []))
        desconocidas = {normalize(w) for w in (unknown_words or set())}
        # Por frase: (palabra normalizada, original, ¿hay que oírla?)
        self._words: list[list[tuple[str, str, bool]]] = []
        for a, b in self.spans:
            frase = []
            for norm, x, y in tokenize(text[a:b]):
                original = text[a + x:a + y]
                exigida = norm not in self._fillers and norm not in desconocidas and not _hard_to_hear(original)
                frase.append((norm, original, exigida))
            self._words.append(frase)
        self._coverage: list[float] = []

    def evaluate(self, sentences: list[int], heard: list[str], pending: int = 0) -> tuple[dict[int, list[str]], int]:
        """Juzga esas frases (seguidas) con lo oído desde que empezó la primera. Las últimas `pending` solo
        ayudan a repartir lo oído (lo que encaja con ellas no se atribuye a las anteriores) y no se juzgan todavía.

        Devuelve ({frase: palabras que parecen faltar} solo de las sospechosas, cuántas palabras de lo oído se
        usaron en las frases juzgadas: lo que sigue pertenece a las pendientes o a la siguiente)."""
        escritas = [(k, w) for k in sentences for w in self._words[k]]
        oidas = self._formas.rewrite([n for n in (normalize(h) for h in heard) if n])
        juzgadas = sentences[:len(sentences) - pending] if pending else sentences
        dichas = [False] * len(escritas)
        usadas = 0
        if escritas and oidas:
            # Se compara del final hacia atrás: lo oído pertenece antes a la frase que acaba de terminar que a una
            # parecida anterior (saltarse «El combate se siente bastante lento» y leer «El combate se siente
            # demasiado lento…» marcaba la segunda en vez de la primera).
            n_e, n_o = len(escritas), len(oidas)
            comparador = SequenceMatcher(None, [w[0] for _, w in reversed(escritas)], oidas[::-1], autojunk=False)
            for bloque in comparador.get_matching_blocks():
                for k in range(bloque.size):
                    indice = n_e - 1 - (bloque.a + k)
                    dichas[indice] = True
                    if escritas[indice][0] in juzgadas:
                        usadas = max(usadas, n_o - (bloque.b + k))
        sospechosas: dict[int, list[str]] = {}
        for k in juzgadas:
            marcas = [(w, d) for (f, w), d in zip(escritas, dichas) if f == k]
            faltan = self._judge(marcas)
            if faltan:
                sospechosas[k] = faltan
        return sospechosas, usadas

    def _judge(self, marcas: list[tuple[tuple[str, str, bool], bool]]) -> list[str]:
        exigidas = [(w, d) for w, d in marcas if w[2]]
        if len(exigidas) < 2:
            return []
        oidas = sum(1 for _, d in exigidas if d)
        if oidas == 0 and len(exigidas) >= 3:
            return [NOT_READ]
        cobertura = oidas / len(exigidas)
        normal = statistics.median(self._coverage) if self._coverage else 1.0
        self._coverage.append(cobertura)
        if normal < _VERY_POOR_HEARING:
            return []                                 # el reconocedor apenas entiende: no se juzga lo que dices
        seguidas_min = self._consecutive + (1 if normal < _POOR_HEARING else 0)
        seguidas = peor = 0
        for (norm, _, _), dicha in exigidas:
            if dicha:
                seguidas = 0
            elif len(norm) >= 4:
                seguidas += 1
                peor = max(peor, seguidas)
        faltan = [w[1] for w, d in exigidas if not d]
        mucho_peor = len(self._coverage) > _WARMUP and cobertura < normal - self._drop and len(faltan) >= 2
        return faltan if peor >= seguidas_min or mucho_peor else []


def check_sentence(expected: str, heard: list[str], sensitivity: str = "media", lang: str = "es") -> list[str]:
    """Una frase suelta (sin historial de la sesión): palabras que parecen faltar, o [] si no hay bastante."""
    revisor = ReadingChecker(expected, sensitivity, lang)
    if not revisor.spans:
        return []
    sospechosas, _ = revisor.evaluate(list(range(len(revisor.spans))), heard)
    return [w for k in sorted(sospechosas) for w in sospechosas[k]]


class SessionLog:
    """Registro de una sesión de lectura, guardado en un JSON tras cada cambio (si se cierra de golpe, no se pierde)."""

    def __init__(self, folder: Path, text: str) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / time.strftime("%Y-%m-%d_%H-%M-%S.json")
        self._t0 = time.monotonic()
        self.spans = sentence_spans(text)
        self._text = text
        self.data = {
            "version": 1,
            "inicio": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "guion_crc": zlib.crc32(text.encode("utf-8")),
            "frases": [{"i": i, "inicio": a, "fin": b, "texto": text[a:b]} for i, (a, b) in enumerate(self.spans)],
            "eventos": [],
        }
        self.save()

    def matches(self, text: str) -> bool:
        return text == self._text

    def event(self, tipo: str, **campos) -> dict:
        evento = {"t": round(time.monotonic() - self._t0, 2), "tipo": tipo, **campos}
        self.data["eventos"].append(evento)
        self.save()
        return evento

    def read_ok_before(self, frase: int) -> bool:
        """¿Esa frase ya se leyó bien en esta sesión? (Saltársela al repasar no es un error.)"""
        return any(e["tipo"] == "frase_bien" and e.get("frase") == frase for e in self.data["eventos"])

    def to_review(self) -> list[int]:
        """Frases con un posible error sin resolver (no se volvieron a leer bien después)."""
        estado: dict[int, bool] = {}
        for e in self.data["eventos"]:
            if e["tipo"] == "posible_error":
                estado[e["frase"]] = True
            elif e["tipo"] == "frase_bien":
                estado[e["frase"]] = False
        return sorted(i for i, mal in estado.items() if mal)

    def save(self) -> None:
        temporal = self.path.with_suffix(".tmp")
        try:
            temporal.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(temporal, self.path)
        except OSError:
            log.exception("No se pudo guardar el registro de la sesión en %s", self.path)

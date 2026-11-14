"""Análisis gramatical para «Líneas por frases»: dónde haría una pausa una persona al leer en voz alta.

Usa spaCy con su modelo pequeño de español (es_core_news_sm, local, sin internet). Para cada hueco entre dos palabras
calcula un coste de hacer ahí una pausa: 0 entre oraciones, más dentro de una oración, y None donde nadie se pararía
(«el | juego», «Lucas | Dravek»). Si spaCy no está, text.py sigue funcionando con sus reglas.

El análisis tarda: se hace en un hilo y avisa con la señal ready. Solo GrammarAnalyzer usa Qt.
"""
from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QObject, Signal

log = logging.getLogger(__name__)

MODEL = "es_core_news_sm"
enabled = True                      # las pruebas lo apagan para no cargar el modelo en cada ventana

# Una línea no puede terminar en una palabra que depende de la siguiente, ni empezar en la continuación de un nombre.
_LEFT_TIGHT = {"det", "case", "mark", "cc", "aux", "aux:pass", "cop", "nummod", "expl:pv", "expl:pass", "expl:impers"}
_RIGHT_TIGHT = {"flat", "flat:name", "flat:foreign", "fixed", "compound"}
_CLITIC_OR_SUBJECT = {"obj", "iobj", "nsubj"}

# Coste de empezar línea en un grupo con esta función. Parecido a la distancia en palabras al centro del trozo.
_COST_BY_DEP = {
    "parataxis": 0.0, "advcl": 0.3, "acl": 0.5, "acl:relcl": 0.5, "vocative": 0.5, "discourse": 0.5, "dislocated": 1.0,
    "ccomp": 1.2, "appos": 1.5, "advmod": 1.5, "obl": 1.5, "csubj": 2.0, "xcomp": 2.0, "iobj": 3.0, "nsubj": 2.5,
    "ROOT": 2.5, "obj": 3.5, "nmod": 3.5,
}
_DEFAULT_COST = 3.0
_VERB_CONJ_COST = 0.0               # «y terminas odiándolos»: otra oración
_OTHER_CONJ_COST = 3.5              # «escenarios y combates»: una sola enumeración
_LONG_CONJ_COST = 1.5               # «y con una manera de hablar»: el segundo elemento ya es largo
_INSIDE_COST = 4.0                  # dentro de un grupo: último recurso si no cabe en la línea
_SUBJECT_VERB_COST = 2.5            # sujeto largo | verbo (con uno corto, nadie se para: «y Hank | termina»)
_LONG_SUBJECT = 4                   # palabras a partir de las que un sujeto admite pausa
_COMPLEMENT_EXTRA = 2.0             # complemento justo después de la palabra que lo pide
_COMPLEMENTS = {"obl", "obj", "iobj", "ccomp", "xcomp"}
_NEVER_LINE_END = {"ADP", "DET", "CCONJ", "SCONJ", "AUX"}   # preposiciones, artículos, conjunciones, «es», «ha»…
_RELATIVES = {"que", "quien", "quienes", "cual", "cuales"}
CLAUSE_COST = 0.5                   # hasta aquí, separa oraciones: se corta aunque el trozo quepa en la línea

_lock = threading.Lock()
_nlp = None
_failed = False


def _load():
    """El modelo, cargado una sola vez (tarda ~1,5 s); None si no se puede usar. Llamar con _lock tomado."""
    global _nlp, _failed
    if _nlp is None and not _failed:
        try:
            import spacy
            _nlp = spacy.load(MODEL, exclude=["ner", "lemmatizer", "attribute_ruler"])
            log.info("Analizador gramatical cargado (%s)", MODEL)
        except Exception:  # noqa: BLE001 - sin spaCy o sin el modelo, Cronista usa sus reglas
            log.exception("No se pudo cargar el analizador gramatical; se usan las reglas")
            _failed = True
    return _nlp


def pause_costs(text: str) -> dict[int, float | None] | None:
    """{posición en el texto donde empieza una palabra: coste de empezar línea ahí}. None si no hay analizador."""
    with _lock:
        nlp = _load()
        if nlp is None:
            return None
        costes: dict[int, float | None] = {}
        lineas = text.split("\n")
        inicios = []
        inicio = 0
        for linea in lineas:
            inicios.append(inicio)
            inicio += len(linea) + 1
        for doc, base in zip(nlp.pipe(lineas, batch_size=64), inicios):
            _doc_costs(doc, base, costes)
        return costes


def _doc_costs(doc, base: int, costes: dict[int, float | None]) -> None:
    for i in range(len(doc) - 1):
        if not doc[i].whitespace_:
            continue                                  # dentro de una palabra escrita («D-Day», «juego,»)
        izquierda = _content(doc, i, -1)
        derecha = _content(doc, i + 1, 1)
        if izquierda is not None and derecha is not None:
            costes[base + doc[i + 1].idx] = _gap_cost(izquierda, derecha)


def _content(doc, i: int, paso: int):
    """El token i, o el primero que no sea puntuación en esa dirección."""
    while 0 <= i < len(doc):
        if not doc[i].is_punct and not doc[i].is_space:
            return doc[i]
        i += paso
    return None


def _gap_cost(izq, der) -> float | None:
    if izq.pos_ in _NEVER_LINE_END or izq.lower_ in _RELATIVES:     # «frente a |», «el problema es |», «en la que |»
        return None
    if izq.head.i > izq.i and (izq.dep_ in _LEFT_TIGHT or izq.dep_ == "amod"
                               or (izq.head.i == der.i and izq.dep_ == "advmod")
                               or (izq.pos_ == "PRON" and izq.dep_ in _CLITIC_OR_SUBJECT and izq.head.i == der.i)):
        return None
    if der.dep_ in _RIGHT_TIGHT or (der.head.i == izq.i and der.dep_ in ("amod", "nummod")):
        return None
    if der.left_edge.i != der.i:                  # el grupo de la derecha ya empezó antes: el hueco está dentro de él
        sujeto = _subject_ending_at(izq, der)
        if sujeto is None:
            return _INSIDE_COST
        return _SUBJECT_VERB_COST if sujeto.right_edge.i - sujeto.left_edge.i + 1 >= _LONG_SUBJECT else _INSIDE_COST
    grupo = der
    while grupo.head.i != grupo.i and grupo.head.left_edge.i == der.i:
        grupo = grupo.head
    if grupo.dep_ == "conj":
        if grupo.pos_ in ("VERB", "AUX"):
            return _VERB_CONJ_COST
        return _LONG_CONJ_COST if grupo.right_edge.i - der.i + 1 >= 3 else _OTHER_CONJ_COST
    coste = _COST_BY_DEP.get(grupo.dep_, _DEFAULT_COST)
    if grupo.head.i == izq.i and grupo.dep_ in _COMPLEMENTS:      # «carga | con el duelo»: el verbo pide lo que sigue
        coste += _COMPLEMENT_EXTRA
    return coste


def _subject_ending_at(izq, verbo):
    """El sujeto de ese verbo si la palabra de la izquierda lo cierra; si no, None."""
    for h in [izq, *izq.ancestors]:
        if h.dep_ == "nsubj" and h.head.i == verbo.i and h.right_edge.i == izq.i:
            return h
    return None


class GrammarAnalyzer(QObject):
    """Analiza textos en un hilo. ready(texto, costes) llega cuando termina; solo vale el último texto pedido."""

    ready = Signal(object, object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._wanted: str | None = None

    def analyze(self, text: str) -> None:
        self._wanted = text
        threading.Thread(target=self._run, args=(text,), name="cronista-gramatica", daemon=True).start()

    def _run(self, text: str) -> None:
        try:
            costes = pause_costs(text)
        except Exception:  # noqa: BLE001 - un fallo del análisis no debe tumbar nada: se siguen usando las reglas
            log.exception("Falló el análisis gramatical")
            return
        if text == self._wanted and costes is not None:
            try:
                self.ready.emit(text, costes)
            except RuntimeError:                      # la ventana ya se cerró
                pass

"""Relaciones entre notas de una sesión, sin IA ni dependencias nuevas.

Mide la similitud entre dos comentarios combinando tres señales convencionales:
texto (TF-IDF con coseno), temas compartidos (Jaccard) y cercanía temporal en la
grabación. Las referencias externas compartidas (otro juego, una película...)
fuerzan una relación fuerte. Con las relaciones se pueden agrupar las notas en
"hilos" (componentes conexas) para el panel de la sesión.

Todo es local y determinista: mismas notas, mismo resultado.
"""
from __future__ import annotations

import logging
import math
import re
import unicodedata
from collections import Counter, defaultdict, deque
from typing import Any

log = logging.getLogger(__name__)

# Palabras vacías del español, sin tildes (se comparan con el texto normalizado).
SPANISH_STOPWORDS: frozenset[str] = frozenset("""
a ante bajo cabe con contra de desde durante en entre hacia hasta mediante para por segun sin sobre tras al del
el la los las un una unos unas lo
yo tu ella nosotros nosotras vosotros vosotras ustedes ellos ellas me te se nos os le les mi mis su sus
nuestro nuestra nuestros nuestras vuestro vuestra mio mia tuyo tuya suyo suya
este esta estos estas ese esa esos esas eso esto aquel aquella aquellos aquello
que quien quienes cual cuales cuyo cuya algo alguien nada nadie todo toda todos todas otro otra otros otras
mismo misma mucho mucha muchos muchas poco poca pocos pocas varios varias cada
y e o u ni pero sino aunque porque pues si como cuando mientras donde
ya tambien ademas incluso solo solamente no muy mas menos aqui alli ahi alla ahora siempre nunca asi bien
tan tanto casi aun todavia luego entonces despues antes
soy eres es somos sois son ser sido siendo estoy estamos estan estar estaba estaban
he has ha hemos han haber hay habia hubo tengo tienes tiene tienen tener tenia hago haces hace hacemos hacen
hacer hecho voy vas va vamos van ir fue fueron era eran sea sean
bueno pues eh ah oh mm hmm osea vale claro oye
""".split())

_LATIN_WORD = re.compile(r"[a-z0-9]+")
_TIME_SCALE_MS = 600_000          # escala temporal de la relación (10 min)
_W_TEXT, _W_TOPIC, _W_TIME = 0.55, 0.30, 0.15


def normalize_text(text: str) -> str:
    """Minúsculas y sin tildes (NFKD). Conserva letras no latinas tal cual salvo las marcas diacríticas."""
    decomposed = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _stem(tok: str) -> str:
    """Raíz ligera: quita plurales y el sufijo adverbial 'mente'.

    El plural en '-es' solo se quita tras consonante ('niveles'→'nivel', 'ciudades'→'ciudad');
    tras vocal se quita solo la 's' ('jefes'→'jefe', 'mentes'→'mente'), para que casen con el singular."""
    if len(tok) > 4 and tok.endswith("es") and tok[-3] in "lrndjyzc":
        tok = tok[:-2]
        if tok.endswith("c") and len(tok) >= 3:     # "luces" → "luz"
            tok = tok[:-1] + "z"
    elif len(tok) > 4 and tok.endswith("s"):
        tok = tok[:-1]
    if len(tok) > 7 and tok.endswith("mente"):
        tok = tok[:-5]
    return tok


def tokenize(text: str) -> list[str]:
    """Términos de búsqueda de un texto: sin tildes, sin stopwords, sin tokens de 1-2 letras (salvo dígitos)."""
    out: list[str] = []
    for tok in _LATIN_WORD.findall(normalize_text(text)):
        if tok in SPANISH_STOPWORDS:
            continue
        if len(tok) <= 2 and not tok.isdigit():
            continue
        out.append(_stem(tok))
    return out


def _dot(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(w * b.get(t, 0.0) for t, w in a.items())


class TfIdfIndex:
    """Índice TF-IDF en memoria con vectores dispersos normalizados (coseno).

    idf suavizado: log((1+N)/(1+df)) + 1. Los términos que no están en el índice
    no aportan a los vectores de consulta.
    """

    def __init__(self, docs: list[tuple[int, str]]) -> None:
        tokenized = [(doc_id, tokenize(text)) for doc_id, text in docs]
        n_docs = len(tokenized)
        df: Counter[str] = Counter()
        for _, toks in tokenized:
            df.update(set(toks))
        self._idf = {t: math.log((1 + n_docs) / (1 + d)) + 1.0 for t, d in df.items()}
        self._vecs: dict[int, dict[str, float]] = {}
        for doc_id, toks in tokenized:
            self._vecs[doc_id] = self._weigh(toks)

    def _weigh(self, tokens: list[str]) -> dict[str, float]:
        vec: dict[str, float] = {}
        for term, count in Counter(tokens).items():
            idf = self._idf.get(term)
            if idf is not None:
                vec[term] = count * idf
        norm = math.sqrt(sum(w * w for w in vec.values()))
        if norm == 0:
            return {}
        return {t: w / norm for t, w in vec.items()}

    def vector(self, text: str) -> dict[str, float]:
        """Vector TF-IDF normalizado (L2) de un texto con el vocabulario del índice."""
        return self._weigh(tokenize(text))

    def cosine(self, a_text: str, b_text: str) -> float:
        return _dot(self.vector(a_text), self.vector(b_text))

    def pair(self, a_id: int, b_id: int) -> float:
        """Coseno entre dos documentos ya indexados (0.0 si alguno no existe)."""
        return _dot(self._vecs.get(a_id, {}), self._vecs.get(b_id, {}))

    def similar(self, text: str, top_k: int = 5, min_score: float = 0.15,
                exclude: set[int] | None = None) -> list[tuple[int, float]]:
        """Documentos más parecidos a `text`, de mayor a menor similitud."""
        q = self.vector(text)
        if not q:
            return []
        scored: list[tuple[int, float]] = []
        for doc_id, vec in self._vecs.items():
            if exclude and doc_id in exclude:
                continue
            s = _dot(q, vec)
            if s > 0 and s >= min_score:
                scored.append((doc_id, s))
        scored.sort(key=lambda kv: (-kv[1], kv[0]))
        return scored[:top_k]


def _text(note: dict[str, Any]) -> str:
    return str(note.get("text") or "")


def _ref_names(note: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for r in note.get("refs") or []:
        name = r.get("name") if isinstance(r, dict) else r
        if name:
            norm = " ".join(normalize_text(str(name)).split())
            if norm:
                out.add(norm)
    return out


def compute_relations(note: dict[str, Any], others: list[dict[str, Any]], max_related: int = 5,
                      threshold: float = 0.35) -> list[tuple[int, str, float]]:
    """Relaciones de `note` con `others`: [(id_otra, tipo, puntuación)], de mayor a menor puntuación.

    Tipos: "same_reference" (comparten una referencia externa; puntuación mínima 0.9),
    "same_topic" (los temas pesan más que el texto) o "similar_text".
    """
    note_id = int(note["id"])
    unique: dict[int, dict[str, Any]] = {}
    for o in others:
        oid = int(o["id"])
        if oid != note_id and oid not in unique:
            unique[oid] = o
    if not unique:
        return []

    docs = [(note_id, _text(note))] + [(oid, _text(o)) for oid, o in unique.items()]
    index = TfIdfIndex(docs)

    note_topics = set(note.get("topics") or [])
    note_sub = str((note.get("context") or {}).get("subtopic") or "")
    note_pos = note.get("position_ms")
    note_refs = _ref_names(note)

    results: list[tuple[int, str, float]] = []
    for oid, o in unique.items():
        cos = index.pair(note_id, oid)

        o_topics = set(o.get("topics") or [])
        union = note_topics | o_topics
        topic = len(note_topics & o_topics) / len(union) if union else 0.0
        o_sub = str((o.get("context") or {}).get("subtopic") or "")
        if note_sub and note_sub == o_sub:
            topic = min(1.0, topic + 0.3)

        o_pos = o.get("position_ms")
        if note_pos is not None and o_pos is not None:
            time_score = math.exp(-abs(int(note_pos) - int(o_pos)) / _TIME_SCALE_MS)
        else:
            time_score = 0.0

        score = _W_TEXT * cos + _W_TOPIC * topic + _W_TIME * time_score
        if note_refs & _ref_names(o):
            kind = "same_reference"
            score = max(score, 0.9)
        else:
            kind = "same_topic" if topic >= cos else "similar_text"

        if score >= threshold:
            results.append((oid, kind, round(score, 3)))

    results.sort(key=lambda r: (-r[2], r[0]))
    return results[:max_related]


def _pos_key(note: dict[str, Any]) -> tuple[bool, int, int]:
    pos = note.get("position_ms")
    return (pos is None, int(pos or 0), int(note["id"]))


def _most_common(values: list[str]) -> str:
    # Counter.most_common desempata por orden de aparición (notas en orden temporal).
    top = Counter(values).most_common(1)
    return top[0][0] if top else ""


def topic_clusters(notes: list[dict[str, Any]], relations_by_note: dict[int, list[int]]) -> list[dict[str, Any]]:
    """Agrupa notas en componentes conexas según las relaciones (tratadas como no dirigidas).

    Solo devuelve grupos de 2 o más notas, con más notas primero.
    """
    by_id = {int(n["id"]): n for n in notes}
    adj: dict[int, set[int]] = defaultdict(set)
    for a, nbrs in relations_by_note.items():
        a = int(a)
        if a not in by_id:
            continue
        for b in nbrs:
            b = int(b)
            if b in by_id and b != a:
                adj[a].add(b)
                adj[b].add(a)

    seen: set[int] = set()
    clusters: list[dict[str, Any]] = []
    for start in sorted(adj):
        if start in seen:
            continue
        comp: list[int] = []
        queue = deque([start])
        seen.add(start)
        while queue:
            cur = queue.popleft()
            comp.append(cur)
            for nb in adj[cur]:
                if nb not in seen:
                    seen.add(nb)
                    queue.append(nb)
        if len(comp) < 2:
            continue

        members = sorted((by_id[i] for i in comp), key=_pos_key)
        topics: list[str] = []
        subtopics: list[str] = []
        for m in members:
            topics.extend(str(t) for t in dict.fromkeys(m.get("topics") or []))
            sub = (m.get("context") or {}).get("subtopic")
            if sub:
                subtopics.append(str(sub))
        positions = [int(m["position_ms"]) for m in members if m.get("position_ms") is not None]
        clusters.append({
            "topic": _most_common(topics),
            "subtopic": _most_common(subtopics),
            "note_ids": [int(m["id"]) for m in members],
            "first_ms": min(positions) if positions else None,
            "last_ms": max(positions) if positions else None,
            "count": len(members),
        })

    clusters.sort(key=lambda c: -c["count"])
    return clusters

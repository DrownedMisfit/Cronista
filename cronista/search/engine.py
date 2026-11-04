"""Búsqueda semántica de notas de todas las sesiones, con filtros y comparación entre juegos.

Puntuación híbrida: `semantic_weight` * coseno de embeddings multilingües + (1 - semantic_weight) * TF-IDF.
Los embeddings se guardan en `note_embeddings` junto con el hash del texto; si una nota cambia, su
embedding queda obsoleto y se recalcula en la siguiente indexación. Todo local.
"""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from typing import Any

import numpy as np

from cronista.notes.relations import TfIdfIndex
from cronista.notes.store import Store
from cronista.search.embedder import MODEL_NAME, SentenceEncoder, model_available

log = logging.getLogger(__name__)

MIN_SCORE = 0.2          # por debajo de esta puntuación un resultado no se muestra
_SEMANTIC_HIT = 0.4      # coseno a partir del cual una coincidencia semántica cuenta como "fuerte"


def _note_text(note: dict[str, Any]) -> str:
    return str(note.get("text") or note.get("text_raw") or "").strip()


def _browse_key(note: dict[str, Any]) -> tuple:
    pos = note.get("position_ms")
    return (str(note.get("session_started") or ""), int(note.get("session_id") or 0),
            pos is None, int(pos or 0), int(note["id"]))


class SearchEngine:
    def __init__(self, store: Store) -> None:
        self.store = store
        self._encoder = SentenceEncoder()

    # ---- indexación --------------------------------------------------------------
    @staticmethod
    def text_hash(text: str) -> str:
        """SHA-1 del texto con los espacios colapsados (mayúsculas y tildes se conservan: el modelo las usa)."""
        normalized = " ".join((text or "").split())
        return hashlib.sha1(normalized.encode("utf-8")).hexdigest()

    def index(self, progress: Callable[[float, str], None] | None = None,
              cancelled: Callable[[], bool] | None = None, batch: int = 64) -> dict[str, Any]:
        """Calcula embeddings de las notas nuevas o cuyo texto cambió. Solo esas, en lotes."""
        notes = self.store.all_notes()
        stored = self.store.embeddings(MODEL_NAME)
        pending: list[tuple[int, str, str]] = []
        for n in notes:
            text = _note_text(n)
            if not text:
                continue
            nid = int(n["id"])
            h = self.text_hash(text)
            if nid not in stored or stored[nid][0] != h:
                pending.append((nid, text, h))
        total = len(notes)
        if not pending:
            return {"message": "Índice de búsqueda al día", "indexed": 0, "total": total}
        if not model_available():
            raise RuntimeError("Falta el modelo de embeddings (models/embed/paraphrase-multilingual-MiniLM-L12-v2)")

        indexed = 0
        try:
            for start in range(0, len(pending), batch):
                if cancelled and cancelled():
                    return {"message": f"Indexación cancelada tras {indexed} notas", "indexed": indexed,
                            "total": total}
                chunk = pending[start:start + batch]
                vectors = self._encoder.encode([text for _, text, _ in chunk], batch=batch)
                for (nid, _, h), vec in zip(chunk, vectors):
                    self.store.set_embedding(nid, MODEL_NAME, h, vec.astype(np.float32).tobytes())
                indexed += len(chunk)
                if progress:
                    progress(indexed / len(pending), f"Indexando notas: {indexed}/{len(pending)}")
        finally:
            if indexed > 0:
                self._encoder.unload()
        return {"message": f"Indexadas {indexed} notas nuevas o modificadas", "indexed": indexed, "total": total}

    def unload(self) -> None:
        """Libera el codificador (la siguiente búsqueda semántica volverá a cargarlo)."""
        self._encoder.unload()

    # ---- búsqueda ----------------------------------------------------------------
    @staticmethod
    def _candidates(game: str | None, scope: str | None, function: str | None, topic: str | None,
                    marker: str | None, session_id: int | None, store: Store) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for n in store.all_notes(game):
            if scope and n.get("scope") != scope:
                continue
            if function and function not in (n.get("functions") or []):
                continue
            if topic and topic not in (n.get("topics") or []):
                continue
            if marker and n.get("marker") != marker:
                continue
            if session_id is not None and int(n.get("session_id") or 0) != int(session_id):
                continue
            out.append(n)
        return out

    def _valid_vectors(self, notes: list[dict[str, Any]]) -> dict[int, np.ndarray]:
        """Embeddings vigentes (el hash coincide con el texto actual) de `notes`."""
        stored = self.store.embeddings(MODEL_NAME)
        out: dict[int, np.ndarray] = {}
        for n in notes:
            nid = int(n["id"])
            item = stored.get(nid)
            if item is None or item[0] != self.text_hash(_note_text(n)):
                continue
            out[nid] = np.frombuffer(item[1], dtype=np.float32)
        return out

    def _rank(self, query: str, notes: list[dict[str, Any]], limit: int, semantic_weight: float) -> list[dict]:
        if not notes:
            return []
        lexical = dict(TfIdfIndex([(int(n["id"]), _note_text(n)) for n in notes])
                       .similar(query, top_k=len(notes), min_score=0.0))
        vectors = self._valid_vectors(notes)
        qvec = None
        if vectors and model_available():
            qvec = self._encoder.encode([query])[0]
        w = min(1.0, max(0.0, float(semantic_weight)))

        scored: list[tuple[float, dict[str, Any], str]] = []
        for n in notes:
            nid = int(n["id"])
            lex = float(lexical.get(nid, 0.0))
            if qvec is None or nid not in vectors:
                score, match = lex, "palabras"
            else:
                sem = float(np.dot(qvec, vectors[nid]))
                score = w * sem + (1.0 - w) * lex
                sem_hit, lex_hit = sem >= _SEMANTIC_HIT, lex > 0.0
                match = "ambas" if (sem_hit and lex_hit) else ("palabras" if lex_hit else "semántica")
            if score >= MIN_SCORE:
                scored.append((score, n, match))
        scored.sort(key=lambda t: -t[0])          # estable: a igual puntuación, orden cronológico
        return [dict(n, score=round(score, 3), match=match) for score, n, match in scored[:limit]]

    def search(self, query: str, *, game: str | None = None, scope: str | None = None,
               function: str | None = None, topic: str | None = None, marker: str | None = None,
               session_id: int | None = None, limit: int = 30, semantic_weight: float = 0.75) -> list[dict]:
        """Notas que coinciden con `query` por significado y/o palabras, tras aplicar los filtros.

        Con consulta vacía devuelve las notas filtradas en orden de sesión y posición, sin puntuar.
        """
        notes = self._candidates(game, scope, function, topic, marker, session_id, self.store)
        q = (query or "").strip()
        if not q:
            return [dict(n) for n in sorted(notes, key=_browse_key)[:limit]]
        return self._rank(q, notes, limit, semantic_weight)

    def compare_games(self, query: str, games: list[str], per_game: int = 5,
                      semantic_weight: float = 0.75) -> dict[str, list[dict]]:
        """Mejores resultados de la misma consulta en cada juego, para comparar opiniones."""
        return {g: self.search(query, game=g, limit=per_game, semantic_weight=semantic_weight) for g in games}

    def similar_notes(self, note_id: int, limit: int = 8, other_games_only: bool = False) -> list[dict]:
        """Notas más parecidas a una nota por embedding (la propia no aparece)."""
        notes = self.store.all_notes()
        target = next((n for n in notes if int(n["id"]) == int(note_id)), None)
        if target is None:
            return []
        pool = [n for n in notes if int(n["id"]) != int(note_id)]
        if other_games_only:
            pool = [n for n in pool if n.get("game") != target.get("game")]
        vectors = self._valid_vectors([target] + pool)
        tvec = vectors.get(int(note_id))
        if tvec is None:
            if not _note_text(target) or not model_available():
                return []
            tvec = self._encoder.encode([_note_text(target)])[0]
        scored: list[tuple[float, dict[str, Any]]] = []
        for n in pool:
            v = vectors.get(int(n["id"]))
            if v is None:
                continue
            s = float(np.dot(tvec, v))
            if s >= MIN_SCORE:
                scored.append((s, n))
        scored.sort(key=lambda t: -t[0])
        return [dict(n, score=round(s, 3), match="semántica") for s, n in scored[:limit]]

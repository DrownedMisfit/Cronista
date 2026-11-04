"""Personalización local (sin IA): ejemplos de correcciones, vocabulario del estilo y juegos conocidos.

Todo se guarda en la base de datos local (tabla de correcciones y claves `learn.*`).
Nada sale de este equipo. Cada parte depende de su opción en Ajustes → Aprendizaje:
  - learn_corrections: usar las correcciones del usuario como ejemplos similares.
  - learn_style: aprender nombres propios y expresiones que el usuario repite.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from typing import TYPE_CHECKING, Any

from cronista.notes.relations import SPANISH_STOPWORDS, TfIdfIndex, normalize_text

if TYPE_CHECKING:
    from cronista.core.settings import Settings
    from cronista.notes.store import Store

log = logging.getLogger(__name__)

_VOCAB_KEY = "learn.vocab"
_GAMES_KEY = "learn.games"
_SENTENCE_SPLIT = re.compile(r"[.!?;:,()\"«»“”\n¿¡…]+")
_WORD = re.compile(r"\w+")
_CONNECTORS = frozenset({"de", "of", "the", "del", "la"})


def _is_cap(word: str) -> bool:
    return word[0].isupper()


def _is_cap_or_num(word: str) -> bool:
    return word[0].isupper() or word[0].isdigit()


def _extract_terms(text: str) -> list[str]:
    """Secuencias de palabras con mayúscula inicial ("Resident Evil 4", "Rise of Iron").

    Reglas: los conectores ("de", "of", "the"...) solo van en medio, nunca al principio
    ni al final. Una sola palabra al principio de una frase se descarta (suele ser
    mayúscula por gramática). Se descartan las secuencias formadas solo por stopwords.
    """
    terms: list[str] = []
    for sentence in _SENTENCE_SPLIT.split(text or ""):
        words = _WORD.findall(sentence)
        i = 0
        while i < len(words):
            if not _is_cap(words[i]):
                i += 1
                continue
            start = i
            seq = [words[i]]
            k = i + 1
            while k < len(words):
                if _is_cap_or_num(words[k]):
                    seq.append(words[k])
                    k += 1
                elif (words[k].lower() in _CONNECTORS and k + 1 < len(words)
                      and _is_cap_or_num(words[k + 1])):
                    seq.extend([words[k], words[k + 1]])
                    k += 2
                else:
                    break
            i = k
            if start == 0 and len(seq) == 1:
                continue
            if all(normalize_text(w) in SPANISH_STOPWORDS for w in seq):
                continue
            terms.append(" ".join(seq))
    return terms


class Personalizer:
    """Personalización a partir de las correcciones y el vocabulario del usuario.

    El índice de ejemplos se cachea en memoria; se reconstruye si cambia el número de
    ejemplos o al llamar a `invalidate()`.
    """

    def __init__(self, store: "Store", settings: "Settings") -> None:
        self.store = store
        self.settings = settings
        self._index: TfIdfIndex | None = None
        self._index_n = -1

    def invalidate(self) -> None:
        self._index = None
        self._index_n = -1

    # ---- ejemplos de correcciones ------------------------------------------
    def examples_for(self, text: str, k: int = 4) -> list[dict[str, Any]]:
        """Hasta `k` correcciones anteriores parecidas al texto; si no hay ninguna, las `k` más recientes."""
        if not self.settings.learning.learn_corrections:
            return []
        examples = self.store.corrected_examples()
        if not examples:
            return []
        if self._index is None or self._index_n != len(examples):
            self._index = TfIdfIndex([(int(n["id"]), str(n.get("text") or "")) for n in examples])
            self._index_n = len(examples)

        by_id = {int(n["id"]): n for n in examples}
        similar = self._index.similar(text, top_k=k, min_score=0.1)
        picked = [by_id[doc_id] for doc_id, _ in similar if doc_id in by_id] or examples[:k]
        return [{"text": n.get("text") or "", "scope": n.get("scope") or "",
                 "functions": list(n.get("functions") or []), "topics": list(n.get("topics") or [])}
                for n in picked[:k]]

    # ---- vocabulario del estilo --------------------------------------------
    def style_vocabulary(self, max_terms: int = 40, refresh: bool = False) -> list[str]:
        """Nombres propios y expresiones que el usuario repite (2 apariciones o más), más frecuentes primero.

        Se cachea en `learn.vocab`. Se guarda la lista completa y se devuelven `max_terms`.
        """
        if not self.settings.learning.learn_style:
            return []
        if not refresh:
            cached = self.store.kv_get(_VOCAB_KEY, None)
            if isinstance(cached, list):
                return [str(t) for t in cached[:max_terms]]

        counts: Counter[str] = Counter()
        first_form: dict[str, str] = {}
        for text in self.store.recent_texts():
            for term in _extract_terms(text):
                key = term.casefold()
                counts[key] += 1
                first_form.setdefault(key, term)
        terms = [first_form[key] for key, c in counts.most_common() if c >= 2]
        self.store.kv_set(_VOCAB_KEY, terms)
        return terms[:max_terms]

    # ---- juegos conocidos ----------------------------------------------------
    def known_games(self) -> list[str]:
        """Juegos de las sesiones, los recordados y las referencias repetidas en correcciones (sin duplicados)."""
        candidates: list[str] = list(self.store.list_games())
        stored = self.store.kv_get(_GAMES_KEY, []) or []
        candidates.extend(str(g) for g in stored)

        ref_counts: Counter[str] = Counter()
        ref_form: dict[str, str] = {}
        for note in self.store.corrected_examples():
            names: set[str] = set()
            for r in note.get("refs") or []:
                name = (r.get("name") if isinstance(r, dict) else r) or ""
                name = str(name).strip()
                if name:
                    key = name.casefold()
                    names.add(key)
                    ref_form.setdefault(key, name)
            ref_counts.update(names)
        candidates.extend(ref_form[key] for key, c in ref_counts.items() if c >= 2)

        out: list[str] = []
        seen: set[str] = set()
        for game in candidates:
            game = (game or "").strip()
            key = game.casefold()
            if game and key not in seen:
                seen.add(key)
                out.append(game)
        return out

    def remember_game(self, name: str) -> None:
        """Recuerda un juego para futuras clasificaciones (solo si alguna personalización está activa)."""
        if not (self.settings.learning.learn_style or self.settings.learning.learn_corrections):
            return
        name = (name or "").strip()
        if not name:
            return
        games = [str(g) for g in (self.store.kv_get(_GAMES_KEY, []) or [])]
        if name.casefold() in {g.casefold() for g in games}:
            return
        games.append(name)
        self.store.kv_set(_GAMES_KEY, games)

    # ---- borrado -------------------------------------------------------------
    def clear(self) -> None:
        """Borra correcciones y claves `learn.*` de la base de datos, y las cachés en memoria."""
        self.store.clear_learning()
        self.invalidate()

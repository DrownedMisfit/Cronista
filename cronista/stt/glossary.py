"""Glosario bilingüe: títulos y términos en inglés dentro de comentarios en español.

Whisper con el idioma fijado en español tiende a "españolizar" lo que dices en inglés ("D-Day" → "Di-Day",
"y de"). Tres capas lo resuelven sin modelos nuevos:

1. Pistas al reconocedor (initial_prompt + hotwords): los términos del glosario, el juego de la sesión y los
   juegos que ya registraste, con una frase de ejemplo bilingüe (lo que mejor funciona con Whisper).
2. Correcciones explícitas que el usuario define: "Rise of Iron: D-Day = Rise of Iron Di Day, Rise of Iron di de".
3. Corrector aproximado: ventanas de palabras del texto que se parecen mucho (sin tildes, sin signos) a un
   término de varias palabras o a una palabra larga del glosario se sustituyen por la grafía correcta.
   Es conservador: umbral alto y solo términos de 2+ palabras o palabras de 6+ letras.

Formato de cada línea del glosario:  Término correcto [= variante mal entendida, otra variante]
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher

# términos de videojuegos que se dicen en inglés en una conversación en español
GAMING_TERMS = [
    "gameplay", "boss", "checkpoint", "hitbox", "frame rate", "input lag", "spawn", "respawn", "loot", "crafting",
    "skill tree", "open world", "cutscene", "quick time event", "headshot", "shooter", "souls-like", "roguelike",
    "metroidvania", "DLC", "remake", "remaster", "game over", "side quest", "fast travel", "hub", "lore", "speedrun",
    "stealth", "cooldown", "parry", "dash", "combo", "health bar", "HUD", "framerate", "ray tracing", "co-op",
    "multiplayer", "single player", "battle royale", "early access", "patch", "nerf", "buff", "grinding", "farming",
]

_WORD = re.compile(r"[\w'’]+", re.UNICODE)


def norm(text: str) -> str:
    """Minúsculas, sin tildes ni signos; guiones y dos puntos como espacios."""
    t = unicodedata.normalize("NFKD", text.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[-:_/.,;!?¡¿\"'’()\[\]]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


@dataclass
class Glossary:
    terms: list[str] = field(default_factory=list)            # grafías correctas
    aliases: dict[str, str] = field(default_factory=dict)     # variante normalizada → término correcto

    @classmethod
    def from_lines(cls, lines: list[str], extra_terms: list[str] | None = None) -> "Glossary":
        g = cls()
        seen: set[str] = set()

        def add(term: str) -> None:
            term = term.strip()
            if term and norm(term) not in seen:
                seen.add(norm(term))
                g.terms.append(term)

        for line in lines:
            if not line or not line.strip() or line.strip().startswith("#"):
                continue
            if "=" in line:
                correct, variants = line.split("=", 1)
                add(correct)
                for v in variants.split(","):
                    if v.strip():
                        g.aliases[norm(v)] = correct.strip()
            else:
                add(line)
        for t in extra_terms or []:
            add(t)
        return g

    # ---- pistas para Whisper -------------------------------------------------------------------
    def prompt(self, game: str = "", limit: int = 24) -> tuple[str, str | None]:
        """(initial_prompt, hotwords=None). Medido con Whisper large-v3-turbo y frases mezcladas ES/EN:
        - una frase de ejemplo en el prompt hace que Whisper la COPIE en la transcripción ("Estoy jugando…" ×2);
        - listas largas o prompt + hotwords duplicados provocan repeticiones ("el input lag y el input lag").
        Lo que funcionó (5/5 frases bien, con puntuación): un prompt corto «Notas sobre <juego>. término, …»
        con ~24 términos como máximo, por prioridad (juego de la sesión, tu glosario, juegos conocidos,
        términos de videojuegos), y sin hotwords."""
        terms = ([game] if game else []) + [t for t in self.terms if norm(t) != norm(game)]
        terms = terms[:limit]
        head = f"Notas sobre {game}. " if game else "Notas sobre videojuegos. "
        return (head + ", ".join(terms) + "." if terms else head.strip()), None

    # ---- corrección del texto --------------------------------------------------------------------
    def fix(self, text: str, game: str = "", threshold: float = 0.86) -> tuple[str, list[dict]]:
        """Devuelve (texto corregido, cambios [{"from", "to", "how"}])."""
        if not text:
            return text, []
        changes: list[dict] = []
        candidates = ([game] if game else []) + self.terms
        tokens = list(_WORD.finditer(text))
        if not tokens:
            return text, []
        # Todas las ventanas candidatas con su puntuación; luego se eligen sin solaparse, de mejor a peor.
        # (Elegir "la ventana más larga" se comía palabras vecinas: «Rise of Iron Di Day y» → se perdía la «y».)
        max_n = max([len(norm(t).split()) for t in candidates] + [len(a.split()) for a in self.aliases] + [1]) + 1
        found: list[tuple[float, int, int, str | None, str]] = []   # (puntuación, i, n, término|None=ya bien, cómo)
        for n in range(1, min(max_n, len(tokens)) + 1):
            for i in range(0, len(tokens) - n + 1):
                a, b = tokens[i].start(), tokens[i + n - 1].end()
                wn = norm(text[a:b])
                if not wn:
                    continue
                if wn in self.aliases:
                    found.append((1.01, i, n, self.aliases[wn], "corrección"))
                    continue
                exact = next((t for t in candidates if norm(t) == wn), None)
                if exact is not None:
                    # ya está bien dicho: se protege; si es un nombre propio escrito distinto («Rise of Iron D Day»)
                    # se unifica a la grafía del glosario para que búsquedas y guiones lo agrupen
                    proper = len(exact.split()) > 1 or any(c.isupper() for c in exact) or "-" in exact
                    canon = exact if proper and text[a:b] != exact else None
                    found.append((1.0, i, n, canon, "grafía"))
                    continue
                if any(wn in (norm(t) + "s", norm(t) + "es") for t in candidates):
                    found.append((1.0, i, n, None, ""))      # plural de un término: está bien dicho
                    continue
                target, score = _best_match(wn, n, candidates, threshold, game)
                if target:
                    found.append((score, i, n, target, "parecido"))
        # a igual calidad, gana la coincidencia que cubre más palabras: «Rise of Iron a Day» debe ser el título
        # completo aunque «Rise of Iron» también coincida exactamente con otro término más corto
        found.sort(key=lambda f: (-(f[0] + 0.03 * f[2]), -f[2]))
        used = [False] * len(tokens)
        replacements: list[tuple[int, int, str, str]] = []      # (inicio, fin, nuevo, cómo)
        for score, i, n, target, how in found:
            if any(used[i:i + n]):
                continue
            for k in range(i, i + n):
                used[k] = True
            if target is not None:
                replacements.append((tokens[i].start(), tokens[i + n - 1].end(), target, how))
        if not replacements:
            return text, []
        out, last = [], 0
        for a, b, new, how in sorted(replacements):
            out.append(text[last:a])
            out.append(new)
            changes.append({"from": text[a:b], "to": new, "how": how})
            last = b
        out.append(text[last:])
        return "".join(out), changes


_PHONETIC = [(r"ph", "f"), (r"th", "t"), (r"ck", "k"), (r"qu", "k"), (r"c(?=[aou])", "k"), (r"c(?=[ei])", "s"),
             (r"z", "s"), (r"v", "b"), (r"w", "u"), (r"y", "i"), (r"x", "ks"), (r"h", ""), (r"oe", "e")]


_LINKS = {"y", "e", "o", "u", "de", "del", "el", "la", "los", "las", "lo", "un", "una", "que", "en", "a", "al", "con",
          "por", "para", "se", "me", "te", "es", "pero", "como", "mas", "muy"}


def phonetic(text_norm: str) -> str:
    """Clave fonética aproximada (cómo suena dicho por un hispanohablante): «phantom» ≈ «fantom»."""
    t = text_norm
    for pat, rep in _PHONETIC:
        t = re.sub(pat, rep, t)
    return re.sub(r"(.)+", r"", t)          # letras repetidas → una


def _best_match(window_norm: str, n_tokens: int, candidates: list[str], threshold: float,
                game: str = "") -> tuple[str | None, float]:
    """Mejor término del glosario para una ventana de texto. Puntuación = máx(parecido escrito, parecido
    fonético), penalizando las letras de la ventana que el término no explica (así «Rise of Ironed» elige
    «Rise of Iron: D-Day» y no «Rise of Iron»), con una pequeña preferencia por el juego de la sesión."""
    best, best_score = None, 0.0
    w_ph = phonetic(window_norm)
    for term in candidates:
        tn = norm(term)
        t_tokens = len(tn.split())
        if t_tokens == 1 and len(tn) < 6:
            continue                              # palabras cortas: demasiado riesgo de falsos cambios
        # el término puede tener hasta 2 palabras más (el reconocedor a veces funde «Iron D-Day» en «Wared»)
        if not (-1 <= t_tokens - n_tokens <= 2):
            continue
        # la ventana no puede empezar ni terminar en una palabra de enlace española que el término no tenga
        # («Rise of Ironed y» no debe tragarse la «y»)
        w_tok, t_tok = window_norm.split(), tn.split()
        if (w_tok[-1] in _LINKS and w_tok[-1] != t_tok[-1]) or (w_tok[0] in _LINKS and w_tok[0] != t_tok[0]):
            continue
        if not (0.70 <= len(window_norm) / max(1, len(tn)) <= 1.33):
            continue
        # la primera palabra debe parecerse (evita cambiar frases que solo comparten el final)
        first_w, first_t = window_norm.split()[0], tn.split()[0]
        if max(SequenceMatcher(None, first_w, first_t).ratio(),
               SequenceMatcher(None, phonetic(first_w), phonetic(first_t)).ratio()) < 0.75:
            continue
        score = max(SequenceMatcher(None, window_norm, tn).ratio(), SequenceMatcher(None, w_ph, phonetic(tn)).ratio())
        extra = max(0, len(window_norm) - len(tn))
        score -= 0.5 * extra / max(1, len(window_norm))
        if game and tn == norm(game):
            score += 0.04
        if score > best_score:
            best, best_score = term, score
    return (best, best_score) if best_score >= threshold else (None, 0.0)

"""Tareas de lenguaje de la fase 2: funciones puras sobre un cliente LLM.

Cada tarea recibe un objeto `llm` que solo necesita el método (duck typing; este módulo
no importa ningún cliente concreto):

    llm.chat(messages: list[dict], *, json_schema: dict | None = None, max_tokens: int = 512,
             temperature: float = 0.2, timeout: float = 180.0) -> str

Las excepciones del cliente no se capturan: se propagan al llamador.

Tareas:
- classify_note: clasifica una nota (función, tema, ámbito, referencias, confianza).
- transform_note: corrige, mejora o convierte una nota en idea de guion.
- summarize_session: resumen de sesión en varias pasadas (map-reduce) con validación
  de los timestamps contra las notas reales.
"""
from __future__ import annotations

import json
import logging
import math
import re
from typing import Any

from cronista.context.engine import FUNCTIONS, SCOPES, TOPICS
from cronista.notes.manager import MARKERS
from cronista.session.clock import fmt_timestamp

log = logging.getLogger(__name__)

SCOPE_VALUES = ("current", "other", "general", "personal")
REF_RELATIONS = ("comparación", "referencia", "mención")
RECENT_MAX = 3
EXAMPLES_MAX = 6

PRINCIPIOS = (
    "Contexto: la app es una libreta para un creador de videoensayos de videojuegos. Cada nota es algo que el "
    "creador dijo mientras jugaba y grababa. Hay dos dimensiones distintas: la FUNCIÓN (qué hace con sus palabras) "
    "y el TEMA (de qué habla). Una descripción no es información poco importante: describir lo que ocurre es una "
    "nota valiosa para el video. El ÁMBITO indica sobre qué habla: el juego actual, otro videojuego, un tema general "
    "de la industria o un pensamiento personal. Una comparación como «esto me recuerda a RE4» es una comparación "
    "sobre el juego actual con una referencia externa, no una nota sobre RE4.\n"
    "Reglas: no inventes nunca datos, opiniones ni hechos del juego. Si no está claro, dilo: marca la clasificación "
    "como ambigua y con confianza baja. No cambies la opinión del usuario ni añadas información que no esté en sus "
    "palabras. Responde en español."
)

FUNCTION_DEFS = {
    "descripción": "describe lo que se ve, ocurre o hay en pantalla, sin valorarlo.",
    "análisis": "explica por qué algo funciona o cómo está hecho (mecánica, intención, causa).",
    "opinión": "expresa lo que el creador piensa o siente, de forma personal.",
    "crítica": "señala un defecto o problema concreto.",
    "comparación": "relaciona algo del juego actual con otro juego, obra o referencia externa.",
    "pensamiento": "reflexiona, duda o se pregunta sin llegar a una conclusión cerrada.",
    "observación": "señala un detalle concreto que llama la atención, sin valorarlo ni explicarlo.",
    "idea para video": "propone algo que podría convertirse en parte de un videoensayo o guion.",
}

SCOPE_DEFS = {
    "current": "habla del videojuego que se está jugando.",
    "other": "habla de otro videojuego concreto.",
    "general": "habla de la industria, de un género o de los juegos en general.",
    "personal": "habla de experiencias o pensamientos personales del creador (recuerdos, infancia).",
}

_CONTEXT_LABELS = (("topic", "tema"), ("subtopic", "subtema"), ("chapter", "capítulo"), ("section", "sección"))


# ---- utilidades -----------------------------------------------------------------
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"```[A-Za-z]*")


def _balanced_end(text: str, start: int) -> int:
    """Índice de la llave que cierra la de `start` (respetando cadenas), o -1."""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def parse_json(text: str) -> dict:
    """Extrae el primer objeto JSON válido de una respuesta del modelo.

    Quita bloques <think>…</think> y vallas ``` antes de buscar. Lanza ValueError si no hay
    ningún objeto JSON válido.
    """
    if not isinstance(text, str):
        raise ValueError("la respuesta no es texto")
    cleaned = _THINK_RE.sub("", text)
    close = cleaned.lower().rfind("</think>")
    if close != -1:
        cleaned = cleaned[close + len("</think>"):]
    cleaned = _FENCE_RE.sub("", cleaned)
    start = cleaned.find("{")
    while start != -1:
        end = _balanced_end(cleaned, start)
        if end != -1:
            try:
                obj = json.loads(cleaned[start:end + 1])
            except ValueError:
                obj = None
            if isinstance(obj, dict):
                return obj
        start = cleaned.find("{", start + 1)
    raise ValueError("no se encontró un objeto JSON válido en la respuesta")


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit].strip()


def _short_label(value: Any, max_words: int = 5, max_chars: int = 48) -> str:
    """Etiqueta corta: si el modelo devuelve una frase, se queda con sus primeras palabras."""
    words = _text(value, 200).strip(" .,;:").split()
    if not words:
        return ""
    label = " ".join(words[:max_words])
    return label[:max_chars].rstrip(" ,;:")


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"true", "sí", "si", "1", "yes"}
    return bool(value)


def _confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return 0.0
    return round(min(1.0, max(0.0, float(value))), 2)


def _enum_list(value: Any, allowed: list[str] | tuple[str, ...], max_items: int) -> list[str]:
    """Filtra a valores del enum (minúsculas, sin duplicados, máximo `max_items`)."""
    out: list[str] = []
    for item in _as_list(value):
        if not isinstance(item, str):
            continue
        key = item.strip().lower()
        if key in allowed and key not in out:
            out.append(key)
            if len(out) >= max_items:
                break
    return out


def _note_text(n: dict) -> str:
    return str(n.get("text") or n.get("text_raw") or "").strip()


def _join(value: Any) -> str:
    items = [str(v) for v in _as_list(value) if v]
    return " / ".join(items) if items else "—"


# ---- clasificación --------------------------------------------------------------
CLASSIFY_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "scope": {"type": "string", "enum": list(SCOPE_VALUES)},
        "functions": {"type": "array", "maxItems": 2, "items": {"type": "string", "enum": list(FUNCTIONS)}},
        "topics": {"type": "array", "maxItems": 3, "items": {"type": "string", "enum": list(TOPICS)}},
        "subtopic": {"type": "string"},
        "refs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "relation": {"type": "string", "enum": list(REF_RELATIONS)},
                },
                "required": ["name", "relation"],
            },
        },
        "chapter": {"type": "string"},
        "section": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "ambiguous": {"type": "boolean"},
        "rationale": {"type": "string"},
    },
    "required": ["scope", "functions", "topics", "subtopic", "refs", "chapter", "section", "confidence",
                 "ambiguous", "rationale"],
}


def _classify_system() -> str:
    funcs = "\n".join(f"- {k}: {v}" for k, v in FUNCTION_DEFS.items())
    scopes = "\n".join(f"- {k}: {v}" for k, v in SCOPE_DEFS.items())
    return (
        f"{PRINCIPIOS}\n\n"
        "Tarea: clasificar una nota hablada durante una sesión de juego.\n\n"
        f"Funciones (elige como máximo 2):\n{funcs}\n\n"
        f"Ámbito (elige uno):\n{scopes}\n\n"
        f"Temas permitidos (elige como máximo 3): {', '.join(TOPICS)}. Si la nota no trata de ningún aspecto "
        "del juego (un recuerdo personal, una divagación), deja topics vacío en lugar de forzar un tema.\n\n"
        "Responde solo con JSON que cumpla el esquema. Campos: scope, functions, topics, subtopic (etiqueta corta "
        "de 1 a 4 palabras que concreta el tema, p. ej. «sistema de cobertura» o «aparición de enemigos»; nunca una "
        "frase; vacío si no aplica), refs (juegos u obras externas mencionados, con name y relation), chapter "
        "(si la nota menciona un capítulo, acto o misión, con esa misma palabra y el número en cifra, p. ej. "
        "«Capítulo 3», «Misión 5», «Acto 2»; si no, vacío), section "
        "(lugar del juego mencionado, p. ej. «Fábrica abandonada»; si no, vacío), confidence (0 a 1), ambiguous "
        "(true si no está claro) y rationale (una frase corta)."
    )


def _classify_user(text: str, game: str, recent: list | None, examples: list | None,
                   prev_state: dict | None) -> str:
    parts = [f"Juego actual: {game or 'desconocido'}"]
    ctx = []
    for key, label in _CONTEXT_LABELS:
        value = str((prev_state or {}).get(key) or "").strip()
        if value:
            ctx.append(f"{label}: {value}")
    parts.append("Estado de contexto previo: " + ("; ".join(ctx) if ctx else "ninguno"))

    recent_texts = [t for t in (_note_text(n) for n in list(recent or [])[-RECENT_MAX:]) if t]
    if recent_texts:
        parts.append("Notas recientes (solo contexto; no las clasifiques):\n"
                     + "\n".join(f"- {t}" for t in recent_texts))

    ex_lines = []
    for ex in list(examples or [])[:EXAMPLES_MAX]:
        if not isinstance(ex, dict):
            continue
        ex_text = _text(ex.get("text"), 200)
        if not ex_text:
            continue
        ex_lines.append(f'- "{ex_text}" → ámbito: {ex.get("scope") or "desconocido"}; '
                        f'funciones: {_join(ex.get("functions"))}; temas: {_join(ex.get("topics"))}')
    if ex_lines:
        parts.append("Así clasificó el usuario notas parecidas (ejemplos corregidos):\n" + "\n".join(ex_lines))

    parts.append('Nota a clasificar:\n"""' + text + '"""')
    return "\n\n".join(parts)


def _normalize_classification(data: dict, game: str) -> dict:
    game_key = (game or "").strip().casefold()

    scope = data.get("scope").strip().lower() if isinstance(data.get("scope"), str) else ""
    ambiguous = _as_bool(data.get("ambiguous", False))
    if scope not in SCOPE_VALUES:
        scope = "current"
        ambiguous = True

    functions = _enum_list(data.get("functions"), FUNCTIONS, 2)
    topics = _enum_list(data.get("topics"), TOPICS, 3)

    refs: list[dict] = []
    seen: set[str] = set()
    for r in _as_list(data.get("refs")):
        if not isinstance(r, dict):
            continue
        name = _text(r.get("name"), 80)
        key = name.casefold()
        if not name or key == game_key or key in seen:
            continue
        seen.add(key)
        relation = r.get("relation").strip().lower() if isinstance(r.get("relation"), str) else ""
        if relation not in REF_RELATIONS:
            relation = "mención"
        refs.append({"name": name, "relation": relation})

    confidence = _confidence(data.get("confidence"))
    ambiguous = ambiguous or confidence < 0.5 or (not functions and not topics)

    return {
        "scope": scope,
        "functions": functions,
        "topics": topics,
        "subtopic": _short_label(data.get("subtopic")),
        "refs": refs,
        "chapter": _text(data.get("chapter"), 120),
        "section": _text(data.get("section"), 120),
        "confidence": confidence,
        "ambiguous": ambiguous,
        "rationale": _text(data.get("rationale"), 300),
        "source": "llm",
    }


def classify_note(llm, text: str, *, game: str = "", recent: list[dict] | None = None,
                  examples: list[dict] | None = None, prev_state: dict | None = None) -> dict:
    """Clasifica una nota. `recent` son notas anteriores (se usan las 3 últimas como contexto),
    `examples` son clasificaciones corregidas por el usuario y `prev_state` el estado contextual previo."""
    messages = [
        {"role": "system", "content": _classify_system()},
        {"role": "user", "content": _classify_user(text.strip(), game, recent, examples, prev_state)},
    ]
    raw = llm.chat(messages, json_schema=CLASSIFY_SCHEMA, max_tokens=350, temperature=0.1)
    return _normalize_classification(parse_json(raw), game)


# ---- transformación de notas ----------------------------------------------------
TRANSFORM_MODES: dict[str, dict[str, str]] = {
    "correct": {
        "title": "Corregir",
        "instructions": (
            "Corrige solo la ortografía, las tildes, la puntuación, las palabras mal reconocidas por la transcripción "
            "automática (que suenan parecido a otra palabra correcta en ese contexto) y las repeticiones accidentales. "
            "Conserva las mismas palabras, las muletillas («como que», «pues»), el registro coloquial y el orden de las "
            "ideas. No reformules ni mejores el estilo. Cuidado con palabras que cambian de sentido con la tilde "
            "(se/sé, si/sí, mas/más, el/él, tu/tú): elige según el sentido de la frase; «no sé» es «no lo sé». "
            "Los títulos de videojuegos, nombres propios y términos que el autor dice en inglés deben quedar en su "
            "grafía inglesa correcta (p. ej. «Rise of Iron: D-Day», «gameplay», «boss»), no españolizados. "
            "Si una palabra no se puede corregir con seguridad, déjala tal cual."
        ),
    },
    "improve": {
        "title": "Mejorar expresión",
        "instructions": (
            "El usuario dudó al hablar (por ejemplo «no sé cómo explicarlo…»). Reescribe en primera persona y con "
            "frases claras LA MISMA idea que intentaba expresar. Reglas de fidelidad: conserva su postura exacta "
            "(si critica algo, sigue siendo una crítica de eso mismo; si elogia, sigue siendo un elogio; si duda, "
            "conserva la duda con «creo que»); nunca afirmes lo contrario de lo que dijo ni cambies de qué habla; "
            "no añadas argumentos, datos ni ejemplos nuevos. Sí puedes nombrar con precisión el concepto que está "
            "describiendo (p. ej. «ritmo», «linealidad», «diseño de niveles») cuando se deduce claramente de sus "
            "palabras. Si la nota no contiene una idea clara, devuelve el texto casi igual, solo mejor puntuado.\n"
            "Ejemplo — nota: «los jefes como que no sé, pegan muy fuerte pero luego es como que siempre hacen lo "
            "mismo y ya te lo sabes» → «Creo que los jefes golpean muy fuerte, pero sus patrones se repiten tanto "
            "que acaban siendo predecibles.»"
        ),
    },
    "script": {
        "title": "Idea para guion",
        "instructions": (
            "Convierte la nota en una idea de guion para un videoensayo: de 1 a 3 frases, en español, escritas como "
            "un apunte del propio autor (p. ej. «Explicar cómo…», «Mostrar que…», «Plantear por qué…»), nunca "
            "hablando de «el creador» o «el usuario» en tercera persona. Sé fiel a lo dicho: no añadas hechos del "
            "juego ni opiniones nuevas."
        ),
    },
}

# temperatura y longitud máxima relativa a la original (+80 caracteres de margen)
_TRANSFORM_PARAMS = {"correct": (0.1, 1.6), "improve": (0.3, 3.0), "script": (0.3, 3.0)}

TRANSFORM_SCHEMA: dict = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
}


def transform_note(llm, text: str, mode: str, *, game: str = "") -> str:
    """Aplica un modo de TRANSFORM_MODES a la nota. Lanza ValueError si el modo no existe
    o si la respuesta está vacía o es desproporcionadamente larga."""
    if mode not in TRANSFORM_MODES:
        raise ValueError(f"modo desconocido: {mode!r}")
    spec = TRANSFORM_MODES[mode]
    temperature, max_ratio = _TRANSFORM_PARAMS[mode]
    source = text.strip()
    messages = [
        {"role": "system", "content": (
            f"{PRINCIPIOS}\n\nTarea: {spec['title']}. {spec['instructions']}\n"
            'Devuelve solo JSON con la clave "text".')},
        {"role": "user", "content": f'Juego actual: {game or "desconocido"}\nNota:\n"""{source}"""'},
    ]
    raw = llm.chat(messages, json_schema=TRANSFORM_SCHEMA, max_tokens=400, temperature=temperature)
    result = _text(parse_json(raw).get("text"), 10_000_000)
    if not result or len(result) > max_ratio * len(source) + 80:
        log.warning("Respuesta no fiable en modo %s (entrada %d caracteres, salida %d)",
                    mode, len(source), len(result))
        raise ValueError("respuesta no fiable")
    return result


# ---- resumen de sesión (map-reduce) ---------------------------------------------
_SUMMARY_ITEM: dict = {
    "type": "object",
    "properties": {
        "texto": {"type": "string"},
        "timestamps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["texto", "timestamps"],
}

SUMMARY_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "criticas": {"type": "array", "items": _SUMMARY_ITEM},
        "positivos": {"type": "array", "items": _SUMMARY_ITEM},
        "temas_recurrentes": {"type": "array", "items": {"type": "string"}},
        "ideas_video": {"type": "array", "items": _SUMMARY_ITEM},
        "desarrollar": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"timestamp": {"type": "string"}, "texto": {"type": "string"}},
                "required": ["timestamp", "texto"],
            },
        },
    },
    "required": ["criticas", "positivos", "temas_recurrentes", "ideas_video", "desarrollar"],
}

_SUMMARY_LIST_KEYS = ("criticas", "positivos", "ideas_video")


def format_notes_for_llm(notes: list[dict]) -> list[str]:
    """Una línea por nota: `[hh:mm:ss] texto (función: …; tema: …; ámbito: …; marcador: …)`."""
    out = []
    for n in notes:
        text = " ".join(_note_text(n).split())
        meta = []
        functions = [str(f) for f in _as_list(n.get("functions")) if f]
        if functions:
            meta.append("función: " + " / ".join(functions))
        topics = [str(t) for t in _as_list(n.get("topics")) if t]
        if topics:
            meta.append("tema: " + " / ".join(topics))
        scope = n.get("scope") or ""
        if scope and scope != "current":
            meta.append("ámbito: " + SCOPES.get(scope, scope))
        marker = n.get("marker") or ""
        if marker:
            meta.append("marcador: " + MARKERS.get(marker, marker))
        line = f"[{fmt_timestamp(n.get('position_ms'))}] {text}"
        if meta:
            line += " (" + "; ".join(meta) + ")"
        out.append(line)
    return out


def chunk_lines(lines: list[str], max_chars: int = 6000) -> list[list[str]]:
    """Agrupa líneas en trozos de como máximo `max_chars` caracteres. Nunca devuelve trozos vacíos;
    una línea más larga que `max_chars` va sola en su trozo."""
    chunks: list[list[str]] = []
    cur: list[str] = []
    cur_len = 0
    for line in lines:
        if not line.strip():
            continue
        cost = len(line) + 1
        if cur and cur_len + cost > max_chars:
            chunks.append(cur)
            cur, cur_len = [], 0
        cur.append(line)
        cur_len += cost
    if cur:
        chunks.append(cur)
    return chunks


def _empty_summary(notes_count: int = 0) -> dict:
    return {"criticas": [], "positivos": [], "temas_recurrentes": [], "ideas_video": [], "desarrollar": [],
            "generated_by": "llm", "notes_count": notes_count}


def _summary_system() -> str:
    return (
        f"{PRINCIPIOS}\n\n"
        "Tarea: resumir una sesión de juego a partir de sus notas. Reglas estrictas: basa tu resumen "
        "exclusivamente en las notas recibidas; cita solo timestamps que aparezcan en ellas, copiados tal cual "
        "(formato hh:mm:ss); no inventes opiniones, hechos ni timestamps. Si una nota trata de otro juego o de un "
        "pensamiento personal, no la presentes como crítica del juego actual. En ideas_video incluye solo notas "
        "donde el autor propone material para un video o plantea una pregunta con potencial claro de análisis; "
        "no incluyas recuerdos personales ni simples descripciones.\n"
        "Responde solo con JSON que cumpla el esquema: criticas, positivos e ideas_video (cada elemento con texto y "
        "timestamps), temas_recurrentes (etiquetas de 1 a 4 palabras que agrupan varias notas, p. ej. «diseño de "
        "niveles», «sistema de cobertura», «ritmo»; nunca frases ni citas) y desarrollar (comentarios que merecen "
        "desarrollarse, con "
        "timestamp y texto). Si una sección no tiene nada, devuelve una lista vacía."
    )


def _clean_ts(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().strip("[]()").strip()


def _valid_timestamps(notes: list[dict]) -> set[str]:
    return {fmt_timestamp(n.get("position_ms")) for n in notes if n.get("position_ms") is not None}


def _validate_summary(data: dict, notes: list[dict]) -> dict:
    """Elimina timestamps inventados y elementos que se queden sin timestamp válido."""
    valid = _valid_timestamps(notes)

    def items(key: str) -> list[dict]:
        out = []
        for item in _as_list(data.get(key)):
            if not isinstance(item, dict):
                continue
            texto = _text(item.get("texto"), 500)
            stamps: list[str] = []
            for ts in _as_list(item.get("timestamps")):
                c = _clean_ts(ts)
                if c in valid and c not in stamps:
                    stamps.append(c)
            if texto and stamps:
                out.append({"texto": texto, "timestamps": stamps})
        return out

    temas: list[str] = []
    for t in _as_list(data.get("temas_recurrentes")):
        t = _text(t, 120).strip(" .")
        # un tema es una etiqueta; las frases largas (citas de notas) no son temas
        if t and len(t.split()) <= 6 and t.lower() not in (x.lower() for x in temas):
            temas.append(t)

    desarrollar = []
    for item in _as_list(data.get("desarrollar")):
        if not isinstance(item, dict):
            continue
        ts = _clean_ts(item.get("timestamp"))
        texto = _text(item.get("texto"), 500)
        if ts in valid and texto:
            desarrollar.append({"timestamp": ts, "texto": texto})

    return {
        "criticas": items("criticas"),
        "positivos": items("positivos"),
        "temas_recurrentes": temas,
        "ideas_video": items("ideas_video"),
        "desarrollar": desarrollar,
        "generated_by": "llm",
        "notes_count": len(notes),
    }


_SUMMARY_KEYS = ("criticas", "positivos", "temas_recurrentes", "ideas_video", "desarrollar")


def _for_llm(summary: dict) -> dict:
    """Solo las claves del esquema (sin metadatos internos) para reenviar al modelo."""
    return {k: summary.get(k, []) for k in _SUMMARY_KEYS}


def summarize_session(llm, session: dict, notes: list[dict], *, max_chars: int = 6000) -> dict:
    """Resumen de sesión. Pasada 'map' por trozos de notas y, si hay varios trozos, una llamada
    final de consolidación. El resultado se valida contra los timestamps reales de las notas."""
    notes = list(notes or [])
    if not notes:
        return _empty_summary(0)
    session = session or {}
    game = str(session.get("game") or "").strip() or "desconocido"

    chunks = chunk_lines(format_notes_for_llm(notes), max_chars)
    partials: list[dict] = []
    for i, chunk in enumerate(chunks, 1):
        messages = [
            {"role": "system", "content": _summary_system()},
            {"role": "user", "content": (
                f"Juego: {game}\n"
                f"Notas de la sesión (parte {i} de {len(chunks)}), formato [hh:mm:ss] texto (metadatos):\n"
                + "\n".join(chunk))},
        ]
        raw = llm.chat(messages, json_schema=SUMMARY_SCHEMA, max_tokens=900, temperature=0.2)
        # validar cada parcial antes de fusionar: los timestamps inventados no llegan a la consolidación
        partials.append(_validate_summary(parse_json(raw), notes))

    # Reducción jerárquica: se consolidan grupos que quepan en el contexto del modelo local hasta que
    # quede uno solo (con una sola llamada final, una sesión de varias horas desbordaría el contexto).
    while len(partials) > 1:
        groups: list[list[dict]] = [[]]
        size = 0
        for p in partials:
            n = len(json.dumps(_for_llm(p), ensure_ascii=False))
            if groups[-1] and size + n > max_chars:
                groups.append([])
                size = 0
            groups[-1].append(p)
            size += n
        if len(groups) == len(partials):      # ningún par cabe junto: fusionar de dos en dos igualmente
            groups = [partials[i:i + 2] for i in range(0, len(partials), 2)]
        merged_round: list[dict] = []
        for group in groups:
            if len(group) == 1:
                merged_round.append(group[0])
                continue
            messages = [
                {"role": "system", "content": _summary_system()},
                {"role": "user", "content": (
                    f"Juego: {game}\n"
                    "A continuación hay resúmenes parciales de trozos consecutivos de la misma sesión, en JSON. "
                    "Fusiónalos en un único resumen con el mismo esquema: une duplicados, conserva los timestamps "
                    "que aparezcan en los parciales y no añadas nada que no esté en ellos.\n"
                    + json.dumps([_for_llm(g) for g in group], ensure_ascii=False))},
            ]
            raw = llm.chat(messages, json_schema=SUMMARY_SCHEMA, max_tokens=900, temperature=0.2)
            merged_round.append(_validate_summary(parse_json(raw), notes))
        partials = merged_round

    return _validate_summary(partials[0], notes)


def _md_list(summary: dict, key: str, fmt) -> list[str]:
    items = _as_list(summary.get(key))
    if not items:
        return ["*Nada destacado.*"]
    return [f"{i}. {fmt(it)}" for i, it in enumerate(items, 1)]


def summary_to_markdown(summary: dict, session: dict) -> str:
    """Resumen en Markdown con timestamps entre corchetes."""
    game = str((session or {}).get("game") or "").strip() or "Sesión"
    summary = summary or {}

    def with_stamps(it: dict) -> str:
        stamps = " ".join(f"[{ts}]" for ts in _as_list(it.get("timestamps")))
        return f"{it.get('texto', '')} {stamps}".rstrip()

    def stamped(it: dict) -> str:
        return f"[{it.get('timestamp', '')}] {it.get('texto', '')}"

    sections = [
        ("Principales críticas", _md_list(summary, "criticas", with_stamps)),
        ("Aspectos positivos", _md_list(summary, "positivos", with_stamps)),
        ("Temas recurrentes", _md_list(summary, "temas_recurrentes", str)),
        ("Posibles ideas para video", _md_list(summary, "ideas_video", with_stamps)),
        ("Comentarios que debería desarrollar", _md_list(summary, "desarrollar", stamped)),
    ]
    out = [f"# {game} — resumen de la sesión", ""]
    for title, lines in sections:
        out += [f"## {title}", ""] + lines + [""]
    return "\n".join(out)

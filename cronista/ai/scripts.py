"""Generación de esqueletos de guion a partir de tus notas (una o varias sesiones de un juego).

Principio: el guion se construye SOLO con lo que dijiste. Cada nota se presenta al modelo con una
referencia corta (N1, N2…) y cada punto del guion debe citar al menos una nota real. Las referencias
inventadas se eliminan y los puntos sin respaldo se descartan; lo que falte para sostener el argumento
se devuelve como "preguntas abiertas" para ti, en lugar de rellenarlo.

Con muchas notas (contexto del modelo local limitado) se hace en dos pasos: extracción de puntos clave
por bloques y, después, estructuración del guion con esos puntos.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any

from cronista.ai.tasks import PRINCIPIOS, parse_json
from cronista.session.clock import fmt_timestamp

log = logging.getLogger(__name__)

LENGTHS = {"corto": ("5-8 minutos", 3), "medio": ("10-15 minutos", 5), "largo": ("20 minutos o más", 7)}
APPROACHES = {
    "análisis": "videoensayo de análisis: explicar por qué funciona o no funciona el juego",
    "crítica": "crítica/reseña con veredicto argumentado",
    "comparación": "comparación con otros juegos que menciona el autor",
    "ensayo": "ensayo temático alrededor de una idea central",
    "ludonarrativa": "análisis de la coherencia entre lo que el juego te hace hacer y lo que cuenta (disonancia ludonarrativa)",
}

_REFS = {"type": "array", "items": {"type": "string"}}
_POINT = {"type": "object", "properties": {"texto": {"type": "string"}, "refs": _REFS}, "required": ["texto", "refs"]}
POINTS_SCHEMA = {
    "type": "object",
    "properties": {"puntos": {"type": "array", "items": {
        "type": "object", "properties": {"tema": {"type": "string"}, "texto": {"type": "string"}, "refs": _REFS},
        "required": ["tema", "texto", "refs"]}}},
    "required": ["puntos"],
}
SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "titulos": {"type": "array", "items": {"type": "string"}},
        "gancho": _POINT,
        "tesis": _POINT,
        "secciones": {"type": "array", "items": {
            "type": "object",
            "properties": {"titulo": {"type": "string"}, "objetivo": {"type": "string"},
                           "puntos": {"type": "array", "items": _POINT}, "clips": _REFS},
            "required": ["titulo", "objetivo", "puntos", "clips"]}},
        "conclusion": _POINT,
        "preguntas": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["titulos", "gancho", "tesis", "secciones", "conclusion", "preguntas"],
}

_SYSTEM = (
    f"{PRINCIPIOS}\n\n"
    "Tarea: ayudar a estructurar el guion de un video a partir de las notas habladas del autor. Reglas estrictas:\n"
    "- Usa SOLO ideas que estén en las notas. Cada punto debe citar en refs las notas en que se apoya (N1, N2…).\n"
    "- No inventes hechos del juego, datos, opiniones ni ejemplos que el autor no dijo. No cambies sus opiniones.\n"
    "- Redacta los puntos como apuntes del propio autor para su guion, en primera persona o como instrucciones "
    "(«Explicar…», «Mostrar…»), en español natural.\n"
    "- Si para sostener el argumento falta algo que el autor no dijo, no lo rellenes: añádelo a preguntas. Las "
    "preguntas son para el AUTOR sobre evidencia o detalles que le faltan (p. ej. «¿Qué misión exacta era?», «¿Hay "
    "otro momento que lo demuestre?», «¿Con qué juego lo comparas?»), nunca preguntas genéricas de diseño.\n"
    "- En clips, pon las referencias de notas cuyo momento de la grabación serviría como imagen para esa sección.\n"
)


# ---- preparación ---------------------------------------------------------------------------
def prepare_notes(notes: list[dict], *, only_marked: bool = False, include_other_voice: bool = False) -> list[dict]:
    """Filtra y ordena las notas; asigna referencias N1… y etiquetas de sesión S1…"""
    sessions: dict[int, str] = {}
    out = []
    for n in sorted(notes, key=lambda x: (x.get("session_started") or "", x.get("position_ms") or 0)):
        if not (n.get("text") or "").strip():
            continue
        if only_marked and not n.get("marker"):
            continue
        voice = ((n.get("stt_info") or {}).get("voice") or {})
        if not include_other_voice and voice.get("match") is False and not voice.get("confirmed"):
            continue
        sid = n.get("session_id")
        if sid not in sessions:
            sessions[sid] = f"S{len(sessions) + 1}"
        out.append(dict(n, ref=f"N{len(out) + 1}", session_ref=sessions[sid]))
    return out


def _line(n: dict) -> str:
    tags = []
    if n.get("functions"):
        tags.append("/".join(n["functions"]))
    if n.get("topics"):
        tags.append("/".join(n["topics"]))
    if n.get("scope") and n["scope"] != "current":
        tags.append({"other": "otro juego", "general": "tema general", "personal": "personal"}.get(n["scope"], ""))
    if n.get("marker"):
        tags.append({"important": "importante", "video_idea": "idea para video", "use_in_video": "usar en video",
                     "develop": "desarrollar", "problem": "problema", "liked": "me gustó"}.get(n["marker"], ""))
    meta = f" ({'; '.join(t for t in tags if t)})" if tags else ""
    return f"{n['ref']} [{n['session_ref']} {fmt_timestamp(n.get('position_ms'))}] {n['text'].strip()}{meta}"


def _chunks(lines: list[str], max_chars: int) -> list[list[str]]:
    out, cur, size = [], [], 0
    for ln in lines:
        if cur and size + len(ln) > max_chars:
            out.append(cur)
            cur, size = [], 0
        cur.append(ln)
        size += len(ln) + 1
    if cur:
        out.append(cur)
    return out


# ---- validación anti-invención ---------------------------------------------------------------
def _clean_refs(refs: Any, valid: set[str]) -> list[str]:
    out = []
    for r in refs if isinstance(refs, list) else []:
        r = str(r).strip().strip("[]").upper()
        if r in valid and r not in out:
            out.append(r)
    return out


def _clean_point(p: Any, valid: set[str], require_refs: bool = True) -> dict | None:
    if not isinstance(p, dict):
        return None
    text = str(p.get("texto") or "").strip()
    refs = _clean_refs(p.get("refs"), valid)
    if not text or (require_refs and not refs):
        return None
    return {"texto": text[:600], "refs": refs}


def validate_script(data: dict, valid: set[str]) -> dict:
    sections = []
    for s in data.get("secciones") or []:
        if not isinstance(s, dict):
            continue
        points = [p for p in (_clean_point(x, valid) for x in s.get("puntos") or []) if p]
        if not points:
            continue                       # una sección sin ninguna nota que la respalde no se queda
        title = re.sub(r"^\s*(?:secci[oó]n\s*)?\d+\s*[.):-]\s*", "", str(s.get("titulo") or ""), flags=re.I)
        sections.append({"titulo": title.strip()[:120] or "Sección",
                         "objetivo": str(s.get("objetivo") or "").strip()[:300],
                         "puntos": points, "clips": _clean_refs(s.get("clips"), valid)})
    titles = [str(t).strip()[:120] for t in data.get("titulos") or [] if str(t).strip()][:3]
    questions = [str(q).strip()[:300] for q in data.get("preguntas") or [] if str(q).strip()][:8]
    return {
        "titulos": titles,
        # gancho, tesis y conclusión resumen lo dicho: pueden no citar, pero si citan, solo notas reales
        "gancho": _clean_point(data.get("gancho"), valid, require_refs=False),
        "tesis": _clean_point(data.get("tesis"), valid, require_refs=False),
        "secciones": sections,
        "conclusion": _clean_point(data.get("conclusion"), valid, require_refs=False),
        "preguntas": questions,
    }


# ---- generación ------------------------------------------------------------------------------
def generate_script(llm, game: str, notes: list[dict], *, length: str = "medio", approach: str = "análisis",
                    extra: str = "", max_chars: int = 5500, progress=None) -> dict:
    """notes: ya preparadas con prepare_notes (tienen ref y session_ref). Devuelve el guion validado."""
    progress = progress or (lambda f, t="": None)
    if not notes:
        raise ValueError("No hay notas para construir el guion.")
    dur_label, n_sections = LENGTHS.get(length, LENGTHS["medio"])
    approach_text = APPROACHES.get(approach, APPROACHES["análisis"])
    valid = {n["ref"] for n in notes}
    lines = [_line(n) for n in notes]
    chunks = _chunks(lines, max_chars)
    if len(chunks) == 1:
        material = "Notas del autor:\n" + "\n".join(lines)
    else:
        points: list[dict] = []
        for i, ch in enumerate(chunks, 1):
            progress(0.8 * (i - 1) / len(chunks), f"Leyendo notas ({i}/{len(chunks)})…")
            raw = llm.chat([{"role": "system", "content": _SYSTEM},
                            {"role": "user", "content": (
                                f"Juego: {game}\nExtrae los puntos clave de estas notas para un guion ({approach_text}). "
                                "Agrupa por tema; cada punto con las refs de sus notas.\n" + "\n".join(ch))}],
                           json_schema=POINTS_SCHEMA, max_tokens=1200, temperature=0.2)
            for p in parse_json(raw).get("puntos") or []:
                cp = _clean_point(p, valid)
                if cp:
                    points.append({"tema": str(p.get("tema") or "")[:60], **cp})
        material = "Puntos clave extraídos de las notas del autor (con sus refs):\n" + "\n".join(
            f"- [{p['tema']}] {p['texto']} (refs: {', '.join(p['refs'])})" for p in points)
    progress(0.85, "Estructurando el guion…")
    user = (f"Juego: {game}\nTipo de video: {approach_text}. Duración objetivo: {dur_label} "
            f"(unas {n_sections} secciones).\n"
            + (f"Indicaciones del autor: {extra.strip()}\n" if extra.strip() else "")
            + f"{material}\n\nDevuelve el esqueleto del guion en JSON: titulos (hasta 3 propuestas), gancho, tesis, "
              "secciones (titulo, objetivo, puntos con refs, clips), conclusion y preguntas.")
    raw = llm.chat([{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
                   json_schema=SCRIPT_SCHEMA, max_tokens=2500, temperature=0.3)
    script = validate_script(parse_json(raw), valid)
    script["meta"] = {"game": game, "length": length, "approach": approach, "notes": len(notes),
                      "generated_at": datetime.now().isoformat(timespec="seconds")}
    progress(1.0, "Listo")
    return script


# ---- presentación ----------------------------------------------------------------------------
def ref_index(notes: list[dict]) -> dict[str, dict]:
    return {n["ref"]: n for n in notes}


def _cite(refs: list[str], idx: dict[str, dict]) -> str:
    out = []
    for r in refs:
        n = idx.get(r)
        if n:
            date = (n.get("session_started") or "")[:10]
            date = f"{date[8:10]}/{date[5:7]}" if len(date) == 10 else n.get("session_ref", "")
            out.append(f"[{date} · {fmt_timestamp(n.get('position_ms'))}]")
    return " ".join(out)


def script_to_markdown(script: dict, notes: list[dict]) -> str:
    idx = ref_index(notes)
    meta = script.get("meta", {})
    out = [f"# {script['titulos'][0] if script.get('titulos') else meta.get('game', 'Guion')}", ""]
    if len(script.get("titulos") or []) > 1:
        out += ["**Otros títulos posibles:** " + " · ".join(script["titulos"][1:]), ""]
    out.append(f"*{meta.get('game', '')} · {APPROACHES.get(meta.get('approach', ''), '')} · "
               f"{LENGTHS.get(meta.get('length', ''), ('',))[0]} · basado en {meta.get('notes', 0)} notas tuyas*")
    out.append("")
    for key, label in (("gancho", "Gancho"), ("tesis", "Idea central")):
        p = script.get(key)
        if p:
            out += [f"## {label}", "", f"{p['texto']} {_cite(p['refs'], idx)}".strip(), ""]
    for i, s in enumerate(script.get("secciones") or [], 1):
        out += [f"## {i}. {s['titulo']}", ""]
        if s.get("objetivo"):
            out += [f"*{s['objetivo']}*", ""]
        for p in s["puntos"]:
            out.append(f"- {p['texto']} {_cite(p['refs'], idx)}".rstrip())
        if s.get("clips"):
            out += ["", "🎬 Imágenes: " + " ".join(_cite([r], idx) for r in s["clips"])]
        out.append("")
    p = script.get("conclusion")
    if p:
        out += ["## Conclusión", "", f"{p['texto']} {_cite(p['refs'], idx)}".strip(), ""]
    if script.get("preguntas"):
        out += ["## Preguntas abiertas (lo que falta para sostener el argumento)", ""]
        out += [f"- {q}" for q in script["preguntas"]]
        out.append("")
    return "\n".join(out)


def script_record(script: dict, notes: list[dict], session_ids: list[int], provider: str, params: dict) -> dict:
    """Registro listo para store.save_script (incluye el mapa de referencias para poder citarlas después)."""
    content = dict(script, refs={n["ref"]: {"note_id": n["id"], "session_id": n.get("session_id"),
                                            "position_ms": n.get("position_ms")} for n in notes})
    return {"game": script.get("meta", {}).get("game", ""),
            "title": (script.get("titulos") or ["Guion"])[0],
            "session_ids": session_ids, "params": params, "content": content,
            "markdown": script_to_markdown(script, notes), "provider": provider}

"""Exportación de sesiones: TXT, Markdown, CSV, JSON, SRT y "paquete para chatbot".

El paquete para chatbot es un texto en Markdown con instrucciones y todas las notas
con timestamp, pensado para pegarlo en ChatGPT, Claude, Gemini, etc. (o enviarlo por
API con un proveedor configurado). No se envía nada automáticamente.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from pathlib import Path

from cronista.notes.manager import MARKERS
from cronista.session.clock import fmt_srt, fmt_timestamp

FORMATS = {"md": "Markdown (.md)", "txt": "Texto (.txt)", "csv": "CSV (.csv)", "json": "JSON (.json)",
           "srt": "Subtítulos (.srt)"}

SCOPE_LABEL = {"current": "Actual", "other": "Otro videojuego", "general": "Tema general",
               "personal": "Pensamiento personal", "": "—"}


def _note_text(n: dict) -> str:
    return (n.get("text") or n.get("text_raw") or "").strip()


def _meta_line(n: dict) -> str:
    parts = []
    if n.get("functions"):
        parts.append("Función: " + " / ".join(n["functions"]))
    if n.get("topics"):
        parts.append("Tema: " + " / ".join(n["topics"]))
    if n.get("scope"):
        parts.append("Videojuego: " + SCOPE_LABEL.get(n["scope"], n["scope"]))
    if n.get("refs"):
        parts.append("Referencias: " + ", ".join(r.get("name", "") if isinstance(r, dict) else str(r) for r in n["refs"]))
    if n.get("marker"):
        parts.append("Importancia: " + MARKERS.get(n["marker"], n["marker"]))
    if n.get("confidence"):
        parts.append(f"Confianza: {int(float(n['confidence']) * 100)}%")
    return " · ".join(parts)


def to_markdown(session: dict, notes: list[dict]) -> str:
    out = [f"# {session.get('game', '')} — {session.get('title', '')}", ""]
    out.append(f"Duración: {fmt_timestamp(session.get('duration_ms'))}  ")
    if session.get("obs_path"):
        out.append(f"Archivo: `{session['obs_path']}`  ")
    out.append(f"Notas: {len(notes)}")
    out.append("")
    for n in notes:
        out.append(f"## [{fmt_timestamp(n.get('position_ms'))}]")
        out.append("")
        out.append(_note_text(n))
        meta = _meta_line(n)
        if meta:
            out.append("")
            out.append(f"*{meta}*")
        for key, label in (("text_corrected", "Corregida"), ("text_improved", "Mejor expresada"),
                           ("text_script", "Idea para guion")):
            if n.get(key) and n.get(key) != _note_text(n):
                out.append("")
                out.append(f"> **{label}:** {n[key]}")
        out.append("")
    summary = session.get("summary") or {}
    analysis = (summary.get("local") or summary.get("api") or {}) if isinstance(summary, dict) else {}
    if analysis.get("markdown"):
        out += ["---", "", "# Análisis de la sesión (IA, basado solo en estas notas)", ""]
        # el resumen trae su propio título "# …": lo bajamos un nivel para que encaje
        out += [("#" + ln if ln.startswith("#") else ln) for ln in analysis["markdown"].splitlines()]
    return "\n".join(out)


def to_txt(session: dict, notes: list[dict]) -> str:
    out = [f"{session.get('game', '')} — {session.get('title', '')}", f"Duración: {fmt_timestamp(session.get('duration_ms'))}"]
    if session.get("obs_path"):
        out.append(f"Archivo: {session['obs_path']}")
    out.append("")
    for n in notes:
        out.append(f"[{fmt_timestamp(n.get('position_ms'))}] {_note_text(n)}")
        meta = _meta_line(n)
        if meta:
            out.append(f"    ({meta})")
        out.append("")
    return "\n".join(out)


def to_csv(session: dict, notes: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["timestamp", "position_ms", "text", "functions", "topics", "scope", "references", "marker",
                "confidence", "source", "game", "session"])
    for n in notes:
        w.writerow([fmt_timestamp(n.get("position_ms")), n.get("position_ms"), _note_text(n),
                    "; ".join(n.get("functions") or []), "; ".join(n.get("topics") or []),
                    SCOPE_LABEL.get(n.get("scope", ""), n.get("scope", "")),
                    "; ".join(r.get("name", "") if isinstance(r, dict) else str(r) for r in n.get("refs") or []),
                    n.get("marker", ""), n.get("confidence", 0), n.get("source", ""), session.get("game", ""),
                    session.get("title", "")])
    return buf.getvalue()


def to_json(session: dict, notes: list[dict]) -> str:
    return json.dumps({"session": session, "notes": notes, "exported_at": datetime.now().isoformat(timespec="seconds"),
                       "app": "Cronista"}, ensure_ascii=False, indent=2)


def to_srt(session: dict, notes: list[dict]) -> str:
    out = []
    for i, n in enumerate(notes, 1):
        start = int(n.get("position_ms") or 0)
        end = start + max(2000, int(n.get("duration_ms") or 0))
        out += [str(i), f"{fmt_srt(start)} --> {fmt_srt(end)}", _note_text(n), ""]
    return "\n".join(out)


def render(fmt: str, session: dict, notes: list[dict]) -> str:
    return {"md": to_markdown, "txt": to_txt, "csv": to_csv, "json": to_json, "srt": to_srt}[fmt](session, notes)


def export_file(fmt: str, session: dict, notes: list[dict], path: Path) -> Path:
    path.write_text(render(fmt, session, notes), encoding="utf-8")
    return path


def default_filename(session: dict, fmt: str) -> str:
    safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in session.get("game", "sesion")).strip() or "sesion"
    started = (session.get("started_at") or "")[:16].replace(":", "-").replace("T", "_")
    return f"{safe}_{started}.{fmt}"


# ---- paquete para chatbot -----------------------------------------------------
CHATBOT_GOALS = {
    "guion": ("Esqueleto de guion",
              "A partir de mis notas, propón un esqueleto de guion para un video-ensayo: secciones, argumento "
              "central de cada una y qué notas (con timestamp) usaría como evidencia. No inventes opiniones que no "
              "estén en mis notas; si falta algo, señálalo como pregunta para mí."),
    "ludonarrativa": ("Disonancia ludonarrativa",
                      "Analiza mis notas buscando posibles disonancias ludonarrativas: momentos donde lo que el juego "
                      "me pide hacer choca con lo que la historia o los personajes dicen. Cita los timestamps y "
                      "explica el choque. Distingue claramente entre lo que yo observé y tus hipótesis."),
    "dialogos": ("Errores de diálogo y coherencia",
                 "Revisa mis notas sobre diálogos, personajes e historia y señala posibles errores de coherencia, "
                 "frases sin sentido, contradicciones o decisiones de personajes que no cuadran. Usa los timestamps "
                 "para que pueda revisarlos en la grabación."),
    "resumen": ("Resumen de la sesión",
                "Resume la sesión: principales críticas, aspectos positivos, temas recurrentes, posibles ideas para "
                "video y comentarios que debería desarrollar. Básate exclusivamente en mis notas y cita timestamps."),
    "libre": ("Solo las notas", ""),
}


def build_chatbot_prompt(session: dict, notes: list[dict], goal: str = "guion", extra: str = "") -> str:
    title, instr = CHATBOT_GOALS.get(goal, CHATBOT_GOALS["libre"])
    head = [
        f"Contexto: estas son mis notas habladas mientras jugaba y grababa **{session.get('game', '')}** "
        f"(sesión del {(session.get('started_at') or '')[:10]}, duración {fmt_timestamp(session.get('duration_ms'))}). "
        "Cada nota tiene el timestamp exacto de la grabación, mi texto transcrito y una clasificación aproximada "
        "(función del comentario, tema, si habla del juego actual u otro).",
        "",
    ]
    if instr:
        head += [f"Tarea: {instr}", ""]
    if extra.strip():
        head += [f"Indicaciones adicionales: {extra.strip()}", ""]
    head += ["Reglas: no inventes hechos del juego ni opiniones mías; cuando especules, dilo. Responde en español.", "",
             "## Notas", ""]
    body = []
    for n in notes:
        line = f"- [{fmt_timestamp(n.get('position_ms'))}] {_note_text(n)}"
        tags = []
        if n.get("functions"):
            tags.append("/".join(n["functions"]))
        if n.get("topics"):
            tags.append("/".join(n["topics"]))
        if n.get("scope") and n["scope"] != "current":
            tags.append(SCOPE_LABEL.get(n["scope"], n["scope"]))
        if n.get("marker"):
            tags.append(MARKERS.get(n["marker"], n["marker"]))
        if tags:
            line += f"  _({'; '.join(tags)})_"
        body.append(line)
    return "\n".join(head + body)


def split_prompt(text: str, max_chars: int = 12000) -> list[str]:
    """Divide el paquete en trozos (por líneas) para chatbots con límite de entrada."""
    if len(text) <= max_chars:
        return [text]
    chunks, cur = [], ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > max_chars and cur:
            chunks.append(cur)
            cur = ""
        cur += line
    if cur:
        chunks.append(cur)
    n = len(chunks)
    return [f"(Parte {i + 1} de {n}. Espera a tener todas las partes antes de responder.)\n\n{c}" for i, c in enumerate(chunks)]

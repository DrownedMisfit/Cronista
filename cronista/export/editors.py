"""Exportación de marcadores para editores estándar (DaVinci Resolve, etc.).

- EDL CMX3600 con marcadores: Resolve los importa como marcadores de color (Timeline > Import > Timeline markers from EDL).
- Marcas de tiempo para pegar en la descripción de un video de YouTube o en un guion.
"""
from __future__ import annotations

from cronista.session.clock import fmt_timestamp

EDL_TEXT_MAX = 120
EDL_START_OFFSET_S = 3600          # como en Resolve, la línea de tiempo empieza en 01:00:00:00

# Color de marcador de Resolve según la importancia de la nota
RESOLVE_COLORS = {"important": "Red", "video_idea": "Orange", "use_in_video": "Green", "develop": "Blue",
                  "problem": "Purple", "liked": "Cyan", "": "Yellow"}


def _one_line(text: str) -> str:
    return " ".join((text or "").split()).replace("|", "/")


def _note_text(n: dict) -> str:
    return (n.get("text") or n.get("text_raw") or "").strip()


def _timecode(frames: int, base: int) -> str:
    """Frames absolutos -> hh:mm:ss:ff (no drop-frame), con base de fotogramas entera."""
    ff = frames % base
    secs = frames // base
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}:{ff:02d}"


def to_marker_edl(session: dict, notes: list[dict], fps: float = 30.0) -> str:
    """EDL CMX3600 con un marcador por nota (las notas sin posición se omiten)."""
    fps = fps if fps and fps > 0 else 30.0
    base = max(1, round(fps))
    import unicodedata
    title = session.get("game") or "Sesion"
    # TITLE en ASCII simple: CMX3600 es un formato antiguo y algunos editores rechazan otros caracteres
    title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii").strip() or "Sesion"
    out = [f"TITLE: {title} - Cronista\nFCM: NON-DROP FRAME\n\n"]
    num = 0
    for n in notes:
        pos = n.get("position_ms")
        if pos is None:
            continue
        num += 1
        frames = round(float(pos) * fps / 1000.0) + EDL_START_OFFSET_S * base
        tc_in = _timecode(frames, base)
        tc_out = _timecode(frames + 1, base)
        color = RESOLVE_COLORS.get(n.get("marker") or "", "Yellow")
        text = _one_line(_note_text(n))[:EDL_TEXT_MAX]
        out.append(f"{num:03d}  {num:03d}      V     C        {tc_in} {tc_out} {tc_in} {tc_out}  \n"
                   f" |C:ResolveColor{color} |M:{text} |D:1\n")
    return "".join(out)


def to_youtube_timestamps(notes: list[dict]) -> str:
    """Una línea por nota: «hh:mm:ss texto» (o «mm:ss texto» si la grabación dura menos de una hora)."""
    lines = []
    for n in notes:
        pos = n.get("position_ms")
        if pos is None:
            continue
        stamp = fmt_timestamp(pos)
        if stamp.startswith("00:"):
            stamp = stamp[3:]
        lines.append(f"{stamp} {_one_line(_note_text(n))}".rstrip())
    return "\n".join(lines) + ("\n" if lines else "")

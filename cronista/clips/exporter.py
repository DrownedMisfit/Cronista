"""Recorte de clips desde la grabación de OBS y vínculo nota↔clip (fase 3). Sin IA.

Cada nota tiene una posición en la grabación (position_ms). El clip cubre desde `clip_pre_s` antes de la
nota hasta `clip_post_s` después de su final. El corte se hace con FFmpeg: "copy" (rápido, puede empezar
unos segundos antes por el keyframe) o "exact" (recodifica con la GPU AMD y cae a libx264 si no funciona).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from cronista.core.ffmpeg import ffmpeg, popen, probe
from cronista.session.clock import fmt_timestamp

if TYPE_CHECKING:
    from cronista.core.settings import Settings
    from cronista.notes.store import Store

log = logging.getLogger(__name__)

HW_ENCODER = "h264_amf"       # codificador H.264 de la GPU AMD (modo exact); si falla se usa libx264
TOL_MS = 200                  # tolerancia para reconocer un clip ya existente
_INVALID = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

Progress = Callable[[float, str], None]


# ---- tramos ------------------------------------------------------------------
def clip_range(note: dict, duration_s: float, pre_s: float, post_s: float) -> tuple[int, int]:
    """Inicio y fin (ms) del clip de una nota, acotados a la grabación."""
    pos = note.get("position_ms")
    if pos is None:
        raise ValueError("La nota no tiene posición en la grabación")
    pos = int(pos)
    dur = int(note.get("duration_ms") or 0)
    total = int(round(duration_s * 1000))
    start = max(0, pos - int(round(pre_s * 1000)))
    end = min(total, pos + dur + int(round(post_s * 1000)))
    if end <= start:
        raise ValueError("El tramo de la nota queda fuera de la grabación")
    return start, end


def merge_ranges(ranges: list[tuple[int, int]], gap_ms: int = 1500) -> list[tuple[int, int, list[int]]]:
    """Une tramos solapados o separados por menos de `gap_ms`. Devuelve (inicio, fin, índices originales)."""
    order = sorted(range(len(ranges)), key=lambda i: ranges[i][0])
    out: list[tuple[int, int, list[int]]] = []
    for i in order:
        s, e = int(ranges[i][0]), int(ranges[i][1])
        if out and s - out[-1][1] < gap_ms:
            ps, pe, idx = out[-1]
            out[-1] = (ps, max(pe, e), idx + [i])
        else:
            out.append((s, e, [i]))
    return out


# ---- nombres y carpetas ------------------------------------------------------------
def safe_name(text: str, max_len: int = 48) -> str:
    """Nombre seguro para Windows: sin \\ / : * ? " < > |, espacios simples. Nunca vacío."""
    s = _INVALID.sub(" ", text or "")
    s = re.sub(r"\s+", " ", s).strip().rstrip(".")
    s = s[:max_len].rstrip(" .")
    return s or "nota"


def clip_filename(session: dict, note: dict, start_ms: int, mode: str = "copy") -> str:
    """'<hh-mm-ss> <primeras palabras>.<ext>'. Con modo exact un .mkv se guarda como .mp4."""
    ts = fmt_timestamp(start_ms).replace(":", "-")
    words = " ".join((note.get("text") or note.get("text_raw") or "").split()[:6])
    name = safe_name(words, 48)
    ext = Path(session.get("obs_path") or "").suffix.lower() or ".mp4"
    if mode == "exact" and ext == ".mkv":
        ext = ".mp4"
    return f"{ts} {name}{ext}"


def _session_day(started_at: str | None) -> str:
    try:
        return datetime.fromisoformat(started_at or "").strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return datetime.now().strftime("%Y-%m-%d")


def default_clip_dir(session: dict, settings: "Settings") -> Path:
    """Carpeta de clips: la configurada, o junto a la grabación: 'Clips Cronista/<juego> <fecha>'."""
    custom = (settings.editor.clip_dir or "").strip()
    if custom:
        return Path(custom)
    obs = session.get("obs_path")
    base = Path(obs).parent if obs else Path.home() / "Videos"
    game = safe_name((session.get("game") or "").strip() or "Sesion", 40)
    return base / "Clips Cronista" / f"{game} {_session_day(session.get('started_at'))}"


# ---- corte ---------------------------------------------------------------------
def _span_progress(progress: Progress, base: float, span: float, text: str) -> Callable[[float], None]:
    """Convierte el progreso de un clip (0-1) en progreso global dentro de [base, base+span]."""
    def report(f: float) -> None:
        progress(base + f * span, text)
    return report


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        log.warning("No se pudo borrar el clip incompleto %s", path)


def _run_ffmpeg(args: list[str], dur_s: float, progress: Callable[[float], None],
                cancelled: Callable[[], bool]) -> tuple[str, str]:
    """Ejecuta ffmpeg con progreso (-progress pipe:1). Devuelve ('ok' | 'error' | 'cancelled', detalle)."""
    proc = popen(args)
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            if cancelled():
                break
            if line.startswith("out_time_us="):
                try:
                    t = int(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue
                progress(min(1.0, max(0.0, t / dur_s)))
        if cancelled():
            proc.kill()
        rc = proc.wait()
        err = proc.stderr.read() if proc.stderr else ""
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    if cancelled():
        return "cancelled", ""
    if rc != 0:
        return "error", "\n".join(err.strip().splitlines()[-6:]) or f"código {rc}"
    return "ok", ""


def cut_clip(src: str | Path, dst: str | Path, start_ms: int, end_ms: int, mode: str,
             progress: Callable[[float], None] | None = None,
             cancelled: Callable[[], bool] | None = None) -> bool:
    """Recorta [start_ms, end_ms) de `src` a `dst`. Devuelve False si se canceló (el archivo a medias se borra).

    Errores → RuntimeError con las últimas líneas de stderr.
    """
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dur_s = max(0.001, (end_ms - start_ms) / 1000)
    ss, dur = f"{start_ms / 1000:.3f}", f"{dur_s:.3f}"
    progress = progress or (lambda f: None)
    cancelled = cancelled or (lambda: False)
    head = [ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error", "-nostats", "-y"]
    tail = ["-progress", "pipe:1", str(dst)]
    audio = ["-c:a", "aac", "-b:a", "192k"]

    if mode == "copy":
        attempts = [head + ["-ss", ss, "-i", str(src), "-t", dur, "-map", "0", "-c", "copy",
                            "-avoid_negative_ts", "make_zero"] + tail]
    else:
        common = head + ["-ss", ss, "-i", str(src), "-t", dur, "-map", "0:v:0", "-map", "0:a?"]
        attempts = [
            common + ["-c:v", HW_ENCODER, "-quality", "balanced", "-rc", "cqp", "-qp_i", "20", "-qp_p", "22"]
            + audio + tail,
            common + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"] + audio + tail,
        ]

    last_err = ""
    for i, args in enumerate(attempts):
        if cancelled():
            _remove(dst)
            return False
        status, err = _run_ffmpeg(args, dur_s, progress, cancelled)
        if status == "ok":
            return True
        if status == "cancelled":
            _remove(dst)
            return False
        last_err = err
        if i + 1 < len(attempts):
            log.warning("El codificador %s falló; se reintenta con libx264: %s", HW_ENCODER, err)
    _remove(dst)
    raise RuntimeError(f"ffmpeg no pudo recortar el clip: {last_err}")


# ---- exportación de las notas de una sesión ------------------------------------------
def _find_existing(store: "Store", note_ids: list[int], start: int, end: int) -> dict | None:
    for nid in note_ids:
        for c in store.clips_for_note(nid):
            if (abs(c["start_ms"] - start) <= TOL_MS and abs(c["end_ms"] - end) <= TOL_MS
                    and Path(c["path"]).is_file()):
                return c
    return None


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    n = 2
    while True:
        cand = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not cand.exists():
            return cand
        n += 1


def _clip_info(path: str, start: int, end: int, note_ids: list[int], mode: str, reused: bool) -> dict:
    return {"path": path, "start_ms": start, "end_ms": end, "note_ids": note_ids, "mode": mode, "reused": reused}


def export_note_clips(store: "Store", session_id: int, settings: "Settings", note_ids: list[int] | None = None,
                      only_marked: bool = False, merge: bool = False, progress: Progress | None = None,
                      cancelled: Callable[[], bool] | None = None) -> dict:
    """Recorta el tramo de cada nota y registra el vínculo nota↔clip en la base de datos.

    Notas sin posición o fuera de la grabación se omiten (contadas en "omitted").
    Con `merge`, las notas con tramos próximos comparten un solo clip (se registra para cada una).
    Clips ya existentes (mismo tramo ±200 ms y archivo presente) no se vuelven a cortar.
    """
    progress = progress or (lambda f, t="": None)
    cancelled = cancelled or (lambda: False)

    session = store.get_session(session_id)
    if session is None:
        raise ValueError("La sesión no existe")
    obs = session.get("obs_path")
    if not obs:
        raise ValueError("La sesión no tiene grabación de OBS asociada")
    if not Path(obs).is_file():
        raise ValueError(f"No se encuentra la grabación de OBS: {obs}")

    notes = store.list_notes(session_id)
    if note_ids is not None:
        wanted = set(note_ids)
        notes = [n for n in notes if n["id"] in wanted]
    if only_marked:
        notes = [n for n in notes if n.get("marker")]
    if not notes:
        return {"message": "No hay notas que exportar", "folder": "", "clips": [], "skipped": 0, "omitted": 0}

    progress(0.0, "Analizando la grabación")
    duration_s = float(probe(str(obs))["duration"])
    if duration_s <= 0:
        raise RuntimeError("No se pudo leer la duración de la grabación")

    mode = "exact" if settings.editor.clip_mode == "exact" else "copy"
    ranged: list[tuple[dict, int, int]] = []
    omitted = 0
    for n in notes:
        try:
            s, e = clip_range(n, duration_s, settings.editor.clip_pre_s, settings.editor.clip_post_s)
        except ValueError:
            omitted += 1
            continue
        ranged.append((n, s, e))

    if merge and ranged:
        targets = [(gs, ge, [ranged[i][0] for i in idxs])
                   for gs, ge, idxs in merge_ranges([(s, e) for _, s, e in ranged])]
    else:
        targets = [(s, e, [n]) for n, s, e in ranged]

    folder = default_clip_dir(session, settings)
    folder.mkdir(parents=True, exist_ok=True)
    total = len(targets)
    out: list[dict] = []
    skipped = 0

    for i, (s, e, group) in enumerate(targets):
        if cancelled():
            break
        ids = [n["id"] for n in group]
        existing = _find_existing(store, ids, s, e)
        if existing:
            for nid in ids:
                if not any(c["path"] == existing["path"] for c in store.clips_for_note(nid)):
                    store.add_clip(nid, session_id, existing["path"], existing["start_ms"], existing["end_ms"],
                                   existing["mode"])
            skipped += 1
            out.append(_clip_info(existing["path"], existing["start_ms"], existing["end_ms"], ids,
                                  existing["mode"], True))
            progress((i + 1) / total, f"Ya existía: {Path(existing['path']).name}")
            continue

        dst = _unique_path(folder / clip_filename(session, group[0], s, mode=mode))
        text = f"Recortando {i + 1}/{total}: {dst.name}"
        progress(i / total, text)
        ok = cut_clip(obs, dst, s, e, mode, progress=_span_progress(progress, i / total, 1 / total, text),
                      cancelled=cancelled)
        if not ok:
            break
        for nid in ids:
            store.add_clip(nid, session_id, str(dst), s, e, mode)
        out.append(_clip_info(str(dst), s, e, ids, mode, False))

    progress(1.0, "Listo")
    msg = f"{len(out)} clips en {folder}"
    if skipped:
        msg += f" ({skipped} ya existían)"
    if omitted:
        msg += f"; {omitted} notas sin posición o fuera de la grabación"
    return {"message": msg, "folder": str(folder), "clips": out, "skipped": skipped, "omitted": omitted}

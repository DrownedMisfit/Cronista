"""Integración con Reclaimer, el editor de video del usuario (proyecto aparte en F:\\Reclaimer).

Cronista no importa el paquete `editoria` de Reclaimer. Solo carga, de forma aislada, su archivo de
modelo (`editoria/core/model.py`: dataclasses puras, sin Qt) y genera con sus propias clases un
proyecto `.eia` que Reclaimer abre tal cual. Nunca escribe dentro de la carpeta de Reclaimer.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, Callable

from cronista.core import ffmpeg
from cronista.core.paths import exports_dir

if TYPE_CHECKING:
    from cronista.core.settings import Settings
    from cronista.notes.store import Store

log = logging.getLogger(__name__)

RECLAIMER_DEFAULT_DIRS = (Path(r"F:\Reclaimer"), Path(r"D:\EditorIA"))
MODEL_REL = Path("editoria") / "core" / "model.py"
MODULE_NAME = "reclaimer_model"
AV_TRACK_NAME = "Audio de video"          # mismo nombre que usa Reclaimer para el sonido de un video
MOMENT_COLOR = "#8b93a3"

MARKER_ICONS = {"important": "⭐", "video_idea": "🔥", "use_in_video": "🎬", "develop": "📝",
                "problem": "❌", "liked": "👍"}
MARKER_COLORS = {"important": "#e5413c", "video_idea": "#f28c28", "use_in_video": "#3fb950",
                 "develop": "#58a6ff", "problem": "#b54ae0", "liked": "#2ec4b6", "": "#f2b33d"}

_model_cache: dict[str, ModuleType] = {}


# ---- localizar y cargar Reclaimer --------------------------------------------------
def _is_reclaimer_root(root: Path) -> bool:
    return (root / MODEL_REL).is_file()


def find_reclaimer(settings: Settings | None) -> Path | None:
    """Carpeta raíz de Reclaimer: la configurada, o F:\\Reclaimer / D:\\EditorIA. None si no hay ninguna."""
    candidates: list[Path] = []
    configured = (settings.editor.reclaimer_dir or "").strip() if settings else ""
    if configured:
        candidates.append(Path(configured))
    candidates.extend(RECLAIMER_DEFAULT_DIRS)
    for c in candidates:
        if _is_reclaimer_root(c):
            return c
    return None


def load_model_module(root: Path) -> ModuleType:
    """Importa editoria/core/model.py de `root` sin tocar sys.path ni importar el paquete `editoria`.

    No escribe __pycache__ dentro de Reclaimer (se desactiva la caché de bytecode solo durante la carga).
    """
    model_file = (Path(root) / MODEL_REL).resolve()
    key = str(model_file)
    cached = _model_cache.get(key)
    if cached is not None:
        return cached
    if not model_file.is_file():
        raise RuntimeError(f"No se encontró el modelo de Reclaimer en {model_file}")
    spec = importlib.util.spec_from_file_location(MODULE_NAME, model_file)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"No se pudo preparar la carga de {model_file}")
    mod = importlib.util.module_from_spec(spec)
    # dataclasses busca el módulo en sys.modules al definir las clases: debe estar registrado.
    sys.modules[MODULE_NAME] = mod
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(MODULE_NAME, None)
        raise
    finally:
        sys.dont_write_bytecode = previous
    _model_cache[key] = mod
    return mod


# ---- texto de las notas ---------------------------------------------------------------
def _note_text(note: dict) -> str:
    return " ".join(((note.get("text") or note.get("text_raw") or "").split()))


def note_label(note: dict) -> str:
    """Etiqueta corta de una nota para marcadores de Reclaimer: «⭐ texto (función · tema)»."""
    text = _note_text(note) or "(sin texto)"
    if len(text) > 90:
        text = text[:89].rstrip() + "…"
    label = f"{MARKER_ICONS.get(note.get('marker') or '', '🎙')} {text}"
    meta = " · ".join(part for part in (
        " / ".join(note.get("functions") or []),
        " / ".join(note.get("topics") or []),
    ) if part)
    if meta:
        label += f" ({meta})"
    info = note.get("stt_info") if isinstance(note.get("stt_info"), dict) else {}
    voice = info.get("voice") if isinstance(info.get("voice"), dict) else {}
    if voice.get("match") is False:
        label += " 🗣️"
    return label


# ---- construir el proyecto ------------------------------------------------------------
def _session_date(session: dict) -> datetime:
    try:
        return datetime.fromisoformat(session.get("started_at") or "")
    except ValueError:
        return datetime.now()


def _track_index_after_video(tracks: list[Any], kinds: tuple[str, ...]) -> int:
    """Índice donde insertar una pista nueva justo debajo de las de video/texto (como Reclaimer)."""
    return max((i for i, t in enumerate(tracks) if t.kind in kinds), default=-1)


def build_project(session: dict, notes: list[dict], *, settings: Settings, moments: list[dict] | None = None,
                  root: Path | None = None, media_info: dict | None = None) -> dict:
    """Devuelve el dict de un proyecto de Reclaimer (listo para guardar como .eia)."""
    reclaimer_root = Path(root) if root else find_reclaimer(settings)
    if reclaimer_root is None:
        raise RuntimeError("No se encontró Reclaimer. Instálalo en F:\\Reclaimer o indica su carpeta en los ajustes del editor.")
    M = load_model_module(reclaimer_root)

    raw_path = session.get("obs_path") or ""
    if not raw_path or not os.path.isfile(raw_path):
        raise ValueError("La sesión no tiene archivo de grabación o el archivo ya no existe.")
    path = os.path.abspath(raw_path)
    info = media_info or ffmpeg.probe(path)
    duration = float(info.get("duration") or 0.0)
    if duration <= 0:
        raise ValueError("No se pudo leer la duración de la grabación.")

    game = session.get("game") or ""
    p = M.Project.new()
    p.name = f"{game or 'Sesión'} — Cronista {_session_date(session):%d/%m/%Y}"
    p.format = "youtube"
    p.width, p.height = 1920, 1080
    if float(info.get("fps") or 0) > 0:
        p.fps = round(float(info["fps"]), 3)

    has_audio = bool(info.get("has_audio"))
    item = M.MediaItem(
        id=M.new_id(), path=path, kind=M.KIND_VIDEO, name=os.path.basename(path), category="Grabaciones",
        tags=["cronista"] + ([game] if game else []), duration=duration,
        width=int(info.get("width") or 0), height=int(info.get("height") or 0), fps=float(info.get("fps") or 0),
        has_audio=has_audio, channels=int(info.get("channels") or 0), probed=True)
    p.media[item.id] = item

    v1 = next((t for t in p.tracks if t.kind == M.TRACK_VIDEO and t.name == "V1"), None)
    if v1 is None:
        v1 = next((t for t in p.tracks if t.kind == M.TRACK_VIDEO), None)
    if v1 is None:
        raise RuntimeError("El proyecto de Reclaimer no tiene pistas de video.")
    link = M.new_id() if has_audio else ""
    clip_note = "Grabación de OBS (Cronista)"
    vc = M.Clip(M.new_id(), item.id, v1.id, 0.0, 0.0, duration, origin=M.ORIGIN_USER, note=clip_note, link=link)
    p.clips[vc.id] = vc

    if has_audio:
        av = M.Track(M.new_id(), M.TRACK_AUDIO, AV_TRACK_NAME)
        p.tracks.insert(_track_index_after_video(p.tracks, (M.TRACK_VIDEO, M.TRACK_TEXT)) + 1, av)
        ac = M.Clip(M.new_id(), item.id, av.id, 0.0, 0.0, duration, origin=M.ORIGIN_USER, note=clip_note, link=link)
        p.clips[ac.id] = ac

    editor = settings.editor
    if editor.marker_notes:
        for n in notes:
            if n.get("position_ms") is None:
                continue
            t = float(n["position_ms"]) / 1000.0
            dur_ms = int(n.get("duration_ms") or 0)
            marker = M.Marker(M.new_id(), time=t, label=note_label(n),
                              color=MARKER_COLORS.get(n.get("marker") or "", MARKER_COLORS[""]),
                              origin=M.ORIGIN_AI, kind="marker")
            if dur_ms > 1500:
                marker.end = t + dur_ms / 1000.0
            p.markers.append(marker)

    if editor.favorites_notes:
        for n in notes:
            if n.get("position_ms") is None:
                continue
            t = float(n["position_ms"]) / 1000.0
            dur_note = int(n.get("duration_ms") or 0) / 1000.0
            start = round(max(0.0, t - editor.clip_pre_s), 2)
            end = round(min(duration, t + dur_note + editor.clip_post_s), 2)
            if any(f.get("media_id") == item.id and abs(f["start"] - start) < 0.05 for f in p.favorites):
                continue
            p.favorites.append({"media_id": item.id, "start": start, "end": end, "label": note_label(n)})

    if editor.include_moments and moments:
        for m in moments:
            # Un momento solo "comentado" ya está representado por los marcadores de sus notas: añadirlo
            # duplicaría información en la línea de tiempo. Se añaden los que aportan algo nuevo (acción
            # detectada en la imagen o el sonido de la grabación).
            if editor.marker_notes and m.get("kind") == "comentado":
                continue
            p.markers.append(M.Marker(
                M.new_id(), time=float(m["start_ms"]) / 1000.0, end=float(m["end_ms"]) / 1000.0,
                label="◆ " + (m.get("title") or "Momento"), color=MOMENT_COLOR, origin=M.ORIGIN_AI, kind="marker"))

    d = p.to_dict()
    M.Project.from_dict(d)   # validación de ida y vuelta con las propias clases de Reclaimer
    return d


def default_project_path(session: dict, settings: Settings) -> Path:
    """Dónde guardar el .eia: la carpeta de proyectos configurada, o la de la grabación."""
    folder_setting = (settings.editor.project_dir or "").strip()
    if folder_setting:
        folder = Path(folder_setting)
    elif session.get("obs_path"):
        folder = Path(session["obs_path"]).parent
    else:
        folder = exports_dir()
    game = "".join("_" if c in '<>:"/\\|?*' else c for c in (session.get("game") or "Sesión")).strip() or "Sesión"
    stamp = _session_date(session).strftime("%Y-%m-%d_%H%M")
    return folder / f"{game} - Cronista - {stamp}.eia"


def _unique_path(path: Path) -> Path:
    """No sobrescribe: si existe, añade « (2)», « (3)»…"""
    if not path.exists():
        return path
    n = 2
    while True:
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def export_session(store: Store, session_id: int, settings: Settings, path: Path | None = None,
                   progress: Callable[[str], None] | None = None) -> Path:
    """Genera el proyecto .eia de una sesión y lo guarda de forma atómica. Devuelve la ruta escrita."""
    def report(msg: str) -> None:
        log.info(msg)
        if progress:
            progress(msg)

    session = store.get_session(session_id)
    if session is None:
        raise ValueError(f"No existe la sesión {session_id}.")
    report("Leyendo notas y momentos…")
    notes = store.list_notes(session_id)
    moments = store.list_moments(session_id)
    report("Construyendo el proyecto de Reclaimer…")
    data = build_project(session, notes, settings=settings, moments=moments)

    target = _unique_path(Path(path) if path else default_project_path(session, settings))
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, target)
    report(f"Proyecto guardado: {target}")
    return target


def open_in_reclaimer(eia_path: Path, settings: Settings) -> None:
    """Abre un .eia con Reclaimer (igual que INICIAR.bat, sin ventana de consola)."""
    root = find_reclaimer(settings)
    if root is None:
        raise RuntimeError("No se encontró Reclaimer para abrir el proyecto.")
    eia = Path(eia_path)
    if not eia.is_file():
        raise FileNotFoundError(f"No existe el proyecto {eia}")
    flags = getattr(subprocess, "DETACHED_PROCESS", 0)
    pythonw = root / ".venv" / "Scripts" / "pythonw.exe"
    if pythonw.is_file():
        subprocess.Popen([str(pythonw), "main.py", str(eia)], cwd=str(root), creationflags=flags, close_fds=True)
    else:
        bat = root / "INICIAR.bat"
        if not bat.is_file():
            raise RuntimeError(f"Reclaimer no tiene entorno de Python ni INICIAR.bat en {root}")
        subprocess.Popen([str(bat), str(eia)], cwd=str(root), creationflags=flags, close_fds=True)
    log.info("Reclaimer abierto con %s", eia)

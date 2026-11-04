"""Abrir la grabación en un reproductor, saltando al timestamp cuando el reproductor lo permite.

Soporta mpv, VLC y PotPlayer (detectados en rutas habituales o indicados en la
configuración). Si no hay ninguno, abre el archivo con el programa predeterminado.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

_CANDIDATES = {
    "mpv": ["mpv.exe", r"C:\Program Files\mpv\mpv.exe", r"C:\Program Files (x86)\mpv\mpv.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\mpv\mpv.exe"), os.path.expandvars(r"%USERPROFILE%\scoop\shims\mpv.exe")],
    "vlc": ["vlc.exe", r"C:\Program Files\VideoLAN\VLC\vlc.exe", r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe"],
    "potplayer": [r"C:\Program Files\DAUM\PotPlayer\PotPlayerMini64.exe", r"C:\Program Files (x86)\DAUM\PotPlayer\PotPlayerMini.exe"],
}


def find_player(configured: str = "") -> tuple[str, str] | None:
    if configured:
        p = Path(configured)
        if p.exists():
            name = p.name.lower()
            kind = "mpv" if "mpv" in name else "vlc" if "vlc" in name else "potplayer" if "potplayer" in name else "generic"
            return kind, str(p)
    for kind, cands in _CANDIDATES.items():
        for c in cands:
            if os.path.sep not in c:
                w = shutil.which(c)
                if w:
                    return kind, w
            elif Path(c).exists():
                return kind, c
    return None


def open_at(path: str, position_ms: int | None, configured_player: str = "") -> str:
    """Devuelve una descripción de lo que se hizo (para mostrar en la UI)."""
    if not path or not Path(path).exists():
        raise FileNotFoundError(f"No se encuentra la grabación: {path}")
    secs = max(0, int((position_ms or 0) / 1000))
    hh, mm, ss = secs // 3600, (secs % 3600) // 60, secs % 60
    player = find_player(configured_player)
    if player is None or position_ms is None:
        os.startfile(path)  # type: ignore[attr-defined]
        return "Abierto con el reproductor predeterminado (sin salto al timestamp)."
    kind, exe = player
    if kind == "mpv":
        args = [exe, f"--start={secs}", path]
    elif kind == "vlc":
        args = [exe, f"--start-time={secs}", path]
    elif kind == "potplayer":
        args = [exe, path, f"/seek={hh:02d}:{mm:02d}:{ss:02d}"]
    else:
        args = [exe, path]
    subprocess.Popen(args, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    return f"Abierto con {kind} en {hh:02d}:{mm:02d}:{ss:02d}."

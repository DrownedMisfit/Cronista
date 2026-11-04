"""Localización y uso seguro de FFmpeg / FFprobe (instalados en el sistema, p. ej. con winget).

Todas las llamadas: sin ventana, stdin cerrado (-nostdin / DEVNULL, evita cuelgues), prioridad baja
(no competir con el juego ni con OBS) y tiempo máximo.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)


@lru_cache(maxsize=4)
def find_tool(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    packages = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    if packages.exists():
        for exe in packages.glob(f"*FFmpeg*/**/bin/{name}.exe"):
            return str(exe)
    for base in (r"C:\ffmpeg\bin", r"C:\Program Files\ffmpeg\bin"):
        p = Path(base) / f"{name}.exe"
        if p.is_file():
            return str(p)
    return None


def ffmpeg() -> str:
    exe = find_tool("ffmpeg")
    if not exe:
        raise FileNotFoundError("No se encontró FFmpeg. Instálalo (p. ej. «winget install Gyan.FFmpeg»).")
    return exe


def ffprobe() -> str:
    exe = find_tool("ffprobe")
    if not exe:
        raise FileNotFoundError("No se encontró FFprobe (viene con FFmpeg).")
    return exe


def run(args: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout, creationflags=_FLAGS)


def popen(args: list[str]) -> subprocess.Popen:
    """Proceso largo con salida de progreso por stdout (usar '-progress pipe:1')."""
    return subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=_FLAGS, text=True, encoding="utf-8", errors="replace")


def probe(path: str) -> dict:
    """Duración (s), resolución, fps, audio (canales, nº de pistas) de un archivo multimedia."""
    r = run([ffprobe(), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path], timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace").strip()[-300:] or "ffprobe falló")
    data = json.loads(r.stdout or b"{}")
    fmt, streams = data.get("format", {}), data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    audios = [s for s in streams if s.get("codec_type") == "audio"]

    def rate(text: str | None) -> float:
        try:
            a, b = (text or "0/1").split("/")
            return float(a) / float(b) if float(b) else 0.0
        except Exception:
            return 0.0

    duration = float(fmt.get("duration") or 0) or float((video or (audios[0] if audios else {})).get("duration") or 0)
    return {"duration": duration,
            "width": int(video.get("width", 0)) if video else 0,
            "height": int(video.get("height", 0)) if video else 0,
            "fps": round(rate(video.get("avg_frame_rate") or video.get("r_frame_rate")), 3) if video else 0.0,
            "has_audio": bool(audios), "channels": int(audios[0].get("channels", 0)) if audios else 0,
            "audio_tracks": len(audios), "video_codec": (video or {}).get("codec_name", "")}

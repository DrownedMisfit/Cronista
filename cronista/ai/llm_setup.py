"""Instalación de la IA local: runtime llama.cpp (Vulkan, Windows) y modelo GGUF.

Solo define funciones: no descarga nada al importarse. Las llaman la interfaz o `tools/download_llm.py`,
y cada descarga usa internet, así que debe ser una acción explícita del usuario.
"""
from __future__ import annotations

import logging
import shutil
import tempfile
import threading
import zipfile
from collections.abc import Callable
from pathlib import Path

import requests

from cronista.ai.local_llm import RUNTIME_EXE, find_model, find_runtime
from cronista.core.paths import llm_models_dir, llm_runtime_dir
from cronista.core.settings import LLMSettings, Settings

log = logging.getLogger(__name__)

LLAMA_RELEASES_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=15"
ASSET_SUFFIX = "-bin-win-vulkan-x64.zip"
MODEL_URL = "https://huggingface.co/unsloth/Qwen3.5-4B-GGUF/resolve/main/Qwen3.5-4B-Q4_K_M.gguf"
MODEL_FILE = "Qwen3.5-4B-Q4_K_M.gguf"
MODEL_SIZE_GB = 2.74
RUNTIME_SIZE_MB = 34
USER_AGENT = "Cronista"

Progress = Callable[[int, int], None]   # progress(bytes_hechos, total)  (total = 0 si se desconoce)


def latest_runtime_asset() -> tuple[str, str]:
    """(tag, url) del zip Vulkan para Windows de la release más reciente de llama.cpp."""
    r = requests.get(LLAMA_RELEASES_API, timeout=30,
                     headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT})
    r.raise_for_status()
    for rel in r.json():
        if rel.get("draft"):
            continue
        for asset in rel.get("assets", []):
            if asset.get("name", "").endswith(ASSET_SUFFIX):
                return rel.get("tag_name", ""), asset["browser_download_url"]
    raise RuntimeError(f"No encuentro ningún asset '*{ASSET_SUFFIX}' en las últimas releases de llama.cpp.")


_ACTIVE: set[str] = set()
_ACTIVE_LOCK = threading.Lock()


def download_file(url: str, dest: Path, progress: Progress | None = None, chunk: int = 1 << 20) -> Path:
    """Descarga `url` a `dest` con reanudación (fichero .part + cabecera Range). Renombra al terminar.
    Dos descargas simultáneas del mismo destino no se permiten (corromperían el .part)."""
    key = str(Path(dest).resolve()).lower()
    with _ACTIVE_LOCK:
        if key in _ACTIVE:
            raise RuntimeError(f"Ya hay una descarga en curso de {Path(dest).name}.")
        _ACTIVE.add(key)
    try:
        return _download_file(url, dest, progress, chunk)
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE.discard(key)


def _download_file(url: str, dest: Path, progress: Progress | None, chunk: int) -> Path:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    done = part.stat().st_size if part.exists() else 0

    for attempt in range(2):
        headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
        if done:
            headers["Range"] = f"bytes={done}-"
        with requests.get(url, headers=headers, stream=True, timeout=(15, 60)) as r:
            if r.status_code == 416 and done and attempt == 0:
                # El .part no encaja con el fichero remoto: empezar de cero.
                part.unlink(missing_ok=True)
                done = 0
                continue
            r.raise_for_status()
            if not (r.status_code == 206 and done > 0):
                done = 0                     # el servidor ignoró Range: se reescribe desde el principio
                mode = "wb"
            else:
                mode = "ab"
            length = int(r.headers.get("Content-Length") or 0)
            total = done + length if length else 0
            if progress:
                progress(done, total)
            with open(part, mode) as f:
                for block in r.iter_content(chunk_size=chunk):
                    if not block:
                        continue
                    f.write(block)
                    done += len(block)
                    if progress:
                        progress(done, total)
        break

    if total and done != total:
        raise RuntimeError(f"Descarga incompleta de {dest.name} ({done} de {total} bytes). "
                           "Vuelve a ejecutarla para reanudar.")
    part.replace(dest)
    log.info("Descargado %s (%d bytes)", dest.name, done)
    return dest


def install_runtime(progress: Progress | None = None) -> Path:
    """Descarga el zip de llama.cpp, extrae y copia todos sus ficheros (aplanados) a tools/llama. Devuelve llama-server.exe."""
    tag, url = latest_runtime_asset()
    log.info("Descargando llama.cpp %s", tag)
    destino = llm_runtime_dir()
    trabajo = Path(tempfile.mkdtemp(prefix="cronista_llama_"))
    try:
        zip_path = download_file(url, trabajo / "runtime.zip", progress)
        extraido = trabajo / "extraido"
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(extraido)
        for f in extraido.rglob("*"):
            if f.is_file():
                try:
                    shutil.copy2(f, destino / f.name)
                except PermissionError as e:
                    raise RuntimeError(f"No puedo reemplazar {f.name}: puede estar en uso. "
                                       "Cierra la IA local e inténtalo de nuevo.") from e
    finally:
        shutil.rmtree(trabajo, ignore_errors=True)

    exe = destino / RUNTIME_EXE
    if not exe.is_file():
        raise RuntimeError(f"El zip de llama.cpp {tag} no contiene {RUNTIME_EXE}.")
    return exe


def install_model(progress: Progress | None = None) -> Path:
    """Descarga el modelo a models/llm. Si ya existe completo (el .part solo se renombra al final), no hace nada."""
    destino = llm_models_dir() / MODEL_FILE
    if destino.is_file():
        return destino
    return download_file(MODEL_URL, destino, progress)


def status(llm_settings: LLMSettings | None = None) -> dict:
    """Rutas del runtime y del modelo tal como las vería el cliente (None si faltan)."""
    cfg = llm_settings or Settings.load().llm
    runtime = find_runtime(cfg)
    model = find_model(cfg)
    return {"runtime": str(runtime) if runtime else None, "model": str(model) if model else None}

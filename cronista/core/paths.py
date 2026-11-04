"""Rutas de datos de la aplicación (todo local, dentro del perfil del usuario)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from cronista import APP_NAME


def app_root() -> Path:
    """Carpeta del código (o del ejecutable empaquetado)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    base = Path(os.environ.get("APPDATA", Path.home()))
    d = base / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def models_dir() -> Path:
    override = os.environ.get("CRONISTA_MODELS")
    d = Path(override) if override else app_root() / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def llm_models_dir() -> Path:
    """Modelos GGUF para la IA local."""
    d = models_dir() / "llm"
    d.mkdir(parents=True, exist_ok=True)
    return d


def llm_runtime_dir() -> Path:
    """Binarios de llama.cpp (llama-server.exe y DLLs)."""
    d = app_root() / "tools" / "llama"
    d.mkdir(parents=True, exist_ok=True)
    return d


def logs_dir() -> Path:
    d = data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def exports_dir() -> Path:
    d = data_dir() / "exports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def settings_path() -> Path:
    return data_dir() / "settings.json"


def db_path() -> Path:
    return data_dir() / "cronista.db"


def teleprompter_text_path() -> Path:
    """Último texto del teleprompter (se guarda solo al escribir o abrir un archivo)."""
    return data_dir() / "teleprompter.txt"

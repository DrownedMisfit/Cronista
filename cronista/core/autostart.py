"""Cronista solo se ejecuta cuando el usuario lo abre a mano: no arranca con Windows ni vigila OBS.

Versiones anteriores podían registrar entradas en la clave Run del usuario ("Cronista" y
"CronistaWatcher"). Este módulo solo sirve para eliminarlas si quedaron (se llama al iniciar la app).
"""
from __future__ import annotations

import logging

from cronista import APP_NAME

log = logging.getLogger(__name__)

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
LEGACY_ENTRIES = (APP_NAME, APP_NAME + "Watcher")


def remove_legacy_entries() -> list[str]:
    """Borra del inicio de Windows cualquier entrada antigua de Cronista. Devuelve las que borró."""
    removed: list[str] = []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                            winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as k:
            for name in LEGACY_ENTRIES:
                try:
                    winreg.DeleteValue(k, name)
                    removed.append(name)
                except FileNotFoundError:
                    pass
    except Exception as e:
        log.debug("No se pudo revisar el inicio de Windows: %s", e)
    if removed:
        log.info("Eliminadas entradas antiguas de inicio automático: %s", ", ".join(removed))
    return removed

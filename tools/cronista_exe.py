"""Punto de entrada de Cronista.exe: solo el teleprompter (no incluye el asistente de notas por voz).

Uso normal: doble clic en Cronista.exe.
Comprobación (la usa tools/construir_exe.py; no abre ventanas):
    Cronista.exe --comprobar RUTA_DEL_RESULTADO.txt
    Escribe un resumen en RUTA y sale con código 0 si las letras cargan y la ventana se crea.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
import traceback
from pathlib import Path

if not getattr(sys, "frozen", False):
    # Ejecución desde el código fuente: que se pueda importar el paquete cronista desde tools/.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _sin_consola() -> None:
    """Con --windowed no hay stdout ni stderr: los dejamos en nulo para que nada falle al escribir."""
    for nombre in ("stdout", "stderr"):
        if getattr(sys, nombre) is None:
            setattr(sys, nombre, open(os.devnull, "w", encoding="utf-8"))


def _comprobar(ruta: Path) -> int:
    lineas: list[str] = []
    codigo = 0
    ventana = None
    try:
        from PySide6.QtGui import QFontDatabase
        from PySide6.QtWidgets import QApplication

        from cronista import __version__
        from cronista.core import paths
        from cronista.core.settings import Settings
        from cronista.teleprompter import fonts
        from cronista.teleprompter.window import TeleprompterWindow
        from cronista.ui.theme import apply_theme

        lineas.append(f"Cronista {__version__}")
        lineas.append(f"Ejecutable congelado: {'sí' if getattr(sys, 'frozen', False) else 'no'}")
        lineas.append(f"Carpeta de la app: {paths.app_root()}")

        qapp = QApplication.instance() or QApplication(sys.argv)

        # Letras incluidas (carpeta fonts/ junto al exe)
        carpeta_letras = paths.app_root() / "fonts"
        lineas.append(f"Carpeta de letras: {carpeta_letras} ({'existe' if carpeta_letras.is_dir() else 'NO EXISTE'})")
        fonts.load_bundled_fonts()
        for opt in fonts.FONT_OPTIONS:
            if not opt.file:
                continue
            archivo = (carpeta_letras / opt.file).exists()
            reconocida = QFontDatabase.hasFamily(opt.family)
            ok = archivo and reconocida
            if not ok:
                codigo = 1
            lineas.append(f"Letra {opt.label}: {'OK' if ok else 'FALLO'} "
                          f"(archivo {'sí' if archivo else 'NO'}, Qt la reconoce {'sí' if reconocida else 'NO'})")

        # Ventana del teleprompter, creada pero no mostrada, con un texto de prueba temporal
        settings = Settings.load()
        apply_theme(qapp, settings.ui.theme)
        texto_prueba = Path(tempfile.gettempdir()) / "cronista_comprobar_texto.txt"
        ventana = TeleprompterWindow(settings, text_path=texto_prueba)
        lineas.append("Ventana del teleprompter: OK (creada sin mostrar)")

        # Dependencias de voz: solo informan, no cambian el código de salida
        try:
            import vosk  # noqa: F401
            lineas.append(f"vosk: OK (versión {getattr(vosk, '__version__', 'desconocida')})")
        except Exception as e:  # noqa: BLE001
            lineas.append(f"vosk: NO SE PUEDE IMPORTAR ({type(e).__name__}: {e})")
        try:
            import sounddevice as sd
            lineas.append(f"sounddevice: OK (PortAudio {sd.get_portaudio_version()[1]})")
        except Exception as e:  # noqa: BLE001
            lineas.append(f"sounddevice: NO SE PUEDE IMPORTAR ({type(e).__name__}: {e})")
    except Exception:  # noqa: BLE001
        codigo = 1
        lineas.append("ERROR durante la comprobación:")
        lineas.append(traceback.format_exc())
    finally:
        if ventana is not None:
            try:
                ventana.close()
            except Exception:  # noqa: BLE001
                pass

    lineas.append("")
    lineas.append("RESULTADO: CORRECTO" if codigo == 0 else "RESULTADO: REVISAR (hay fallos arriba)")
    try:
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text("\n".join(lineas) + "\n", encoding="utf-8")
    except OSError:
        return 2
    return codigo


def main(argv: list[str] | None = None) -> int:
    _sin_consola()
    parser = argparse.ArgumentParser(prog="Cronista", description="Teleprompter de Cronista")
    parser.add_argument("--comprobar", metavar="RUTA", help="comprobar la instalación y escribir el resumen en RUTA")
    args = parser.parse_args(argv)

    from cronista.core import logging_setup
    logging_setup.setup(logging.INFO)

    if args.comprobar:
        return _comprobar(Path(args.comprobar))

    from cronista.core.settings import Settings
    from cronista.teleprompter.app import run_teleprompter
    return run_teleprompter(Settings.load())


if __name__ == "__main__":
    sys.exit(main())

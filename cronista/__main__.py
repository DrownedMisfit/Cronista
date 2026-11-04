"""Punto de entrada: `python -m cronista` abre el teleprompter; `--notas [--minimized] [--no-gui]` el asistente de
notas por voz (en pausa: necesita todo requirements.txt)."""
from __future__ import annotations

import argparse
import logging
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cronista", description="Teleprompter (con --notas: asistente de notas por voz)")
    parser.add_argument("--notas", action="store_true", help="abrir el asistente de notas por voz en vez del teleprompter")
    parser.add_argument("--minimized", action="store_true", help="(notas) iniciar oculto en la bandeja")
    parser.add_argument("--no-gui", action="store_true", help="(notas) modo consola, sin interfaz")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    from cronista.core import logging_setup
    logging_setup.setup(logging.DEBUG if args.debug else logging.INFO)

    from cronista.core.settings import Settings
    settings = Settings.load()

    if args.no_gui:
        return _run_headless(settings)
    if args.notas or args.minimized:
        from cronista.ui.main import run_gui
        return run_gui(settings, minimized=args.minimized or settings.ui.start_minimized)
    from cronista.teleprompter.app import run_teleprompter
    return run_teleprompter(settings)


def _run_headless(settings) -> int:
    import time
    from cronista.app import App
    from cronista.core.events import Topics

    app = App(settings)
    app.bus.subscribe(Topics.NOTE_CREATED, lambda e: print(f"[{e.note.get('position_ms')}] {e.note['text']}"))
    app.bus.subscribe(Topics.STT_PARTIAL, lambda e: print(f"  … {e.text}"))
    app.bus.subscribe(Topics.STATUS, lambda e: print(f"[{e.level}] {e.message}"))
    app.start()
    print("Cronista en modo consola. Ctrl+C para salir.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())

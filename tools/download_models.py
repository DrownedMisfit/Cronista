"""Descarga los modelos locales: Silero VAD y el modelo Whisper elegido.

Uso:  python tools/download_models.py [modelo]   (p. ej. large-v3-turbo, small)
Sin argumento usa el recomendado para este hardware.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cronista.audio.vad import ensure_silero_model  # noqa: E402
from cronista.core import hardware  # noqa: E402
from cronista.stt.engine import download_model, model_is_downloaded  # noqa: E402


def main() -> int:
    print("Silero VAD:", ensure_silero_model())
    name = sys.argv[1] if len(sys.argv) > 1 else hardware.detect().recommended_model()
    if model_is_downloaded(name):
        print(f"Whisper {name}: ya descargado")
        return 0
    print(f"Descargando Whisper {name}… (puede tardar varios minutos)")
    path = download_model(name)
    print("Listo:", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

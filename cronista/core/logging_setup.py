from __future__ import annotations

import logging
import logging.handlers
import sys

from cronista.core.paths import logs_dir


def setup(level: int = logging.INFO) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    fh = logging.handlers.RotatingFileHandler(logs_dir() / "cronista.log", maxBytes=2_000_000,
                                              backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if sys.stderr:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    for noisy in ("websocket", "faster_whisper", "urllib3", "huggingface_hub", "httpx", "httpcore", "filelock"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # obsws_python registra con traceback cada intento de conexión fallido (OBS cerrado): lo silenciamos,
    # el cliente de OBS ya informa del estado de forma legible.
    logging.getLogger("obsws_python").setLevel(logging.CRITICAL)

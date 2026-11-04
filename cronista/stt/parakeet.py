"""Motor de transcripción Parakeet-TDT-0.6B-v3 (NVIDIA, CC-BY-4.0) sobre sherpa-onnx (Apache-2.0), en CPU.

Por qué existe (medido en un Ryzen 7 5700X): Whisper procesa siempre ventanas fijas de 30 s, así que una
frase de 4 s cuesta casi lo mismo que 30 s de audio; con faster-whisper large-v3-turbo eso daba ~1,3-2 s de
cálculo por cada segundo hablado (más lento que el tiempo real: las notas se iban atrasando). Parakeet es un
modelo transductor que procesa solo la duración real de la frase y es multilingüe (25 idiomas europeos,
detecta el idioma solo). En español tiene menos error que Whisper large-v3 en MLS (3,41 frente a 4,32).

Interfaz idéntica a FasterWhisperBackend: transcribe(audio16k, ...) -> (texto, info). Los parámetros de
Whisper (prompt, beam…) se aceptan y se ignoran; el glosario bilingüe se aplica después igual que con Whisper.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import numpy as np

from cronista.core.paths import models_dir

log = logging.getLogger(__name__)

MODEL_NAME = "parakeet-tdt-0.6b-v3"
MODEL_DIR_NAME = "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
             "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8.tar.bz2")
MODEL_SIZE_MB = 670
_FILES = ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt")


def model_dir() -> Path:
    return models_dir() / "stt" / MODEL_DIR_NAME


def available() -> bool:
    try:
        import sherpa_onnx  # noqa: F401
    except Exception:
        return False
    d = model_dir()
    return all((d / f).is_file() for f in _FILES)


HF_BASE = "https://huggingface.co/csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8/resolve/main/"


def install_model(progress=None) -> Path:
    """Descarga el modelo (internet, ~670 MB) archivo por archivo desde el repositorio del autor de sherpa-onnx en
    Hugging Face (medido: ~25 s; el mismo modelo empaquetado en GitHub bajaba a ~50 KB/s)."""
    from cronista.ai.llm_setup import download_file
    if available():
        return model_dir()
    d = model_dir()
    d.mkdir(parents=True, exist_ok=True)
    sizes = {"tokens.txt": 0.1, "decoder.int8.onnx": 11.8, "joiner.int8.onnx": 6.4, "encoder.int8.onnx": 652.2}
    total, done_mb = sum(sizes.values()), 0.0
    for name in ("tokens.txt", "decoder.int8.onnx", "joiner.int8.onnx", "encoder.int8.onnx"):
        def prog(done, tot, _base=done_mb):
            if progress:
                progress(int((_base + done / 1e6) * 1e6), int(total * 1e6))
        download_file(HF_BASE + name, d / name, prog)
        done_mb += sizes[name]
    return d


class ParakeetBackend:
    """Reconocedor offline de sherpa-onnx (una frase completa cada vez). Seguro entre hilos con un lock."""

    def __init__(self, cpu_threads: int = 4, language: str = "es") -> None:
        import sherpa_onnx
        d = model_dir()
        if not available():
            raise FileNotFoundError(f"Falta el modelo Parakeet en {d}")
        t0 = time.monotonic()
        self.name = MODEL_NAME
        self.language = language
        self.device, self.compute_type = "cpu", "int8"
        self._rec = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(d / "encoder.int8.onnx"), decoder=str(d / "decoder.int8.onnx"),
            joiner=str(d / "joiner.int8.onnx"), tokens=str(d / "tokens.txt"),
            num_threads=max(1, int(cpu_threads)), sample_rate=16000, feature_dim=80,
            decoding_method="greedy_search", model_type="nemo_transducer", provider="cpu")
        self._lock = threading.Lock()
        log.info("Parakeet cargado en %.1fs (%d hilos)", time.monotonic() - t0, cpu_threads)

    def transcribe(self, audio: np.ndarray, beam_size: int = 5, initial_prompt: str | None = None,
                   hotwords: str | None = None, fast: bool = False) -> tuple[str, dict]:
        samples = np.ascontiguousarray(audio, dtype=np.float32)
        with self._lock:
            stream = self._rec.create_stream()
            stream.accept_waveform(16000, samples)
            self._rec.decode_stream(stream)
            res = stream.result
        text = (res.text or "").strip()
        lang = getattr(res, "lang", "") or ""
        return text, {"model": self.name, "language": lang.strip("<|>") or self.language,
                      "device": "cpu", "compute_type": "int8", "backend": "parakeet"}

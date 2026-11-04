"""Detección de actividad de voz (VAD).

SileroVAD: modelo ONNX de ~2 MB, ~1 ms por bloque de 32 ms en CPU. Es lo único
que se ejecuta de forma continua mientras no hablo. EnergyVAD es el respaldo si
el modelo no está disponible.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

import numpy as np

from cronista.core.paths import models_dir

log = logging.getLogger(__name__)

SILERO_URL = "https://raw.githubusercontent.com/snakers4/silero-vad/master/src/silero_vad/data/silero_vad.onnx"


class VAD(Protocol):
    sample_rate: int
    chunk: int

    def prob(self, chunk: np.ndarray) -> float: ...
    def reset(self) -> None: ...


class SileroVAD:
    """Silero VAD v5 (ONNX). Entrada: bloques de 512 muestras a 16 kHz + 64 de contexto."""

    def __init__(self, model_path: Path | None = None, sample_rate: int = 16000) -> None:
        import onnxruntime as ort

        path = model_path or (models_dir() / "silero_vad.onnx")
        if not path.exists():
            raise FileNotFoundError(f"No se encontró el modelo VAD en {path}. Ejecuta tools/download_models.py")
        so = ort.SessionOptions()
        so.intra_op_num_threads = 1
        so.inter_op_num_threads = 1
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.sess = ort.InferenceSession(str(path), sess_options=so, providers=["CPUExecutionProvider"])
        self.sample_rate = sample_rate
        self.chunk = 512 if sample_rate == 16000 else 256
        self.context_size = 64 if sample_rate == 16000 else 32
        names = {i.name for i in self.sess.get_inputs()}
        self._v5 = "state" in names
        self.reset()

    def reset(self) -> None:
        if self._v5:
            self._state = np.zeros((2, 1, 128), dtype=np.float32)
        else:
            self._h = np.zeros((2, 1, 64), dtype=np.float32)
            self._c = np.zeros((2, 1, 64), dtype=np.float32)
        self._context = np.zeros(self.context_size, dtype=np.float32)

    def prob(self, chunk: np.ndarray) -> float:
        if len(chunk) != self.chunk:
            if len(chunk) < self.chunk:
                chunk = np.pad(chunk, (0, self.chunk - len(chunk)))
            else:
                chunk = chunk[:self.chunk]
        x = np.concatenate([self._context, chunk]).astype(np.float32)[None, :]
        if self._v5:
            out, self._state = self.sess.run(None, {"input": x, "state": self._state,
                                                    "sr": np.array(self.sample_rate, dtype=np.int64)})
        else:
            out, self._h, self._c = self.sess.run(None, {"input": x, "h": self._h, "c": self._c,
                                                         "sr": np.array(self.sample_rate, dtype=np.int64)})
        self._context = chunk[-self.context_size:].astype(np.float32)
        return float(out[0][0])


class EnergyVAD:
    """Respaldo simple: umbral adaptativo sobre el nivel RMS relativo al suelo de ruido."""

    def __init__(self, sample_rate: int = 16000, chunk: int = 512) -> None:
        self.sample_rate = sample_rate
        self.chunk = chunk
        self.noise = 1e-4
        self.reset()

    def reset(self) -> None:
        self.noise = 1e-4

    def prob(self, chunk: np.ndarray) -> float:
        e = float(np.mean(chunk.astype(np.float64) ** 2)) + 1e-12
        if e < self.noise * 3:
            self.noise = 0.98 * self.noise + 0.02 * e
        snr = 10 * np.log10(e / self.noise)
        return float(np.clip((snr - 6.0) / 12.0, 0.0, 1.0))


def ensure_silero_model() -> Path:
    path = models_dir() / "silero_vad.onnx"
    if path.exists():
        return path
    import urllib.request
    log.info("Descargando modelo Silero VAD...")
    urllib.request.urlretrieve(SILERO_URL, path)
    return path


def build_vad(sample_rate: int = 16000) -> VAD:
    try:
        return SileroVAD(ensure_silero_model(), sample_rate)
    except Exception as e:
        log.warning("Silero VAD no disponible (%s). Se usa VAD por energía.", e)
        return EnergyVAD(sample_rate)


SENSITIVITY = {
    # (umbral de inicio, umbral de fin). Bajo = exige más seguridad para abrir.
    "low": (0.65, 0.40),
    "medium": (0.50, 0.30),
    "high": (0.35, 0.20),
}

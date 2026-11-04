"""Codificador de frases local (ONNX Runtime + tokenizers), sin conexión ni dependencias nuevas.

Usa paraphrase-multilingual-MiniLM-L12-v2 (Apache-2.0, 384 dimensiones, multilingüe), cuyos ficheros
ya están en `models/embed`. La carga es diferida: el modelo se lee al primer `encode` y se libera con
`unload()` para no ocupar RAM mientras se juega.
"""
from __future__ import annotations

import gc
import logging
import threading
from pathlib import Path

import numpy as np

from cronista.core.paths import models_dir

log = logging.getLogger(__name__)

MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2-q8"
MODEL_SUBDIR = "paraphrase-multilingual-MiniLM-L12-v2"
ONNX_RELPATH = Path("onnx") / "model_quint8_avx2.onnx"
TOKENIZER_FILE = "tokenizer.json"
DIM = 384
MAX_LENGTH = 128


def model_dir() -> Path:
    """Carpeta del modelo: models/embed/paraphrase-multilingual-MiniLM-L12-v2."""
    return models_dir() / "embed" / MODEL_SUBDIR


def model_available() -> bool:
    """True si están el grafo ONNX y el tokenizador en disco (no descarga nada)."""
    d = model_dir()
    return (d / ONNX_RELPATH).is_file() and (d / TOKENIZER_FILE).is_file()


class SentenceEncoder:
    """Convierte textos en vectores de 384 dimensiones normalizados (L2), listos para coseno."""

    MODEL_NAME = MODEL_NAME

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._session = None
        self._tokenizer = None
        self._token_type_ids = False

    def _load(self) -> None:
        """Carga la sesión ONNX y el tokenizador la primera vez (llamar con el lock tomado)."""
        if self._session is not None:
            return
        if not model_available():
            raise RuntimeError(f"Falta el modelo de embeddings en {model_dir()}")
        import onnxruntime as ort
        from tokenizers import Tokenizer

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        opts.log_severity_level = 3
        d = model_dir()
        session = ort.InferenceSession(str(d / ONNX_RELPATH), sess_options=opts,
                                       providers=["CPUExecutionProvider"])
        tok = Tokenizer.from_file(str(d / TOKENIZER_FILE))
        tok.enable_truncation(max_length=MAX_LENGTH)
        pad_id = tok.token_to_id("<pad>")
        tok.enable_padding(pad_id=pad_id if pad_id is not None else 0, pad_token="<pad>")
        self._token_type_ids = any(i.name == "token_type_ids" for i in session.get_inputs())
        self._tokenizer = tok
        self._session = session
        log.info("Modelo de embeddings cargado (%s)", MODEL_NAME)

    def encode(self, texts: list[str], batch: int = 32) -> np.ndarray:
        """Vectores [n, 384] float32 normalizados. Textos vacíos se codifican como " "."""
        items = [t if t and t.strip() else " " for t in texts]
        if not items:
            return np.zeros((0, DIM), dtype=np.float32)
        step = max(1, int(batch))
        out: list[np.ndarray] = []
        with self._lock:
            self._load()
            for start in range(0, len(items), step):
                encs = self._tokenizer.encode_batch(items[start:start + step])
                ids = np.array([e.ids for e in encs], dtype=np.int64)
                mask = np.array([e.attention_mask for e in encs], dtype=np.int64)
                feed = {"input_ids": ids, "attention_mask": mask}
                if self._token_type_ids:
                    feed["token_type_ids"] = np.zeros_like(ids)
                hidden = self._session.run(None, feed)[0]                    # [b, tokens, dim]
                w = mask[:, :, None].astype(np.float32)
                pooled = (hidden * w).sum(axis=1) / np.clip(w.sum(axis=1), 1e-9, None)
                norm = np.linalg.norm(pooled, axis=1, keepdims=True)
                out.append((pooled / np.clip(norm, 1e-12, None)).astype(np.float32))
        return np.concatenate(out, axis=0)

    def unload(self) -> None:
        """Libera la sesión ONNX y el tokenizador (la siguiente llamada a encode los vuelve a cargar)."""
        with self._lock:
            self._session = None
            self._tokenizer = None
        gc.collect()

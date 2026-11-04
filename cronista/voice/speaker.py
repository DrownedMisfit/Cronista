"""Perfil local de voz: ¿esta frase la dijo el usuario?

- SpeakerEncoder: WeSpeaker ResNet34-LM (ONNX, 26 MB, Apache-2.0) sobre onnxruntime en CPU, 1 hilo.
  Convierte audio en un vector de 256 números (embedding) que resume el timbre de la voz.
- VoiceProfile: la media de los embeddings de la grabación de configuración. Es lo único que se guarda
  (en %APPDATA%\\Cronista\\voice\\profile.json); el audio de la configuración no se guarda nunca.
- VoiceGate: se ejecuta en el hilo del transcriptor al terminar cada frase (~20-60 ms) y decide.

Filosofía (prioridad: no perder palabras): ante la duda, la frase se procesa. Las frases forzadas con el
hotkey y las demasiado cortas para juzgar no se filtran nunca. Por defecto, una voz que no coincide se
transcribe igualmente y se MARCA como "otra voz"; descartarla es una opción explícita.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from cronista.core.paths import data_dir, models_dir
from cronista.voice.features import SAMPLE_RATE, fbank

log = logging.getLogger(__name__)

MODEL_FILE = "wespeaker_en_voxceleb_resnet34_LM.onnx"
MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
             "wespeaker_en_voxceleb_resnet34_LM.onnx")
MODEL_SIZE_MB = 26.5

CHUNK_S = 3.0              # trozos de la grabación de configuración
MIN_ENROLL_SPEECH_S = 15.0
MIN_VERIFY_S = 1.0         # por debajo, la frase es demasiado corta para juzgar → se procesa
MAX_VERIFY_S = 6.0         # bastan los últimos 6 s (a partir de 3 s ya es fiable; ~200 ms en 1 hilo)
SHORT_FULL_S = 3.0         # por debajo de esto el parecido baja: el umbral se relaja
SHORT_RELAX = 0.08         # relajación máxima del umbral (a 1 s de voz)
SKIP_MARGIN = 0.10         # para DESCARTAR hay que quedar claramente por debajo del umbral...
SKIP_MIN_S = 2.0           # ...y la frase debe durar al menos esto; si no, solo se marca
DEFAULT_THRESHOLD = 0.35   # medido: la misma voz da 0,5-0,8 (≥3 s) y otra persona < 0,4


def model_path() -> Path:
    d = models_dir() / "voice"
    d.mkdir(parents=True, exist_ok=True)
    return d / MODEL_FILE


def profile_path() -> Path:
    d = data_dir() / "voice"
    d.mkdir(parents=True, exist_ok=True)
    return d / "profile.json"


def model_available() -> bool:
    return model_path().is_file()


def install_model(progress=None) -> Path:
    """Descarga el modelo de hablante (internet, ~26 MB). Reutiliza el descargador reanudable de la IA local."""
    from cronista.ai.llm_setup import download_file
    p = model_path()
    if p.is_file():
        return p
    return download_file(MODEL_URL, p, progress)


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


class SpeakerEncoder:
    def __init__(self, path: Path | None = None) -> None:
        import onnxruntime as ort
        path = path or model_path()
        if not path.is_file():
            raise FileNotFoundError(f"Falta el modelo de voz: {path}")
        so = ort.SessionOptions()
        so.intra_op_num_threads = 1      # no competir con el juego
        so.inter_op_num_threads = 1
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.sess = ort.InferenceSession(str(path), sess_options=so, providers=["CPUExecutionProvider"])
        self.input_name = self.sess.get_inputs()[0].name
        self.meta = dict(self.sess.get_modelmeta().custom_metadata_map or {})
        self._lock = threading.Lock()

    def embed(self, audio: np.ndarray) -> np.ndarray | None:
        """Embedding normalizado (256) de un audio de 16 kHz, o None si es demasiado corto."""
        feats = fbank(audio, SAMPLE_RATE, cmn=True)
        if len(feats) < 50:              # < 0,5 s de tramas: no hay información suficiente
            return None
        with self._lock:
            out = self.sess.run(None, {self.input_name: feats[None, :, :]})[0]
        emb = np.asarray(out, dtype=np.float32).reshape(-1)
        n = float(np.linalg.norm(emb))
        return emb / n if n > 0 else None


@dataclass
class VoiceProfile:
    embedding: list[float]
    threshold: float
    consistency: float              # similitud media de los trozos con su media (calidad de la grabación)
    speech_seconds: float
    chunks: int
    model: str = MODEL_FILE
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    @property
    def vector(self) -> np.ndarray:
        return np.asarray(self.embedding, dtype=np.float32)

    def save(self, path: Path | None = None) -> Path:
        path = path or profile_path()
        path.write_text(json.dumps(asdict(self), ensure_ascii=False), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path | None = None) -> "VoiceProfile | None":
        path = path or profile_path()
        if not path.is_file():
            return None
        try:
            return cls(**json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            log.exception("Perfil de voz ilegible; se ignora")
            return None

    @staticmethod
    def delete(path: Path | None = None) -> None:
        path = path or profile_path()
        if path.is_file():
            path.unlink()


def speech_only(audio: np.ndarray, probs: list[float] | None = None, chunk: int = 512,
                threshold: float = 0.5) -> np.ndarray:
    """Concatena solo los bloques con voz según las probabilidades de VAD (una por bloque de `chunk`).
    Sin probabilidades, devuelve el audio tal cual."""
    if not probs:
        return audio
    keep = [audio[i * chunk:(i + 1) * chunk] for i, p in enumerate(probs) if p >= threshold]
    return np.concatenate(keep) if keep else np.zeros(0, dtype=np.float32)


def build_profile(encoder: SpeakerEncoder, speech: np.ndarray) -> VoiceProfile:
    """Crea el perfil a partir de audio que solo contiene voz del usuario (≥ 15 s recomendado)."""
    seconds = len(speech) / SAMPLE_RATE
    if seconds < MIN_ENROLL_SPEECH_S:
        raise ValueError(f"Se necesitan al menos {MIN_ENROLL_SPEECH_S:.0f} s de voz (se detectaron {seconds:.0f} s). "
                         "Habla un poco más.")
    n = int(CHUNK_S * SAMPLE_RATE)
    embs = [e for i in range(0, len(speech) - n + 1, n) if (e := encoder.embed(speech[i:i + n])) is not None]
    if len(embs) < 3:
        raise ValueError("No se pudo extraer suficiente información de la voz. Repite la grabación.")
    E = np.stack(embs)
    mean = E.mean(axis=0)
    mean /= np.linalg.norm(mean)
    sims = E @ mean
    consistency = float(sims.mean())
    # Umbral calibrado con tu propia variabilidad, acotado para no rechazar tu voz (prioridad: no perder palabras)
    threshold = float(np.clip(float(np.percentile(sims, 10)) - 0.35, 0.25, 0.40))
    return VoiceProfile(embedding=[round(float(v), 6) for v in mean], threshold=round(threshold, 3),
                        consistency=round(consistency, 3), speech_seconds=round(seconds, 1), chunks=len(embs))


class VoiceGate:
    """Decide para cada frase: process (es tu voz o no se puede saber), mark (otra voz, se transcribe y
    se marca) o skip (otra voz y el usuario eligió descartarla)."""

    def __init__(self, settings) -> None:
        self.settings = settings
        self._encoder: SpeakerEncoder | None = None
        self._profile: VoiceProfile | None = None
        self._profile_mtime = 0.0
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.settings.voice.use_voice_id) and model_available() and self.profile() is not None

    def profile(self) -> VoiceProfile | None:
        p = profile_path()
        mtime = p.stat().st_mtime if p.is_file() else 0.0
        if mtime != self._profile_mtime:
            self._profile = VoiceProfile.load(p) if mtime else None
            self._profile_mtime = mtime
        return self._profile

    def encoder(self) -> SpeakerEncoder:
        with self._lock:
            if self._encoder is None:
                self._encoder = SpeakerEncoder()
            return self._encoder

    def unload(self) -> None:
        with self._lock:
            self._encoder = None

    def threshold(self) -> float:
        manual = float(self.settings.voice.threshold or 0.0)
        prof = self.profile()
        if manual > 0:
            return manual
        return prof.threshold if prof else DEFAULT_THRESHOLD

    def score(self, audio: np.ndarray) -> float | None:
        prof = self.profile()
        if prof is None:
            return None
        tail = audio[-int(MAX_VERIFY_S * SAMPLE_RATE):]
        emb = self.encoder().embed(tail)
        return None if emb is None else cosine(emb, prof.vector)

    def __call__(self, audio: np.ndarray, forced: bool = False) -> dict:
        """Devuelve {"score", "match", "action", "threshold"}; nunca lanza (ante error: process)."""
        if not self.enabled:
            return {"action": "process", "match": None, "score": None}
        if forced or len(audio) < MIN_VERIFY_S * SAMPLE_RATE:
            return {"action": "process", "match": None, "score": None, "reason": "forced" if forced else "short"}
        try:
            s = self.score(audio)
        except Exception as e:
            log.warning("Verificación de voz fallida (%s); se procesa la frase", e)
            return {"action": "process", "match": None, "score": None, "reason": "error"}
        if s is None:
            return {"action": "process", "match": None, "score": None, "reason": "short"}
        seconds = min(len(audio), MAX_VERIFY_S * SAMPLE_RATE) / SAMPLE_RATE
        thr = self.effective_threshold(seconds)
        match = s >= thr
        if match:
            action = "process"
        elif (self.settings.voice.non_match_action == "skip" and seconds >= SKIP_MIN_S
              and s < thr - SKIP_MARGIN):
            action = "skip"          # solo cuando es claramente otra voz
        else:
            action = "mark"          # zona dudosa: se transcribe y se marca (no se pierde nada)
        return {"action": action, "match": bool(match), "score": round(s, 3), "threshold": round(thr, 3),
                "seconds": round(seconds, 1)}

    def effective_threshold(self, seconds: float) -> float:
        """Umbral relajado en frases cortas: con 1 s de voz el parecido con tu perfil baja de forma natural.
        Medido con hablantes reales: a 1 s la propia voz llega a bajar a ~0,3; otra persona no pasa de ~0,25."""
        thr = self.threshold()
        if float(self.settings.voice.threshold or 0.0) > 0:
            return thr               # umbral manual: se respeta tal cual
        short = max(0.0, min(1.0, (SHORT_FULL_S - seconds) / (SHORT_FULL_S - MIN_VERIFY_S)))
        return max(0.2, thr - SHORT_RELAX * short)

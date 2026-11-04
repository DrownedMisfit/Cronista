"""Supresión de ruido en streaming.

Filosofía: conservador. La salida "limpia" se usa sobre todo para DETECTAR voz
(VAD). Whisper recibe por defecto el audio casi original, porque un filtro
agresivo puede borrar palabras y Whisper es bastante robusto al ruido.

Backends:
  - RNNoiseDenoiser: red neuronal ligera (48 kHz, 10 ms por trama), muy barata.
  - StationaryDenoiser: perfil espectral de ruido constante (abanico, aire
    acondicionado, PC) aprendido en los silencios; se resta de forma suave.
  - ChainDenoiser: combina varios.
"""
from __future__ import annotations

import logging
from typing import Protocol

import numpy as np

log = logging.getLogger(__name__)


class Denoiser(Protocol):
    sample_rate: int

    def process(self, x: np.ndarray) -> np.ndarray: ...
    def reset(self) -> None: ...


class NullDenoiser:
    def __init__(self, sample_rate: int) -> None:
        self.sample_rate = sample_rate

    def process(self, x: np.ndarray) -> np.ndarray:
        return x

    def reset(self) -> None:
        pass


class RNNoiseDenoiser:
    """Envuelve rnnoise.dll (paquete pyrnnoise) procesando tramas de 480 muestras a 48 kHz.
    `mix` controla cuánto de la señal filtrada se usa (1.0 = todo). Un mix < 1
    protege consonantes débiles que RNNoise a veces recorta."""

    FRAME = 480
    RATE = 48000

    def __init__(self, sample_rate: int = 48000, mix: float = 0.85) -> None:
        if sample_rate != self.RATE:
            raise ValueError("RNNoise requiere 48 kHz")
        from pyrnnoise import rnnoise as rn  # import diferido: opcional
        self._rn = rn
        self.sample_rate = sample_rate
        self.mix = float(mix)
        self._state = rn.create()
        self._pending = np.zeros(0, dtype=np.float32)
        self.last_speech_prob = 0.0
        import ctypes
        self._c_float_p = ctypes.POINTER(ctypes.c_float)
        self._lib = rn.lib

    def process(self, x: np.ndarray) -> np.ndarray:
        buf = np.concatenate([self._pending, x]) if self._pending.size else x
        n_frames = len(buf) // self.FRAME
        if n_frames == 0:
            self._pending = buf
            return np.zeros(0, dtype=np.float32)
        usable = n_frames * self.FRAME
        self._pending = buf[usable:].copy()
        # RNNoise trabaja con escala int16 en float
        inp = np.ascontiguousarray(buf[:usable] * 32768.0, dtype=np.float32)
        out = np.empty_like(inp)
        lib = self._lib
        probs = []
        for i in range(n_frames):
            seg_in = inp[i * self.FRAME:(i + 1) * self.FRAME]
            seg_out = out[i * self.FRAME:(i + 1) * self.FRAME]
            p = lib.rnnoise_process_frame(self._state,
                                          seg_out.ctypes.data_as(self._c_float_p),
                                          seg_in.ctypes.data_as(self._c_float_p))
            probs.append(float(p))
        self.last_speech_prob = max(probs) if probs else 0.0
        clean = out / 32768.0
        if self.mix < 1.0:
            clean = self.mix * clean + (1.0 - self.mix) * buf[:usable]
        return clean.astype(np.float32)

    def reset(self) -> None:
        self._pending = np.zeros(0, dtype=np.float32)

    def __del__(self) -> None:
        try:
            self._rn.destroy(self._state)
        except Exception:
            pass


class StationaryDenoiser:
    """Sustracción espectral suave contra ruido CONSTANTE.

    Aprende el perfil del ruido en los bloques que el VAD marca como silencio
    (se alimenta con `observe_noise`). La atenuación máxima está limitada
    (`max_atten_db`) para no crear huecos ni "voz robótica"."""

    def __init__(self, sample_rate: int, n_fft: int = 512, max_atten_db: float = 12.0,
                 over_sub: float = 1.3, adapt: float = 0.02) -> None:
        self.sample_rate = sample_rate
        self.n_fft = n_fft
        self.hop = n_fft // 2
        # raíz de Hann en análisis y síntesis: el producto es Hann, que con 50 % de
        # solape suma exactamente 1 (reconstrucción perfecta sin ganancia).
        self.window = np.sqrt(np.hanning(n_fft)).astype(np.float32)
        self.noise_psd = np.zeros(n_fft // 2 + 1, dtype=np.float32)
        self.have_profile = False
        self.floor = 10 ** (-max_atten_db / 20.0)
        self.over_sub = over_sub
        self.adapt = adapt
        self._in_tail = np.zeros(0, dtype=np.float32)
        self._out_overlap = np.zeros(self.hop, dtype=np.float32)
        self._frames_seen = 0

    def observe_noise(self, x: np.ndarray) -> None:
        """Actualiza el perfil con audio que se considera ruido de fondo."""
        if len(x) < self.n_fft:
            return
        n = (len(x) - self.n_fft) // self.hop + 1
        acc = np.zeros_like(self.noise_psd)
        for i in range(n):
            seg = x[i * self.hop:i * self.hop + self.n_fft] * self.window
            acc += np.abs(np.fft.rfft(seg)) ** 2
        acc /= max(n, 1)
        if not self.have_profile:
            self.noise_psd = acc.astype(np.float32)
            self.have_profile = True
        else:
            self.noise_psd = ((1 - self.adapt) * self.noise_psd + self.adapt * acc).astype(np.float32)

    def process(self, x: np.ndarray) -> np.ndarray:
        if not self.have_profile:
            return x
        buf = np.concatenate([self._in_tail, x])
        n = (len(buf) - self.n_fft) // self.hop + 1
        if n <= 0:
            self._in_tail = buf
            return np.zeros(0, dtype=np.float32)
        out = np.zeros((n - 1) * self.hop + self.n_fft, dtype=np.float32)
        for i in range(n):
            seg = buf[i * self.hop:i * self.hop + self.n_fft] * self.window
            spec = np.fft.rfft(seg)
            mag2 = np.abs(spec) ** 2
            gain = 1.0 - self.over_sub * self.noise_psd / (mag2 + 1e-10)
            gain = np.maximum(gain, self.floor ** 2)
            spec *= np.sqrt(gain)
            out[i * self.hop:i * self.hop + self.n_fft] += np.fft.irfft(spec, n=self.n_fft).astype(np.float32) * self.window
        consumed = n * self.hop
        self._in_tail = buf[consumed:]
        # overlap-add: entregamos las muestras completas, guardamos la cola
        ready = out[:consumed]
        ready[:self.hop] += self._out_overlap
        self._out_overlap = out[consumed:consumed + self.hop].copy()
        return ready.astype(np.float32)

    def reset(self) -> None:
        self._in_tail = np.zeros(0, dtype=np.float32)
        self._out_overlap = np.zeros(self.hop, dtype=np.float32)


class ChainDenoiser:
    def __init__(self, stages: list) -> None:
        self.stages = stages
        self.sample_rate = stages[0].sample_rate if stages else 0

    def process(self, x: np.ndarray) -> np.ndarray:
        for s in self.stages:
            x = s.process(x)
        return x

    def reset(self) -> None:
        for s in self.stages:
            s.reset()


def build_denoiser(level: str, sample_rate: int) -> tuple[object, StationaryDenoiser | None]:
    """Crea la cadena según el nivel de configuración. Devuelve (cadena, etapa estacionaria)."""
    level = (level or "auto").lower()
    if level == "off":
        return NullDenoiser(sample_rate), None
    stages: list = []
    stationary: StationaryDenoiser | None = None
    if sample_rate == 48000:
        try:
            mix = {"light": 0.6, "auto": 0.85, "strong": 1.0}.get(level, 0.85)
            stages.append(RNNoiseDenoiser(sample_rate, mix=mix))
        except Exception as e:  # DLL ausente o incompatible
            log.warning("RNNoise no disponible (%s); se usa solo filtro estacionario", e)
    if level in ("auto", "strong"):
        stationary = StationaryDenoiser(sample_rate, max_atten_db=10.0 if level == "auto" else 15.0)
        stages.append(stationary)
    if not stages:
        return NullDenoiser(sample_rate), None
    return ChainDenoiser(stages), stationary

"""Utilidades DSP ligeras (solo numpy): remuestreo en streaming, niveles, ganancia."""
from __future__ import annotations

import math

import numpy as np


def db(x: float) -> float:
    return 20.0 * math.log10(max(x, 1e-9))


def rms_db(audio: np.ndarray) -> float:
    if audio.size == 0:
        return -120.0
    return db(float(np.sqrt(np.mean(audio.astype(np.float64) ** 2))))


def peak_db(audio: np.ndarray) -> float:
    if audio.size == 0:
        return -120.0
    return db(float(np.max(np.abs(audio))))


def apply_gain(audio: np.ndarray, gain_db: float) -> np.ndarray:
    if abs(gain_db) < 0.01:
        return audio
    return np.clip(audio * (10 ** (gain_db / 20.0)), -1.0, 1.0).astype(np.float32)


def _lowpass_fir(cutoff_hz: float, fs: float, taps: int = 63) -> np.ndarray:
    fc = cutoff_hz / fs
    n = np.arange(taps) - (taps - 1) / 2.0
    h = 2 * fc * np.sinc(2 * fc * n)
    h *= np.hamming(taps)
    return (h / h.sum()).astype(np.float32)


class StreamResampler:
    """Remuestreo en streaming float32 mono. Decimación entera por FIR cuando es
    posible (48k→16k) o interpolación lineal tras paso bajo en el caso general."""

    def __init__(self, src_rate: int, dst_rate: int) -> None:
        self.src = int(src_rate)
        self.dst = int(dst_rate)
        self.identity = self.src == self.dst
        if self.identity:
            return
        self.ratio = self.src / self.dst
        self.int_decim = self.src % self.dst == 0
        self.factor = self.src // self.dst if self.int_decim else 1
        cutoff = min(self.src, self.dst) * 0.45
        self.h = _lowpass_fir(cutoff, self.src)
        self.tail = np.zeros(len(self.h) - 1, dtype=np.float32)
        self.phase = 0  # para decimación entera
        self._pos = 0.0  # para interpolación

    def process(self, x: np.ndarray) -> np.ndarray:
        if self.identity:
            return x
        if x.size == 0:
            return x
        buf = np.concatenate([self.tail, x])
        y = np.convolve(buf, self.h, mode="valid").astype(np.float32)  # len == len(x)
        self.tail = buf[-(len(self.h) - 1):]
        if self.int_decim:
            out = y[self.phase::self.factor]
            self.phase = (self.phase - len(y)) % self.factor
            return out
        # caso general
        n_out = int(math.floor((len(y) - self._pos - 1) / self.ratio)) + 1
        idx = self._pos + np.arange(n_out) * self.ratio
        out = np.interp(idx, np.arange(len(y)), y).astype(np.float32)
        self._pos = idx[-1] + self.ratio - len(y) if n_out else self._pos - len(y)
        return out

    def flush(self) -> np.ndarray:
        return np.zeros(0, dtype=np.float32)


class RingBuffer:
    """Buffer circular de muestras float32 para el pre-roll."""

    def __init__(self, capacity: int) -> None:
        self.capacity = max(1, int(capacity))
        self.buf = np.zeros(self.capacity, dtype=np.float32)
        self.filled = 0
        self.pos = 0

    def push(self, x: np.ndarray) -> None:
        n = len(x)
        if n >= self.capacity:
            self.buf[:] = x[-self.capacity:]
            self.pos = 0
            self.filled = self.capacity
            return
        end = self.pos + n
        if end <= self.capacity:
            self.buf[self.pos:end] = x
        else:
            k = self.capacity - self.pos
            self.buf[self.pos:] = x[:k]
            self.buf[:n - k] = x[k:]
        self.pos = end % self.capacity
        self.filled = min(self.capacity, self.filled + n)

    def get(self, n: int | None = None) -> np.ndarray:
        n = self.filled if n is None else min(n, self.filled)
        if n == 0:
            return np.zeros(0, dtype=np.float32)
        start = (self.pos - n) % self.capacity
        if start + n <= self.capacity:
            return self.buf[start:start + n].copy()
        return np.concatenate([self.buf[start:], self.buf[:(start + n) % self.capacity]])

    def clear(self) -> None:
        self.filled = 0
        self.pos = 0

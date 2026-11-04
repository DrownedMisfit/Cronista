"""Filterbank log-mel compatible con Kaldi (torchaudio.compliance.kaldi.fbank), solo con numpy.

Es la entrada que esperan los modelos de hablante de WeSpeaker: 80 bandas mel, tramas de 25 ms cada 10 ms,
16 kHz, ventana Hamming, sin dither, `snip_edges=True`, muestras en escala int16 y normalización de media
por oyente (CMN). Mantener idéntico el preprocesado del entrenamiento es lo que hace fiable el embedding.
"""
from __future__ import annotations

import math
from functools import lru_cache

import numpy as np

SAMPLE_RATE = 16000
FRAME_LEN = 400          # 25 ms
FRAME_SHIFT = 160        # 10 ms
N_FFT = 512              # siguiente potencia de 2 (round_to_power_of_two de Kaldi)
NUM_MEL = 80
LOW_FREQ = 20.0
PREEMPH = 0.97
_EPS = float(np.finfo(np.float32).eps)


def _mel(f: np.ndarray | float) -> np.ndarray | float:
    return 1127.0 * np.log(1.0 + np.asarray(f) / 700.0)


@lru_cache(maxsize=4)
def mel_banks(num_bins: int = NUM_MEL, n_fft: int = N_FFT, sr: int = SAMPLE_RATE,
              low: float = LOW_FREQ, high: float = 0.0) -> np.ndarray:
    """Matriz [num_bins, n_fft//2] de pesos triangulares en escala mel de Kaldi (sin el bin de Nyquist)."""
    nyquist = 0.5 * sr
    if high <= 0.0:
        high = nyquist + high
    n_bins_fft = n_fft // 2
    fft_bin_width = sr / n_fft
    mel_low, mel_high = float(_mel(low)), float(_mel(high))
    delta = (mel_high - mel_low) / (num_bins + 1)
    mel_f = _mel(fft_bin_width * np.arange(n_bins_fft))
    banks = np.zeros((num_bins, n_bins_fft), dtype=np.float64)
    for b in range(num_bins):
        left, center, right = mel_low + b * delta, mel_low + (b + 1) * delta, mel_low + (b + 2) * delta
        up = (mel_f - left) / (center - left)
        down = (right - mel_f) / (right - center)
        banks[b] = np.maximum(0.0, np.minimum(up, down))
        banks[b][(mel_f <= left) | (mel_f >= right)] = 0.0
    return banks


@lru_cache(maxsize=2)
def _hamming(n: int = FRAME_LEN) -> np.ndarray:
    return 0.54 - 0.46 * np.cos(2.0 * math.pi * np.arange(n) / (n - 1))


def fbank(audio: np.ndarray, sr: int = SAMPLE_RATE, cmn: bool = True) -> np.ndarray:
    """audio float32 mono en [-1, 1] a 16 kHz → [T, 80] float32. Vacío si dura menos de 25 ms."""
    if sr != SAMPLE_RATE:
        raise ValueError("fbank requiere audio a 16 kHz")
    x = np.asarray(audio, dtype=np.float64) * 32768.0          # escala int16, como en el entrenamiento
    n = len(x)
    if n < FRAME_LEN:
        return np.zeros((0, NUM_MEL), dtype=np.float32)
    num_frames = 1 + (n - FRAME_LEN) // FRAME_SHIFT
    idx = np.arange(FRAME_LEN)[None, :] + FRAME_SHIFT * np.arange(num_frames)[:, None]
    frames = x[idx]                                             # [T, 400]
    frames = frames - frames.mean(axis=1, keepdims=True)        # remove_dc_offset
    # preénfasis de Kaldi: x[i] -= c*x[i-1] (i>0); x[0] -= c*x[0]
    pre = frames.copy()
    pre[:, 1:] -= PREEMPH * frames[:, :-1]
    pre[:, 0] -= PREEMPH * frames[:, 0]
    pre *= _hamming()
    spec = np.fft.rfft(pre, n=N_FFT, axis=1)
    power = (spec.real ** 2 + spec.imag ** 2)[:, :N_FFT // 2]   # Kaldi descarta el bin de Nyquist
    mel = power @ mel_banks().T
    feats = np.log(np.maximum(mel, _EPS))
    if cmn and len(feats):
        feats = feats - feats.mean(axis=0, keepdims=True)
    return feats.astype(np.float32)

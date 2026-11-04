"""Captura independiente del micrófono (sounddevice / PortAudio).

Solo se abre el dispositivo de ENTRADA elegido. Nunca se toca el audio de
salida de Windows, OBS ni el juego. `stop()` cierra el dispositivo de inmediato.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass

import numpy as np
import sounddevice as sd

log = logging.getLogger(__name__)


@dataclass
class AudioBlock:
    samples: np.ndarray   # float32 mono, a `rate`
    rate: int
    t_mono: float         # time.monotonic() aproximado del primer sample


@dataclass
class InputDevice:
    index: int
    name: str
    hostapi: str
    default_rate: int
    channels: int

    def label(self) -> str:
        return f"{self.name} ({self.hostapi})"


def list_input_devices() -> list[InputDevice]:
    out: list[InputDevice] = []
    try:
        apis = sd.query_hostapis()
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_input_channels", 0) <= 0:
                continue
            api = apis[d["hostapi"]]["name"]
            out.append(InputDevice(i, d["name"], api, int(d.get("default_samplerate", 48000)),
                                   int(d["max_input_channels"])))
    except Exception:
        log.exception("No se pudieron listar los micrófonos")
    return out


def _preferred_order(devs: list[InputDevice]) -> list[InputDevice]:
    rank = {"Windows WASAPI": 0, "MME": 1, "Windows DirectSound": 2, "Windows WDM-KS": 3}
    return sorted(devs, key=lambda d: rank.get(d.hostapi, 9))


def resolve_device(name: str | None) -> InputDevice | None:
    devs = list_input_devices()
    if not devs:
        return None
    if name:
        exact = [d for d in devs if d.label() == name]
        if exact:
            return exact[0]
        same = [d for d in devs if d.name == name or name in d.name]
        if same:
            return _preferred_order(same)[0]
    try:
        default_idx = sd.default.device[0]
        default_name = sd.query_devices(default_idx)["name"] if default_idx is not None and default_idx >= 0 else None
    except Exception:
        default_name = None
    if default_name:
        same = [d for d in devs if d.name == default_name]
        if same:
            return _preferred_order(same)[0]
    return _preferred_order(devs)[0]


class MicCapture:
    """Hilo de PortAudio → cola de AudioBlock. Intenta 48 kHz (para RNNoise) y
    cae al sample rate nativo del dispositivo si no es posible."""

    def __init__(self, device: InputDevice, preferred_rate: int = 48000, block_ms: int = 20,
                 max_queue: int = 400) -> None:
        self.device = device
        self.preferred_rate = preferred_rate
        self.block_ms = block_ms
        self.queue: queue.Queue[AudioBlock] = queue.Queue(maxsize=max_queue)
        self.stream: sd.InputStream | None = None
        self.rate = preferred_rate
        self.dropped = 0
        self._lock = threading.Lock()
        self.error: str | None = None

    def start(self) -> int:
        with self._lock:
            if self.stream is not None:
                return self.rate
            last_err: Exception | None = None
            for rate in (self.preferred_rate, self.device.default_rate, 16000, 44100):
                try:
                    self._open(rate)
                    self.rate = rate
                    log.info("Micrófono abierto: %s @ %d Hz", self.device.label(), rate)
                    return rate
                except Exception as e:  # probar siguiente tasa
                    last_err = e
                    self.stream = None
            raise RuntimeError(f"No se pudo abrir el micrófono {self.device.label()}: {last_err}")

    def _open(self, rate: int) -> None:
        extra = None
        if self.device.hostapi == "Windows WASAPI":
            try:
                extra = sd.WasapiSettings(auto_convert=True)
            except Exception:
                extra = None
        channels = 1 if self.device.channels >= 1 else self.device.channels
        blocksize = int(rate * self.block_ms / 1000)
        self.stream = sd.InputStream(device=self.device.index, channels=channels, samplerate=rate,
                                     dtype="float32", blocksize=blocksize, callback=self._cb,
                                     extra_settings=extra, latency="low")
        self.stream.start()

    def _cb(self, indata: np.ndarray, frames: int, t, status) -> None:  # hilo de PortAudio
        if status and status.input_overflow:
            self.dropped += 1
        mono = indata[:, 0].copy() if indata.ndim == 2 else indata.copy()
        now = time.monotonic() - frames / self.rate
        try:
            self.queue.put_nowait(AudioBlock(mono, self.rate, now))
        except queue.Full:
            self.dropped += 1

    def stop(self) -> None:
        with self._lock:
            if self.stream is not None:
                try:
                    self.stream.stop()
                    self.stream.close()
                finally:
                    self.stream = None
                log.info("Micrófono cerrado")

    @property
    def active(self) -> bool:
        return self.stream is not None

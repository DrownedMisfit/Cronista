"""Reloj de grabación: convierte instantes del reloj monotónico del sistema en
posición dentro de la grabación (ms), respetando pausas y re-sincronizándose
periódicamente con la duración real que informa OBS.

Es una estructura pura (sin hilos). Guarda "anclas" (mono, pos, running): la
posición en un instante t es la del último ancla anterior a t más el tiempo
transcurrido si el reloj estaba corriendo.
"""
from __future__ import annotations

import bisect
import time
from dataclasses import dataclass


@dataclass
class Anchor:
    mono: float
    pos_ms: float
    running: bool


class RecordingClock:
    def __init__(self) -> None:
        self._anchors: list[Anchor] = []
        self._keys: list[float] = []
        self.started = False
        self.total_drift_ms = 0.0

    def _add(self, a: Anchor) -> None:
        self._anchors.append(a)
        self._keys.append(a.mono)

    def start(self, mono: float | None = None, pos_ms: float = 0.0) -> None:
        mono = time.monotonic() if mono is None else mono
        self._anchors.clear()
        self._keys.clear()
        self._add(Anchor(mono, pos_ms, True))
        self.started = True
        self.total_drift_ms = 0.0

    def pause(self, mono: float | None = None) -> None:
        if not self.started:
            return
        mono = time.monotonic() if mono is None else mono
        self._add(Anchor(mono, self.position_at(mono), False))

    def resume(self, mono: float | None = None) -> None:
        if not self.started:
            return
        mono = time.monotonic() if mono is None else mono
        self._add(Anchor(mono, self.position_at(mono), True))

    def stop(self) -> None:
        self.started = False

    @property
    def running(self) -> bool:
        return self.started and bool(self._anchors) and self._anchors[-1].running

    def sync(self, obs_duration_ms: float, mono: float | None = None, paused: bool | None = None,
             jump_threshold_ms: float = 250.0) -> float:
        """Corrige la deriva con la duración informada por OBS. Devuelve la corrección aplicada."""
        if not self.started:
            return 0.0
        mono = time.monotonic() if mono is None else mono
        est = self.position_at(mono)
        drift = obs_duration_ms - est
        running = (not paused) if paused is not None else self.running
        if abs(drift) >= jump_threshold_ms or running != self.running:
            self._add(Anchor(mono, obs_duration_ms, running))
            self.total_drift_ms += drift
            return drift
        return 0.0

    def position_at(self, mono: float) -> float:
        if not self._anchors:
            return 0.0
        i = bisect.bisect_right(self._keys, mono) - 1
        if i < 0:
            a = self._anchors[0]
            return max(0.0, a.pos_ms - (a.mono - mono) * 1000.0) if a.running else a.pos_ms
        a = self._anchors[i]
        return a.pos_ms + (mono - a.mono) * 1000.0 if a.running else a.pos_ms

    def position_now(self) -> float:
        return self.position_at(time.monotonic())


def fmt_timestamp(ms: float | int | None, with_ms: bool = False) -> str:
    if ms is None:
        return "--:--:--"
    ms = max(0, int(ms))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, frac = divmod(rem, 1000)
    base = f"{h:02d}:{m:02d}:{s:02d}"
    return f"{base}.{frac:03d}" if with_ms else base


def fmt_srt(ms: float | int) -> str:
    ms = max(0, int(ms))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, frac = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{frac:03d}"

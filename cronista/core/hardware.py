"""Detección de hardware y recomendación de modelo (sin cargar nada pesado)."""
from __future__ import annotations

import os
from dataclasses import dataclass

import psutil


@dataclass
class HardwareInfo:
    physical_cores: int
    logical_cores: int
    ram_gb: float
    cuda: bool

    def recommended_model(self) -> str:
        if self.cuda:
            return "large-v3"
        if self.ram_gb >= 12 and self.physical_cores >= 8:
            return "large-v3-turbo"
        if self.ram_gb >= 8 and self.physical_cores >= 6:
            return "medium"
        if self.physical_cores >= 4:
            return "small"
        return "base"

    def recommended_threads(self) -> int:
        # La mitad de los núcleos físicos: deja margen al juego y a OBS.
        return max(2, self.physical_cores // 2)

    def describe(self) -> str:
        return (f"{self.physical_cores} núcleos / {self.logical_cores} hilos, "
                f"{self.ram_gb:.0f} GB RAM, GPU CUDA: {'sí' if self.cuda else 'no'}")


def _has_cuda() -> bool:
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def detect() -> HardwareInfo:
    phys = psutil.cpu_count(logical=False) or os.cpu_count() or 4
    logi = psutil.cpu_count(logical=True) or phys
    ram = psutil.virtual_memory().total / (1024 ** 3)
    return HardwareInfo(phys, logi, ram, _has_cuda())

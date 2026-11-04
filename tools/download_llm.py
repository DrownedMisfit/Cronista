"""Descarga la IA local de Cronista: runtime llama.cpp (Vulkan, Windows) y modelo Qwen3.5-4B GGUF.

AVISO: usa internet. Se ejecuta a mano y una sola vez; si se corta, al repetirlo se reanuda.

Uso:  .venv\\Scripts\\python.exe tools\\download_llm.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cronista.ai import llm_setup  # noqa: E402


def _progreso(etiqueta: str):
    ultimo = {"pct": -1}

    def cb(hechos: int, total: int) -> None:
        if total:
            pct = hechos * 100 // total
            if pct == ultimo["pct"]:
                return
            ultimo["pct"] = pct
            print(f"\r{etiqueta}: {hechos / 1e6:,.0f} / {total / 1e6:,.0f} MB ({pct}%)   ", end="", flush=True)
        else:
            print(f"\r{etiqueta}: {hechos / 1e6:,.1f} MB   ", end="", flush=True)

    return cb


def main() -> int:
    print("IA local de Cronista: llama.cpp (Vulkan) + Qwen3.5-4B Q4_K_M")
    print(f"AVISO: esta descarga usa internet: ~{llm_setup.RUNTIME_SIZE_MB} MB de runtime "
          f"y ~{llm_setup.MODEL_SIZE_GB} GB de modelo.")

    estado = llm_setup.status()
    if estado["runtime"] and estado["model"]:
        print("Ya están instalados:")
        print("  runtime:", estado["runtime"])
        print("  modelo: ", estado["model"])
        return 0

    try:
        if estado["runtime"]:
            print("1/2 Runtime ya instalado:", estado["runtime"])
        else:
            print("1/2 Descargando runtime llama.cpp (Vulkan)…")
            ruta = llm_setup.install_runtime(_progreso("runtime"))
            print(f"\n    OK: {ruta}")

        if estado["model"]:
            print("2/2 Modelo ya instalado:", estado["model"])
        else:
            print("2/2 Descargando modelo…")
            ruta = llm_setup.install_model(_progreso("modelo"))
            print(f"\n    OK: {ruta}")
    except KeyboardInterrupt:
        print("\nCancelado. Vuelve a ejecutarlo para reanudar.")
        return 130
    except Exception as e:
        print(f"\nERROR: {e}")
        return 1

    print("Listo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

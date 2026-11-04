"""Arma Exportados/Cronista.zip para mandárselo a otra persona: solo el teleprompter, las letras y el lanzador.

Uso: .venv\\Scripts\\python.exe tools\\empaquetar.py
La otra persona descomprime y hace doble clic en «Abrir Cronista.bat» (la primera vez lo prepara todo solo).
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
from cronista import __version__  # noqa: E402

SALIDA = RAIZ / "Exportados" / "Cronista.zip"
CARPETA = "Cronista"        # carpeta que aparece al descomprimir

# Solo lo que usa el teleprompter (las notas por voz, en pausa, no se reparten).
CODIGO = [
    "cronista/__init__.py",
    "cronista/__main__.py",
    "cronista/core/__init__.py",
    "cronista/core/paths.py",
    "cronista/core/settings.py",
    "cronista/core/logging_setup.py",
    "cronista/ui/__init__.py",
    "cronista/ui/theme.py",
]
DISTRIBUCION = ["Abrir Cronista.bat", "COMO USAR.txt", "requisitos.txt"]


def _archivos() -> list[tuple[Path, str]]:
    lista = [(RAIZ / ruta, ruta) for ruta in CODIGO]
    lista += [(p, p.relative_to(RAIZ).as_posix()) for p in sorted((RAIZ / "cronista" / "teleprompter").glob("*.py"))]
    lista += [(p, p.relative_to(RAIZ).as_posix()) for p in sorted((RAIZ / "fonts").iterdir()) if p.is_file()]
    lista += [(RAIZ / "distribucion" / nombre, nombre) for nombre in DISTRIBUCION]
    lista.append((RAIZ / "LICENSE", "LICENSE.txt"))           # GPL v3
    return lista


def main() -> int:
    archivos = _archivos()
    faltan = [str(origen) for origen, _ in archivos if not origen.exists()]
    if faltan:
        print("Faltan archivos:\n  " + "\n  ".join(faltan))
        return 1
    SALIDA.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(SALIDA, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for origen, destino in archivos:
            datos = origen.read_bytes()
            if origen.suffix.lower() in (".bat", ".txt"):
                # cmd.exe necesita finales de línea de Windows (con solo \n los «goto» fallan)
                datos = datos.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            zf.writestr(f"{CARPETA}/{destino}", datos)
    print(f"Cronista {__version__}: {SALIDA} ({SALIDA.stat().st_size / 1024:.0f} KB, {len(archivos)} archivos)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

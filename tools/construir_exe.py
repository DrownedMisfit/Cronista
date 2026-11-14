"""Construye Cronista.exe (carpeta, sin consola) y deja listo el .zip para repartir.

Uso: .venv\\Scripts\\python.exe tools\\construir_exe.py

Resultado: Exportados\\Cronista-<versión>-windows.zip (dentro, carpeta Cronista\\ con Cronista.exe).
Las carpetas de trabajo de PyInstaller van a Exportados\\_construir.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
from cronista import __version__  # noqa: E402

VENV_PY = RAIZ / ".venv" / "Scripts" / "python.exe"
ENTRADA = RAIZ / "tools" / "cronista_exe.py"
NOMBRE = "Cronista"

EXPORTADOS = RAIZ / "Exportados"
OBRA = EXPORTADOS / "_construir"
DIST = OBRA / "dist"
BUILD = OBRA / "build"
SPEC = OBRA / "spec"
CARPETA = DIST / NOMBRE
ZIP = EXPORTADOS / f"{NOMBRE}-{__version__}-windows.zip"

# Paquetes pesados del asistente de notas por voz: el teleprompter no los usa.
EXCLUIR = [
    "torch", "torchvision", "torchaudio", "faster_whisper", "ctranslate2", "sherpa_onnx", "onnxruntime",
    "obsws_python", "keyboard", "pyrnnoise", "soundfile", "scipy", "docx", "pytest", "_pytest",
    "psutil", "tkinter", "matplotlib", "IPython",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DExtras", "PySide6.QtMultimedia",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtPdf", "PySide6.QtDesigner",
]
# Paquetes que se importan de forma perezosa (vosk, sounddevice): hay que llevar sus DLL y datos.
RECOGER_TODO = ["vosk", "sounddevice", "_sounddevice_data",
                # «Líneas por frases»: spaCy, su modelo de español y las piezas compiladas que carga por su cuenta
                "spacy", "es_core_news_sm", "thinc", "blis", "cymem", "preshed", "murmurhash", "srsly", "catalogue",
                "confection", "wasabi", "spacy_legacy", "spacy_loggers"]
IMPORTACIONES_OCULTAS = ["_cffi_backend"]


def _tamano(carpeta: Path) -> int:
    return sum(p.stat().st_size for p in carpeta.rglob("*") if p.is_file())


def _mb(bytes_: int) -> str:
    return f"{bytes_ / (1024 * 1024):.1f} MB"


def _texto_windows(origen: Path, destino: Path) -> None:
    """Copia un .txt con finales de línea de Windows (CRLF)."""
    datos = origen.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    destino.write_bytes(datos)


def main() -> int:
    if not VENV_PY.exists():
        print(f"No encuentro el Python de la .venv: {VENV_PY}")
        return 1

    print(f"Cronista {__version__}: limpiando la obra anterior...")
    if OBRA.exists():
        shutil.rmtree(OBRA)
    if ZIP.exists():
        ZIP.unlink()
    for carpeta in (DIST, BUILD, SPEC):
        carpeta.mkdir(parents=True, exist_ok=True)

    print("Ejecutando PyInstaller (puede tardar varios minutos)...")
    args = [
        str(VENV_PY), "-m", "PyInstaller",
        "--noconfirm", "--clean", "--log-level", "WARN",
        "--onedir", "--windowed", "--name", NOMBRE,
        "--distpath", str(DIST), "--workpath", str(BUILD), "--specpath", str(SPEC),
        "--paths", str(RAIZ),
    ]
    for modulo in EXCLUIR:
        args += ["--exclude-module", modulo]
    for paquete in RECOGER_TODO:
        args += ["--collect-all", paquete]
    for modulo in IMPORTACIONES_OCULTAS:
        args += ["--hidden-import", modulo]
    args.append(str(ENTRADA))
    resultado = subprocess.run(args, cwd=str(RAIZ))
    if resultado.returncode != 0 or not (CARPETA / f"{NOMBRE}.exe").exists():
        print("PyInstaller falló; no se ha generado el ejecutable.")
        return 1

    print("Copiando letras, licencias y LEEME...")
    shutil.copytree(RAIZ / "fonts", CARPETA / "fonts")
    shutil.copyfile(RAIZ / "LICENSE", CARPETA / "LICENSE.txt")
    _texto_windows(RAIZ / "distribucion" / "LEEME.txt", CARPETA / "LEEME.txt")

    print("Comprimiendo...")
    with zipfile.ZipFile(ZIP, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for archivo in sorted(CARPETA.rglob("*")):
            if archivo.is_file():
                zf.write(archivo, arcname=f"{NOMBRE}/{archivo.relative_to(CARPETA).as_posix()}")

    print()
    print(f"Carpeta: {CARPETA}  ({_mb(_tamano(CARPETA))})")
    print(f"Zip:     {ZIP}  ({_mb(ZIP.stat().st_size)})")
    print(f"Ejecutable: {CARPETA / (NOMBRE + '.exe')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

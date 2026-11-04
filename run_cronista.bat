@echo off
rem Lanza Cronista (teleprompter) con el entorno virtual del proyecto. Usa pythonw para no abrir consola.
rem Para el asistente de notas por voz: run_cronista.bat --notas (necesita todo requirements.txt).
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" -m cronista %*
) else (
    echo No se encontro .venv. Crea el entorno: python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements-teleprompter.txt
    pause
)
endlocal

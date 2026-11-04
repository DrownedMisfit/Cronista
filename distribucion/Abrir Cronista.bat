@echo off
rem Doble clic para abrir Cronista (teleprompter).
rem La primera vez prepara todo solo: si hace falta instala Python 3.12 y la interfaz (PySide6). Necesita internet.
setlocal
cd /d "%~dp0"
title Cronista

rem Si se abre desde dentro del zip, Windows lo ejecuta en una carpeta temporal y no se guardaria nada.
echo %~dp0 | find /i "\Temp\" >nul && (
    echo Primero descomprime el zip: clic derecho sobre Cronista.zip ^> Extraer todo.
    echo Despues abre "Abrir Cronista.bat" desde la carpeta descomprimida.
    pause
    exit /b 1
)

if exist ".venv\Scripts\pythonw.exe" goto abrir

echo ==========================================================
echo   Cronista - primera vez: preparando todo
echo   Solo pasa esta vez. Hace falta internet (unos 100 MB).
echo ==========================================================
echo.

set "PYEXE="
set "PYARG="
py -3.12 -c "import sys" >nul 2>nul && (set "PYEXE=py" & set "PYARG=-3.12")
if not defined PYEXE py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && (set "PYEXE=py" & set "PYARG=-3")
if not defined PYEXE python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PYEXE=python"
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if defined PYEXE goto entorno

echo No se encontro Python en esta computadora.
echo Se va a instalar Python 3.12 (gratuito, de python.org) solo para este usuario...
echo.
where winget >nul 2>nul || goto sin_winget
winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    goto entorno
)
goto sin_python

:entorno
echo.
echo Preparando Cronista...
"%PYEXE%" %PYARG% -m venv .venv || goto error
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requisitos.txt || goto error
echo.
echo Listo. Desde ahora Cronista se abre directamente.
if /i "%~1"=="--solo-instalar" exit /b 0

:abrir
start "" ".venv\Scripts\pythonw.exe" -m cronista
exit /b 0

:sin_winget
echo Esta computadora no tiene el instalador de Windows (winget).
:sin_python
echo.
echo Instala Python 3.12 desde https://www.python.org/downloads/ (marca "Add python.exe to PATH")
echo y vuelve a abrir "Abrir Cronista.bat".
pause
exit /b 1

:error
echo.
echo Algo fallo al preparar Cronista. Revisa la conexion a internet y vuelve a intentarlo.
if exist ".venv" rmdir /s /q ".venv"
pause
exit /b 1

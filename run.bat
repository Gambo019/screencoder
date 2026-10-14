@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\pythonw.exe" goto run

echo Setting up ScreenCoder (first run)...
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do set "PY="%%D\python.exe""
if not defined PY (
    echo Python 3.10+ was not found. Install it from https://www.python.org/downloads/ and run this again.
    pause
    exit /b 1
)
%PY% -m venv .venv || (pause & exit /b 1)
".venv\Scripts\python.exe" -m pip install -r requirements.txt || (pause & exit /b 1)

:run
start "" ".venv\Scripts\pythonw.exe" -m screencoder

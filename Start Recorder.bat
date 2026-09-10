@echo off
setlocal
cd /d "%~dp0"

if exist "MeetingSystemAudioRecorder.exe" (
    start "" "MeetingSystemAudioRecorder.exe"
    exit /b 0
)

where python >nul 2>nul
if errorlevel 1 (
    echo Python 3 is required but was not found.
    echo Install Python from https://www.python.org/downloads/windows/ and try again.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\pythonw.exe" (
    echo First-time setup: creating a private Python environment...
    python -m venv .venv
    if errorlevel 1 goto :setup_failed

    echo Installing the audio libraries...
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
    if errorlevel 1 goto :setup_failed
)

start "" ".venv\Scripts\pythonw.exe" recorder.py
exit /b 0

:setup_failed
echo.
echo Setup failed. Check the error above, then run this file again.
pause
exit /b 1

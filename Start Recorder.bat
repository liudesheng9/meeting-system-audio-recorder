@echo off
setlocal
pushd "%~dp0" || (
    echo Could not open the folder that contains this file.
    pause
    exit /b 1
)

if exist "MeetingSystemAudioRecorder.exe" (
    start "" "MeetingSystemAudioRecorder.exe"
    popd
    exit /b 0
)

rem Source mode: setup counts as complete only after the libraries import.
if not exist ".venv\setup-complete" (
    if not exist ".venv\Scripts\python.exe" (
        where python >nul 2>nul
        if errorlevel 1 (
            echo Python 3.10 or newer is required but was not found.
            echo Install Python from https://www.python.org/downloads/windows/ and try again.
            goto :fail
        )
        echo First-time setup: creating a private Python environment...
        python -m venv .venv
        if errorlevel 1 goto :setup_failed
    )
    echo Installing the audio libraries...
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
    if errorlevel 1 goto :setup_failed
    ".venv\Scripts\python.exe" -c "import recorder"
    if errorlevel 1 goto :setup_failed
    echo ok> ".venv\setup-complete"
)

start "" ".venv\Scripts\pythonw.exe" recorder.py
popd
exit /b 0

:setup_failed
echo.
echo Setup failed. Check the error above, then run this file again.
:fail
pause
popd
exit /b 1

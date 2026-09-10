@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    where python >nul 2>nul
    if errorlevel 1 (
        echo Python 3 is required for source-mode diagnostics but was not found.
        pause
        exit /b 1
    )
    echo Creating a private Python environment for diagnostics...
    python -m venv .venv
    if errorlevel 1 goto :setup_failed
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
    if errorlevel 1 goto :setup_failed
)

".venv\Scripts\python.exe" recorder.py --self-test
echo.
pause
exit /b 0

:setup_failed
echo.
echo Diagnostic setup failed. Check the error above, then try again.
pause
exit /b 1

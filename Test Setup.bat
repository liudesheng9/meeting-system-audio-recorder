@echo off
setlocal
pushd "%~dp0" || (
    echo Could not open the folder that contains this file.
    pause
    exit /b 1
)

rem Diagnoses the source (Python) setup. The packaged .exe is checked by build.ps1.
if not exist ".venv\setup-complete" (
    if not exist ".venv\Scripts\python.exe" (
        where python >nul 2>nul
        if errorlevel 1 (
            echo Python 3.10 or newer is required for source-mode diagnostics but was not found.
            goto :fail
        )
        echo Creating a private Python environment for diagnostics...
        python -m venv .venv
        if errorlevel 1 goto :setup_failed
    )
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
    if errorlevel 1 goto :setup_failed
    ".venv\Scripts\python.exe" -c "import recorder"
    if errorlevel 1 goto :setup_failed
    echo ok> ".venv\setup-complete"
)

".venv\Scripts\python.exe" recorder.py --self-test
set "RESULT=%ERRORLEVEL%"
echo.
if "%RESULT%"=="0" (echo Setup check passed.) else (echo Setup check FAILED with exit code %RESULT%.)
pause
popd
exit /b %RESULT%

:setup_failed
echo.
echo Diagnostic setup failed. Check the error above, then try again.
:fail
pause
popd
exit /b 1

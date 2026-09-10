$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$buildEnvironment = Join-Path $repoRoot ".build-venv"
$buildWork = Join-Path $repoRoot ".build"
$buildPython = Join-Path $buildEnvironment "Scripts\python.exe"

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python 3 was not found on PATH."
}

if (-not (Test-Path -LiteralPath $buildPython)) {
    python -m venv $buildEnvironment
}

& $buildPython -m pip install --disable-pip-version-check -r (Join-Path $repoRoot "requirements.txt") "pyinstaller==6.22.2"
& $buildPython -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name "MeetingSystemAudioRecorder" `
    --distpath $repoRoot `
    --workpath $buildWork `
    --specpath $buildWork `
    (Join-Path $repoRoot "recorder.py")

Write-Host "Built: $(Join-Path $repoRoot 'MeetingSystemAudioRecorder.exe')"

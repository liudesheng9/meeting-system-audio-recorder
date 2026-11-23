$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$buildEnvironment = Join-Path $repoRoot ".build-venv"
$buildWork = Join-Path $repoRoot ".build"
$stagingDist = Join-Path $buildWork "dist"
$buildPython = Join-Path $buildEnvironment "Scripts\python.exe"
$exeName = "MeetingSystemAudioRecorder.exe"

# Native programs do not stop the script on failure; check every exit code.
function Invoke-Checked {
    param([string]$Description, [scriptblock]$Command)
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

if (-not (Test-Path -LiteralPath $buildPython)) {
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
        throw "Python 3.10 or newer was not found on PATH."
    }
    Invoke-Checked "Creating the build environment" { python -m venv $buildEnvironment }
}

Invoke-Checked "Installing build dependencies" {
    & $buildPython -m pip install --disable-pip-version-check -r (Join-Path $repoRoot "requirements.txt") "pyinstaller==6.22.2"
}

if (Test-Path -LiteralPath $stagingDist) {
    Remove-Item -LiteralPath $stagingDist -Recurse -Force
}

Invoke-Checked "PyInstaller" {
    & $buildPython -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --name "MeetingSystemAudioRecorder" `
        --distpath $stagingDist `
        --workpath $buildWork `
        --specpath $buildWork `
        (Join-Path $repoRoot "recorder.py")
}

$stagedExe = Join-Path $stagingDist $exeName
if (-not (Test-Path -LiteralPath $stagedExe)) {
    throw "PyInstaller did not produce $stagedExe."
}

# Smoke-test the new executable: record two seconds and finalize a WAV file.
$smokeDir = Join-Path $buildWork "smoke-test"
if (Test-Path -LiteralPath $smokeDir) {
    Remove-Item -LiteralPath $smokeDir -Recurse -Force
}
$smoke = Start-Process -FilePath $stagedExe -ArgumentList @("--record-seconds", "2", "--output-dir", "`"$smokeDir`"") -PassThru
if (-not $smoke.WaitForExit(90000)) {
    # A one-file build runs as a bootloader plus a child process: end the whole tree.
    & taskkill.exe /PID $smoke.Id /T /F | Out-Null
    throw "The built executable did not finish its recording smoke test within 90 seconds."
}
if ($smoke.ExitCode -ne 0) {
    throw "The built executable failed its recording smoke test (exit code $($smoke.ExitCode))."
}
if (-not (Get-ChildItem -LiteralPath $smokeDir -Filter "*.wav" -ErrorAction SilentlyContinue)) {
    throw "The built executable did not write a WAV file during the smoke test."
}
Remove-Item -LiteralPath $smokeDir -Recurse -Force

# Replace the distributed executable only after the new one has passed. Copy
# next to it first, then swap, so an interrupted copy never damages the old one.
$target = Join-Path $repoRoot $exeName
$incoming = "$target.new"
Copy-Item -LiteralPath $stagedExe -Destination $incoming -Force
if (Test-Path -LiteralPath $target) {
    [System.IO.File]::Replace($incoming, $target, [NullString]::Value)
} else {
    Move-Item -LiteralPath $incoming -Destination $target
}
Write-Host "Built and verified: $(Join-Path $repoRoot $exeName)"

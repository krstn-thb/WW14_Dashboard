$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = Join-Path $ProjectDir ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    $Launcher = $null
    $LauncherArgs = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $Launcher = "py"
        $LauncherArgs = @("-3")
    } elseif (Get-Command python3 -ErrorAction SilentlyContinue) {
        $Launcher = "python3"
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        $Launcher = "python"
    } else {
        $BundledPython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
        if (Test-Path $BundledPython) {
            $Launcher = $BundledPython
        }
    }

    if (-not $Launcher) {
        throw "Python 3 wurde nicht gefunden. Bitte Python 3.11 oder neuer installieren."
    }

    & $Launcher @LauncherArgs -m venv (Join-Path $ProjectDir ".venv")
    & $Python -m pip install --upgrade pip
    & $Python -m pip install -r (Join-Path $ProjectDir "requirements.txt")
}

if (-not (Test-Path (Join-Path $ProjectDir ".env"))) {
    Copy-Item (Join-Path $ProjectDir ".env.example") (Join-Path $ProjectDir ".env")
}

Set-Location $ProjectDir
& $Python -m uvicorn app.main:app --host 0.0.0.0 --port 8080


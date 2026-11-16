# Run from source on Windows (dev only): .\run-dev.ps1 [files...]
# The first run makes .venv with PySide6, mutagen, pyusb and libusb-package.
# $env:XLTTOOL_PYTHON picks the Python that makes it (default: py -3, else python).
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$venv = Join-Path $here '.venv'
$py = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path $py)) {
    if ($env:XLTTOOL_PYTHON) { & $env:XLTTOOL_PYTHON -m venv $venv }
    elseif (Get-Command py -ErrorAction SilentlyContinue) { & py -3 -m venv $venv }
    else { & python -m venv $venv }
    & $py -m pip install --upgrade pip
    & $py -m pip install PySide6-Essentials mutagen pyusb libusb-package
}
$env:PYTHONPATH = "$here;$(Join-Path $here 'dev')"
& $py -m xlttool @args
exit $LASTEXITCODE

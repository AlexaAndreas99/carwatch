# One-time setup for CarWatch on a Windows machine.
#
#   .\setup.ps1
#
# Creates the virtual environment, installs the dependencies and the Chromium
# that olx.ro needs, and leaves you a config.yaml to fill in. Safe to run
# again: whatever is already in place is left alone.
#
# -SkipBrowser installs no Chromium. autovit and mobile.de still work; olx
# does not, because CloudFront refuses plain HTTP requests.

[CmdletBinding()]
param([switch]$SkipBrowser)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Write-Output "CarWatch setup - $root"
Write-Output ""

# --------------------------------------------------------------- Python
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) {
    Write-Error "Python is not on PATH. Install Python 3.11 or newer from https://www.python.org/downloads/ - tick 'Add python.exe to PATH' in the installer - then run this again."
}

$version = & $python -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ([version]$version -lt [version]"3.11") {
    Write-Error "Found Python $version, but CarWatch needs 3.11 or newer."
}
Write-Output "Python $version - ok"

# ----------------------------------------------------- virtual environment
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
if (Test-Path $venvPython) {
    Write-Output "Virtual environment already there."
} else {
    Write-Output "Creating the virtual environment (.venv)..."
    & $python -m venv .venv
}

Write-Output "Installing dependencies..."
& $venvPython -m pip install --upgrade pip --quiet
& $venvPython -m pip install -r requirements.txt --quiet
Write-Output "Dependencies - ok"

# ------------------------------------------------------------- Chromium
# Into the project rather than %LOCALAPPDATA%: a scheduled task cannot reliably
# read the per-user location (see carwatch/adapters/browser.py).
if ($SkipBrowser) {
    Write-Output "Skipping Chromium (-SkipBrowser). olx will not collect."
} else {
    Write-Output "Installing Chromium for olx (a few hundred MB, once)..."
    $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $root ".playwright"
    & $venvPython -m playwright install chromium
    Write-Output "Chromium - ok"
}

# --------------------------------------------------------------- config
$config = Join-Path $root "config.yaml"
$fresh = -not (Test-Path $config)
if ($fresh) {
    Copy-Item (Join-Path $root "config.example.yaml") $config
    Write-Output "Created config.yaml from the example."
} else {
    Write-Output "config.yaml already there - left alone."
}

Write-Output ""
Write-Output "Done. Next:"
if ($fresh) {
    Write-Output "  1. Open config.yaml and paste your search URLs over PASTE_URL_HERE."
    Write-Output "  2. .\install-shortcut.ps1     # puts a CarWatch icon on the Desktop"
    Write-Output "  3. Double-click the icon, or run .\open-carwatch.ps1"
} else {
    Write-Output "  .\install-shortcut.ps1        # puts a CarWatch icon on the Desktop"
    Write-Output "  Double-click the icon, or run .\open-carwatch.ps1"
}
Write-Output ""
Write-Output "A schedule is optional and off by default - set it on the Runs page."

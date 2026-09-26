# Open the CarWatch dashboard: start it if it isn't running, then open the browser.
#
#   .\open-carwatch.ps1              # start if needed, open the browser
#   .\open-carwatch.ps1 -NoBrowser   # start if needed, and nothing else
#
# The work is done by carwatch\launch.py, which the desktop icon runs directly
# with pythonw (see install-shortcut.ps1) - going through PowerShell flashed a
# console window on every double-click. This script is the same thing for
# typing at a prompt.
#
# The dashboard runs with no window, writing to logs\dashboard.log, and stops
# by itself once no CarWatch page has been open for five minutes. A collection
# in progress finishes first. dashboard.bat is the other way in: a visible
# window that runs until closed.

[CmdletBinding()]
param(
    [int]$Port = 8009,
    [int]$IdleMinutes = 5,
    [switch]$NoBrowser
)

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$launchArgs = @("-m", "carwatch.launch", "--port", $Port, "--idle-minutes", $IdleMinutes)
if ($NoBrowser) { $launchArgs += "--no-browser" }

Push-Location $root
try {
    & (Join-Path $root ".venv\Scripts\python.exe") @launchArgs
    exit $LASTEXITCODE
} finally {
    Pop-Location
}

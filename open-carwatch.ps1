# Open the CarWatch dashboard: start it if it isn't running, then open the browser.
#
# This is what the desktop icon runs (see install-shortcut.ps1).
#
#   .\open-carwatch.ps1              # start if needed, open the browser
#   .\open-carwatch.ps1 -NoBrowser   # start if needed, and nothing else
#
# The dashboard runs in its own minimised window, "CarWatch dashboard".
# Closing that window stops it. Double-clicking the icon again while it runs
# just opens the browser — it never starts a second copy.

[CmdletBinding()]
param(
    [int]$Port = 8009,
    [switch]$NoBrowser
)

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$url = "http://127.0.0.1:$Port/"

function Test-Dashboard {
    try {
        Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2 | Out-Null
        return $true
    } catch {
        return $false
    }
}

if (-not (Test-Dashboard)) {
    Start-Process -FilePath (Join-Path $root "dashboard.bat") -WorkingDirectory $root -WindowStyle Minimized

    # Wait for it to answer before opening the browser, so the first page is
    # the dashboard and not a "can't connect" error. Twenty seconds is plenty;
    # it normally takes one or two.
    $ready = $false
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 500
        if (Test-Dashboard) { $ready = $true; break }
    }
    if (-not $ready) {
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.MessageBox]::Show(
            "The CarWatch dashboard did not start. Open the 'CarWatch dashboard' window on the taskbar to see why.",
            "CarWatch") | Out-Null
        exit 1
    }
}

if (-not $NoBrowser) { Start-Process $url }

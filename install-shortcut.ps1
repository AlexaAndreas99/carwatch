# Put a CarWatch icon on the desktop (and in the Start menu) that opens the dashboard.
#
#   .\install-shortcut.ps1           # create or refresh the icons
#   .\install-shortcut.ps1 -Remove   # take them away again
#
# The icon runs open-carwatch.ps1: start the dashboard if it isn't running,
# then open it in the browser. Re-run this after moving the project folder.

[CmdletBinding()]
param([switch]$Remove)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

$places = @(
    [Environment]::GetFolderPath("Desktop"),
    [Environment]::GetFolderPath("Programs")   # the Start menu
)

foreach ($folder in $places) {
    $link = Join-Path $folder "CarWatch.lnk"

    if ($Remove) {
        if (Test-Path $link) { Remove-Item $link; Write-Output "Removed $link" }
        continue
    }

    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($link)
    $shortcut.TargetPath = "powershell.exe"
    $shortcut.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$root\open-carwatch.ps1`""
    $shortcut.WorkingDirectory = $root
    $shortcut.IconLocation = "$root\carwatch.ico,0"
    $shortcut.Description = "Open the CarWatch dashboard"
    # Minimised, so the PowerShell that starts things does not flash up.
    $shortcut.WindowStyle = 7
    $shortcut.Save()
    Write-Output "Created $link"
}

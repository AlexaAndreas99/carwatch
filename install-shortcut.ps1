# Put a CarWatch icon on the desktop (and in the Start menu) that opens the dashboard.
#
#   .\install-shortcut.ps1           # create or refresh the icons
#   .\install-shortcut.ps1 -Remove   # take them away again
#
# The icon runs carwatch\launch.py with pythonw: start the dashboard if it
# isn't running, then open it in the browser. pythonw rather than PowerShell,
# which flashes a console window before it can hide it. Re-run this after
# moving the project folder, and once after updating from a version whose icon
# ran open-carwatch.ps1.

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
    $shortcut.TargetPath = "$root\.venv\Scripts\pythonw.exe"
    $shortcut.Arguments = "-m carwatch.launch"
    $shortcut.WorkingDirectory = $root
    $shortcut.IconLocation = "$root\carwatch.ico,0"
    $shortcut.Description = "Open the CarWatch dashboard"
    $shortcut.Save()
    Write-Output "Created $link"
}

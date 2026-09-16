# CarWatch collector wrapper (Windows).
#
# Activates the local venv, runs a collection, and appends everything to a log
# file. This is what Task Scheduler should call, so a scheduled entry is one
# line and its output isn't lost to an invisible console.
#
#   .\run.ps1                                      # all enabled searches
#   .\run.ps1 --status                             # config + DB state
#   .\run.ps1 --all --site autovit
#   .\run.ps1 --search "Nissan Qashqai 2025 4x4 Tekna"
#
# Exit code is the collector's own: 0 all ok, 2 bad config,
# 5 at least one source blocked or errored.

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Error @"
No virtualenv at $python
Create it first:
    python -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -r requirements.txt
"@
}

# NOTE: assign inside the branches, not from an `if` expression.
# `$a = if (...) { $args }` unrolls a single-element array into a bare string,
# and `@a` then splats that string one CHARACTER at a time - turning
# `--status` into `- - s t a t u s`. Wrapping each branch in @() keeps it an
# array.
if ($args.Count -gt 0) {
    $carwatchArgs = @($args)
} else {
    $carwatchArgs = @("--all")
}

# --jitter <minutes>: wait a random 0..N minutes before collecting. Consumed
# here, never passed to Python.
#
# A scheduled job firing at exactly 08:00:00.000 every single day is the most
# machine-looking traffic pattern there is, and it also means every CarWatch
# user hits the same sites at the same instant. Spreading the start is politer
# and costs nothing, since nobody is watching for the result at 08:00 sharp.
#
# --scheduled: this run was started by the schedule (schedule.ps1 registers the
# task with it). Also consumed here. The task runs without a window, through
# `conhost.exe --headless`, and conhost reports exit code 0 whatever happened —
# so a scheduled run writes its real result to logs\last-scheduled-run.json,
# which is where the dashboard's schedule panel reads it.
$jitterMinutes = 0
$scheduled = $false
$filtered = @()
for ($i = 0; $i -lt $carwatchArgs.Count; $i++) {
    if ($carwatchArgs[$i] -eq "--jitter" -and ($i + 1) -lt $carwatchArgs.Count) {
        $jitterMinutes = [int]$carwatchArgs[$i + 1]
        $i++
    } elseif ($carwatchArgs[$i] -eq "--scheduled") {
        $scheduled = $true
    } else {
        $filtered += $carwatchArgs[$i]
    }
}
$carwatchArgs = @($filtered)
if ($carwatchArgs.Count -eq 0) { $carwatchArgs = @("--all") }

# Task Scheduler starts in %SystemRoot%\system32 unless told otherwise, so move
# to the project root to make `config.yaml` (and its relative db_path) resolve.
Set-Location $root

$logDir = Join-Path $root "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$logFile = Join-Path $logDir ("collect-{0}.log" -f (Get-Date -Format "yyyy-MM"))

# The collector emits UTF-8 (see carwatch/console.py). PowerShell decodes a
# child process's output using [Console]::OutputEncoding, which defaults to the
# OEM code page - so an em dash arrived in the log as garbled bytes. Tell the
# host to read UTF-8 instead.
$previousEncoding = [Console]::OutputEncoding
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
# Culture-free: the dashboard parses these, and not every locale uses ":".
$invariant = [System.Globalization.CultureInfo]::InvariantCulture
$startedIso = (Get-Date).ToString("yyyy-MM-ddTHH:mm:ss", $invariant)
Add-Content -Path $logFile -Value "`n===== $stamp  run.ps1 $($carwatchArgs -join ' ') =====" -Encoding utf8

$wait = 0
if ($jitterMinutes -gt 0) {
    $wait = Get-Random -Minimum 0 -Maximum ($jitterMinutes * 60)
}

# A scheduled run says when it will actually collect, the moment it has picked
# its random delay, so the dashboard can show "collecting at 19:07" rather than
# only a twenty-minute window. Removed again when the run finishes.
$currentRun = Join-Path $logDir "scheduled-run.json"
if ($scheduled) {
    [ordered]@{
        started    = $startedIso
        collect_at = (Get-Date).AddSeconds($wait).ToString("yyyy-MM-ddTHH:mm:ss", $invariant)
    } | ConvertTo-Json -Compress | Set-Content -Path $currentRun -Encoding utf8
}

if ($wait -gt 0) {
    Add-Content -Path $logFile -Value "jitter: sleeping $wait s before collecting" -Encoding utf8
    Start-Sleep -Seconds $wait
}

# Tee so an interactive run still prints, while a scheduled run leaves a trace.
#
# $ErrorActionPreference MUST drop to Continue around this call. Under "Stop",
# PowerShell 5.1 wraps every stderr line from a native command in a terminating
# NativeCommandError - so the collector's own log warnings (e.g. autovit
# relaxing a search) abort the wrapper mid-run with exit 1 and an empty log.
# That broke every scheduled `--all` run while `--status` looked fine, because
# --status writes nothing to stderr.
#
# $LASTEXITCODE stays reliable here; $? does not, which is why it isn't used.
#
# Tee-Object is NOT used: in PowerShell 5.1 it has no -Encoding parameter and
# writes UTF-16LE, which mixed with the UTF-8 headers above produced a log full
# of "i n s t a l l e d" spacing. Writing each line ourselves keeps the file
# UTF-8 throughout and still streams to the console for interactive runs.
$ErrorActionPreference = "Continue"
& $python -m carwatch.collect @carwatchArgs 2>&1 | ForEach-Object {
    $line = $_.ToString()
    Write-Output $line
    Add-Content -Path $logFile -Value $line -Encoding utf8
}
$code = $LASTEXITCODE

Add-Content -Path $logFile -Value "----- exit code $code -----" -Encoding utf8

if ($scheduled) {
    [ordered]@{
        started   = $startedIso
        finished  = (Get-Date).ToString("yyyy-MM-ddTHH:mm:ss", $invariant)
        exit_code = $code
    } | ConvertTo-Json -Compress | Set-Content -Path (Join-Path $logDir "last-scheduled-run.json") -Encoding utf8
    Remove-Item -Path $currentRun -ErrorAction SilentlyContinue
}

try { [Console]::OutputEncoding = $previousEncoding } catch { }
exit $code

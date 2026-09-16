# Register (or remove) a scheduled CarWatch collection in Windows Task Scheduler.
#
#   .\schedule.ps1                      # daily at 08:00
#   .\schedule.ps1 -At 19:30            # daily at 19:30
#   .\schedule.ps1 -At 08:00 -EveryHours 6    # 08:00, 14:00, 20:00, 02:00
#   .\schedule.ps1 -Status              # show the registered task and its last result
#   .\schedule.ps1 -Status -Json        # the same, for the dashboard's schedule panel
#   .\schedule.ps1 -Remove              # unregister it
#
# The dashboard's Runs page calls this script too, so the panel and the command
# line always mean the same thing by "a schedule".
#
# Registers under the current user and runs whether or not you are logged in
# is deliberately NOT set: that would require storing your password. The task
# runs when you are logged on, which suits a personal machine. Pass -WakeToRun
# if you want it to wake the machine.
#
# -EveryHours is 6, 8 or 12 — nothing more often. mobile.de, whose robots.txt
# disallows the route CarWatch uses, was accepted on the basis of low volume.

[CmdletBinding()]
param(
    [string]$At = "08:00",
    [int]$EveryHours = 0,
    [string]$TaskName = "CarWatch scheduled collection",
    [int]$JitterMinutes = 20,
    [switch]$Remove,
    [switch]$Status,
    [switch]$Json,
    [switch]$WakeToRun
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
# Dates and times are written and read culture-free: in some locales ":" is not
# the time separator, and the dashboard parses what this prints.
$invariant = [System.Globalization.CultureInfo]::InvariantCulture

function Get-Task {
    Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}

if ($Status) {
    $task = Get-Task

    if ($Json) {
        if (-not $task) {
            '{"registered": false}'
            exit 0
        }
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        $trigger = $task.Triggers | Select-Object -First 1

        $hours = 0
        if ($trigger.Repetition -and $trigger.Repetition.Interval -match '^PT(\d+)H$') {
            $hours = [int]$Matches[1]
        }
        $startAt = ""
        if ($trigger.StartBoundary) {
            $startAt = ([datetime]::Parse($trigger.StartBoundary, $invariant)).ToString("HH:mm", $invariant)
        }
        $next = $null
        if ($info.NextRunTime) {
            $next = $info.NextRunTime.ToString("yyyy-MM-ddTHH:mm:ss", $invariant)
        }
        # A task that has never run reports 30 Nov 1999.
        $last = $null
        if ($info.LastRunTime -and $info.LastRunTime.Year -gt 2000) {
            $last = $info.LastRunTime.ToString("yyyy-MM-ddTHH:mm:ss", $invariant)
        }

        # The task's own result is conhost's, which is always 0 (see the
        # action below). Unless the task is running or has never run, the real
        # one is what run.ps1 recorded.
        $result = $info.LastTaskResult
        $record = Join-Path $root "logs\last-scheduled-run.json"
        if ($result -ne 267009 -and $result -ne 267011 -and (Test-Path $record)) {
            try {
                $result = [int]((Get-Content $record -Raw | ConvertFrom-Json).exit_code)
            } catch { }
        }

        # Registered by an older CarWatch, which ran powershell.exe directly
        # and so opened a window on every run. Saving again replaces it.
        $headless = [bool]($task.Actions | Where-Object { $_.Execute -match 'conhost' })

        [ordered]@{
            registered  = $true
            headless    = $headless
            state       = "$($task.State)"
            at          = $startAt
            every_hours = $hours
            next_run    = $next
            last_run    = $last
            last_result = $result
        } | ConvertTo-Json -Compress
        exit 0
    }

    if (-not $task) {
        Write-Output "No task named '$TaskName' is registered."
        Write-Output "Register it with:  .\schedule.ps1 -At 08:00"
        exit 0
    }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Output "Task     : $TaskName"
    Write-Output "State    : $($task.State)"
    Write-Output "Action   : $($task.Actions[0].Execute) $($task.Actions[0].Arguments)"
    Write-Output "Last run : $($info.LastRunTime)"
    Write-Output "Last code: $($info.LastTaskResult)   (0 ok, 5 a source was blocked or errored)"
    Write-Output "Next run : $($info.NextRunTime)"
    Write-Output ""
    Write-Output "Logs     : $root\logs\"
    exit 0
}

if ($Remove) {
    if (Get-Task) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Output "Removed scheduled task '$TaskName'."
    } else {
        Write-Output "No task named '$TaskName' to remove."
    }
    exit 0
}

# ---------------------------------------------------------------- register

try {
    $when = [datetime]::ParseExact($At, "HH:mm", $invariant)
} catch {
    Write-Error "Could not read -At '$At'. Use 24-hour HH:mm, e.g. 08:00 or 19:30."
}

if ($EveryHours -ne 0 -and @(6, 8, 12) -notcontains $EveryHours) {
    Write-Error "-EveryHours must be 6, 8 or 12 (got $EveryHours). Nothing more often than every 6 hours."
}

$runPs1 = Join-Path $root "run.ps1"
if (-not (Test-Path $runPs1)) { Write-Error "run.ps1 not found next to schedule.ps1 ($root)." }

# No window. A console popping up four times a day is easy to close by
# accident, which cancels the run. `conhost.exe --headless` runs the console
# without one and waits for it, so Task Scheduler still sees the run for as
# long as it lasts — but it reports exit code 0 whatever happened. Hence
# --scheduled: run.ps1 records the real result in logs\last-scheduled-run.json,
# and -Status reads it from there.
$action = New-ScheduledTaskAction `
    -Execute "conhost.exe" `
    -Argument "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$runPs1`" --all --jitter $JitterMinutes --scheduled" `
    -WorkingDirectory $root

$trigger = New-ScheduledTaskTrigger -Daily -At $when
if ($EveryHours -gt 0) {
    # A daily trigger that repeats every N hours for one day: 08:00 with 6
    # hours fires at 08, 14, 20 and 02, and the next day's trigger picks up at
    # 08 again. The repetition is borrowed from a -Once trigger, the only
    # cmdlet form that builds one.
    $repeat = New-ScheduledTaskTrigger -Once -At $when `
        -RepetitionInterval (New-TimeSpan -Hours $EveryHours) `
        -RepetitionDuration (New-TimeSpan -Days 1)
    $trigger.Repetition = $repeat.Repetition
}

$settingsArgs = @{
    # A collection can involve a real browser; give it room but don't let a
    # hung run linger until the next one.
    ExecutionTimeLimit          = (New-TimeSpan -Hours 2)
    StartWhenAvailable          = $true   # catch up if the machine was off
    DontStopIfGoingOnBatteries  = $true
    AllowStartIfOnBatteries     = $true
    MultipleInstances           = "IgnoreNew"
}
if ($WakeToRun) { $settingsArgs["WakeToRun"] = $true }
$settings = New-ScheduledTaskSettingsSet @settingsArgs

if (Get-Task) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

if ($EveryHours -gt 0) {
    $what = "every $EveryHours hours from $At"
} else {
    $what = "daily at $At"
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Collects CarWatch listings $what. Logs to $root\logs\." | Out-Null

Write-Output "Registered '$TaskName' - $what (+0-$JitterMinutes min jitter)."
Write-Output "  runs   : run.ps1 --all --jitter $JitterMinutes --scheduled, with no window"
Write-Output "  logs   : $root\logs\"
Write-Output ""
Write-Output "Check it with :  .\schedule.ps1 -Status"
Write-Output "Run it now    :  Start-ScheduledTask -TaskName `"$TaskName`""
Write-Output "Remove it     :  .\schedule.ps1 -Remove"

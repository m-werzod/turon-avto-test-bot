<#
.SYNOPSIS
    Keep the bot running on this Windows machine across logins and crashes.

.DESCRIPTION
    Registers a Scheduled Task that starts the bot when you log in and restarts
    it if it stops. This is the best "always on" a desktop can offer — read the
    limits below before relying on it.

    What it does NOT survive:
      * the machine being switched off, asleep, or hibernating
      * losing power or internet

    A desktop is not a server. If posts must go out at 07:00 whether or not this
    PC is awake, deploy to a VPS instead — see deploy/README.md. This script is
    for running it from home in the meantime.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\install-windows-task.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\install-windows-task.ps1 -Remove
#>

[CmdletBinding()]
param(
    # Remove the task instead of creating it.
    [switch]$Remove,

    # Name the task appears under in Task Scheduler.
    [string]$TaskName = "TuronAvtoTestBot"
)

$ErrorActionPreference = "Stop"

# The repository root is this script's parent directory.
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$python = Join-Path $root ".venv\Scripts\python.exe"

if ($Remove) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'." -ForegroundColor Yellow
    }
    else {
        Write-Host "No scheduled task named '$TaskName'." -ForegroundColor Yellow
    }
    return
}

# --- sanity checks before registering anything -------------------------------
if (-not (Test-Path $python)) {
    throw "Python not found at $python. Create the virtualenv first: python -m venv .venv"
}
if (-not (Test-Path (Join-Path $root ".env"))) {
    throw "No .env in $root. Copy .env.example to .env and fill in BOT_TOKEN and ADMIN_IDS."
}

Write-Host "Repository: $root"
Write-Host "Python:     $python"

# -u keeps stdout unbuffered so the log file stays current rather than filling
# in 8 KB bursts, which matters when the log is how you diagnose a stuck bot.
$action = New-ScheduledTaskAction `
    -Execute $python `
    -Argument "-u -m bot" `
    -WorkingDirectory $root

$triggers = @(
    New-ScheduledTaskTrigger -AtLogOn
)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -RestartCount 999 `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -MultipleInstances IgnoreNew

# ExecutionTimeLimit 0 means "no limit": the default kills a task after three
# days, which would silently stop the bot mid-week.
# MultipleInstances IgnoreNew prevents a second copy starting: two pollers on one
# token fight over getUpdates and neither works reliably.

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Replaced the existing task." -ForegroundColor Yellow
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $triggers `
    -Settings $settings `
    -Description "Turon Avto Test bot - posts driving-test quizzes to Telegram channels." | Out-Null

Write-Host ""
Write-Host "Registered '$TaskName'." -ForegroundColor Green
Write-Host "  starts:   when you log in"
Write-Host "  restarts: every minute if it stops, indefinitely"
Write-Host ""
Write-Host "Start it now:   Start-ScheduledTask -TaskName $TaskName"
Write-Host "Check it:       Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo"
Write-Host "Stop it:        Stop-ScheduledTask -TaskName $TaskName"
Write-Host "Remove it:      powershell -File deploy\install-windows-task.ps1 -Remove"
Write-Host ""
Write-Host "Reminder: this only runs while the PC is on and you are logged in." -ForegroundColor Yellow
Write-Host "For posting that does not depend on this machine, see deploy\README.md."

# Run the WHOLE bot on this Windows PC (test phase, before the Google VM):
# paper-trading jobs every hour, the daily comparison, and the MT5 executor
# every 10 minutes, all sharing state\ledger.db.
#
#   powershell -ExecutionPolicy Bypass -File deploy\windows\setup_local.ps1
#   powershell -ExecutionPolicy Bypass -File deploy\windows\setup_local.ps1 -NoSchedule   # checks only
#
# Needs: Python 3.11, the MT5 terminal open and logged in with Algo Trading on.
# Scheduled tasks run while you are logged in and the PC is awake; a run that
# was missed catches up at the next one. Re-running replaces the tasks.

param(
    [string]$EnvFile = ".env",
    [switch]$NoSchedule
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Root
. (Join-Path $PSScriptRoot "common.ps1")
Write-Host "== project: $Root   env file: $EnvFile"

$Py = Initialize-BotPython $Root
if (Initialize-EnvFile $Root $EnvFile) {
    Write-Host "== created $EnvFile from deploy\windows\env.windows.example (TARGETS_SOURCE=ledger, FX_DATA_SOURCE=mt5)"
}
if ((Get-EnvValue $EnvFile "TARGETS_SOURCE" "ssh").ToLower() -ne "ledger") {
    Write-Host "$EnvFile has TARGETS_SOURCE other than 'ledger'. For the all-on-this-PC setup set TARGETS_SOURCE=ledger."
    exit 1
}
New-Item -ItemType Directory -Force -Path (Join-Path $Root "logs"), (Join-Path $Root "state"), (Join-Path $Root "data") | Out-Null

Write-Host "== harness validation (quick)"
& $Py validate_harness.py --quick | Select-Object -Last 3
if ($LASTEXITCODE -ne 0) { Write-Host "Harness validation failed -- not scheduling."; exit 1 }

Write-Host "== paper jobs (first run downloads history from MT5 and Binance; can take several minutes)"
& $Py -m live.run_jobs --env-file $EnvFile
Get-Content (Join-Path $Root "logs\run_jobs.log") -Tail 10 -ErrorAction SilentlyContinue

# The executor checks and its task (every 10 minutes)
$execArgs = @{ EnvFile = $EnvFile }
if ($NoSchedule) { $execArgs["NoSchedule"] = $true }
& (Join-Path $PSScriptRoot "setup_executor.ps1") @execArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if ($NoSchedule) { exit 0 }

$jobsCmd = Join-Path $Root "deploy\windows\run_jobs.cmd"
$next = (Get-Date).Date.AddHours((Get-Date).Hour + 1).AddMinutes(2)   # 2 minutes after each hour
$hourly = New-ScheduledTaskTrigger -Once -At $next -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
Register-HiddenTask "SignalBot Paper Jobs" @($jobsCmd) $hourly 55

# Daily comparison shortly after the FX/gold daily close (21:00-22:00 UTC).
$utcTarget = [DateTime]::UtcNow.Date.AddHours(22).AddMinutes(45)
$daily = New-ScheduledTaskTrigger -Daily -At $utcTarget.ToLocalTime()
Register-HiddenTask "SignalBot Daily Compare" @($jobsCmd, "--compare-only") $daily 55

Write-Host ""
Write-Host "Scheduled: SignalBot Paper Jobs (hourly at :02), SignalBot Daily Compare ($($utcTarget.ToLocalTime().ToString('HH:mm')) local), SignalBot MT5 Executor."
Write-Host "Logs:    $Root\logs\"
Write-Host "Control: .venv-windows\Scripts\python.exe -m live.control status"
Write-Host "Pause:   Get-ScheduledTask 'SignalBot*' | Disable-ScheduledTask"

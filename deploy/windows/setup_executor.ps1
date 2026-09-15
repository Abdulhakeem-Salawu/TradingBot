# Set up the MT5 executor on a Windows PC and schedule it every 10 minutes.
#
#   powershell -ExecutionPolicy Bypass -File deploy\windows\setup_executor.ps1
#   powershell -ExecutionPolicy Bypass -File deploy\windows\setup_executor.ps1 -EnvFile .env.live
#
# The first run creates the .env file (from env.windows.example) and stops so you
# can fill it in; the second tests everything and registers the scheduled task.
# A second executor for a live terminal uses its own .env file (EXECUTOR_MODE=live,
# MT5_TERMINAL_PATH, MT5_LOGIN) and gets its own task. Re-running replaces the
# task, never duplicates it. -NoSchedule runs the checks only.
#
# To run the paper jobs on this PC too (test phase), use setup_local.ps1 instead.

param(
    [string]$EnvFile = ".env",
    [int]$Minutes = 10,
    [string]$TaskName = "",
    [switch]$NoSchedule
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Root
. (Join-Path $PSScriptRoot "common.ps1")
Write-Host "== project: $Root   env file: $EnvFile"

$Py = Initialize-BotPython $Root
$created = Initialize-EnvFile $Root $EnvFile
if ($created) {
    Write-Host ""
    Write-Host "Created $EnvFile -- check EXECUTOR_MODE, TARGETS_SOURCE (and VM_SSH_TARGET for ssh), then run this script again."
    exit 0
}
$mode = (Get-EnvValue $EnvFile "EXECUTOR_MODE" "demo").ToLower()
$source = (Get-EnvValue $EnvFile "TARGETS_SOURCE" "ssh").ToLower()
if (-not $TaskName) { $TaskName = "SignalBot MT5 Executor ($mode)" }

if ($source -eq "ssh" -and -not (Get-Command ssh -ErrorAction SilentlyContinue)) {
    Write-Host "OpenSSH client not found: Settings > System > Optional features > Add 'OpenSSH Client', then re-run."
    exit 1
}

New-Item -ItemType Directory -Force -Path (Join-Path $Root "logs"), (Join-Path $Root "state") | Out-Null
Write-Host "== execution self-test (fake broker, no orders)"
& $Py -m live.selftest | Select-Object -Last 3
if ($LASTEXITCODE -ne 0) { Write-Host "Self-test failed -- do not schedule the executor."; exit 1 }

Write-Host "== MT5 connection and targets (MT5 must be open and logged in, Algo Trading on)"
& $Py -m live.mt5_executor --env-file $EnvFile --status
if ($LASTEXITCODE -ne 0) {
    Write-Host "Status check failed. Fix the message above (terminal open? Algo Trading on? right account type? VM_SSH_TARGET?) and re-run."
    exit 1
}
Write-Host "== dry run"
& $Py -m live.mt5_executor --env-file $EnvFile --dry-run

if ($NoSchedule) { Write-Host "Checks done (-NoSchedule: nothing registered)."; exit 0 }
$Cmd = Join-Path $Root "deploy\windows\run_executor.cmd"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $Minutes) -RepetitionDuration (New-TimeSpan -Days 3650)
Register-HiddenTask $TaskName @($Cmd, $EnvFile, $mode) $trigger ([Math]::Max(1, $Minutes - 1))

Write-Host ""
Write-Host "Scheduled '$TaskName' every $Minutes minutes."
Write-Host "Log:      $Root\logs\mt5_executor_$mode.log"
Write-Host "Pause:    Disable-ScheduledTask -TaskName '$TaskName'"
Write-Host "Stop now: create the file $Root\state\KILL (no orders while it exists)"

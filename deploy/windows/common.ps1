# Helpers shared by setup_executor.ps1 and setup_local.ps1 (dot-sourced, not run directly).

function Initialize-BotPython([string]$Root) {
    $Venv = Join-Path $Root ".venv-windows"
    $Py = Join-Path $Venv "Scripts\python.exe"
    if (-not (Test-Path $Py)) {
        $launcher = Get-Command py -ErrorAction SilentlyContinue
        $python = Get-Command python -ErrorAction SilentlyContinue
        Write-Host "== creating virtual environment"
        # The MetaTrader5 package ships wheels for specific Python versions; 3.11 is tested.
        if ($launcher) { & py -3.11 -m venv $Venv 2>$null }
        if (-not (Test-Path $Py)) {
            if (-not $python) {
                Write-Host "Python not found. Install Python 3.11 from python.org (tick 'Add python.exe to PATH') and re-run."
                exit 1
            }
            & python -m venv $Venv
        }
    }
    & $Py -m pip install --upgrade pip --quiet
    & $Py -m pip install -r (Join-Path $Root "deploy\windows\requirements-windows.txt") --quiet
    if ($LASTEXITCODE -ne 0) { Write-Host "pip install failed"; exit 1 }
    return $Py
}

function Initialize-EnvFile([string]$Root, [string]$EnvFile) {
    $path = Join-Path $Root $EnvFile
    if (Test-Path $path) { return $false }
    Copy-Item (Join-Path $Root "deploy\windows\env.windows.example") $path
    return $true
}

function Get-EnvValue([string]$EnvFile, [string]$Key, [string]$Default) {
    if (-not (Test-Path $EnvFile)) { return $Default }
    foreach ($line in Get-Content $EnvFile) {
        if ($line -match "^\s*$Key\s*=\s*([^#]*)") {
            $v = $Matches[1].Trim().Trim('"').Trim("'")
            if ($v) { return $v }
        }
    }
    return $Default
}

function Register-HiddenTask([string]$Name, [string[]]$CommandLine, $Trigger, [int]$LimitMinutes) {
    # wscript + hidden.vbs: no console window flashes on screen at each run. The task
    # runs in your logged-in session because the MT5 terminal is a desktop app.
    $vbs = Join-Path $PSScriptRoot "hidden.vbs"
    $quoted = ($CommandLine | ForEach-Object { '"' + $_ + '"' }) -join " "
    $action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$vbs`" $quoted"
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Minutes $LimitMinutes) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $Trigger -Settings $settings -Force | Out-Null
    Write-Host "   registered task: $Name"
}

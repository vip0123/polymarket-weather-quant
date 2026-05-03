# Weather trader supervisor for Windows.
# Keeps weather.trader alive — restarts on crash with a 3-second cooldown.
# Usage:  .\dashboard\supervise_weather.ps1
# Background: Start-Process powershell -ArgumentList "-File dashboard\supervise_weather.ps1" -WindowStyle Hidden

$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

$logDir = Join-Path $root "dashboard\runtime"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

$supervisorLog = Join-Path $logDir "supervisor_weather.log"

function Log($msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
    Write-Host $line
    Add-Content -Path $supervisorLog -Value $line
}

Log "Supervisor started. PID=$PID  root=$root"

while ($true) {
    Log "Starting weather.trader..."
    & uv run python -m weather.trader
    $exit = $LASTEXITCODE
    Log "weather.trader exited (code=$exit). Restarting in 3s..."
    Start-Sleep -Seconds 3
}

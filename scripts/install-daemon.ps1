# Registers APIPlatform daemon as a logon scheduled task and starts it now.
# No admin rights needed (current-user task). Uninstall: uninstall-daemon.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

$pyw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pyw) { $pyw = (Get-Command python.exe).Source }

$action  = New-ScheduledTaskAction -Execute $pyw -Argument 'daemon.py' -WorkingDirectory (Join-Path $root 'scripts')
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable
Register-ScheduledTask -TaskName "APIPlatformDaemon" -Action $action -Trigger $trigger `
    -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName "APIPlatformDaemon"
Write-Host "installed & started: APIPlatformDaemon (logon auto-start, crash auto-restart)"

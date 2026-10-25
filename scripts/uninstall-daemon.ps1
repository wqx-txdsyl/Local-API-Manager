# Stops and removes the APIPlatformDaemon scheduled task and its processes.
$ErrorActionPreference = "SilentlyContinue"
Stop-ScheduledTask -TaskName "APIPlatformDaemon"
Unregister-ScheduledTask -TaskName "APIPlatformDaemon" -Confirm:$false
# kill daemon + service processes
$pidfile = Join-Path (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)) "data\daemon.pid"
if (Test-Path $pidfile) {
    $dp = Get-Content $pidfile -ErrorAction SilentlyContinue
    if ($dp) { Stop-Process -Id $dp -Force -ErrorAction SilentlyContinue }
    Remove-Item $pidfile -Force -ErrorAction SilentlyContinue
}
Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object {
    $_.CommandLine -match 'main\.py' -and $_.CommandLine -notmatch 'daemon\.py'
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Write-Host "uninstalled."

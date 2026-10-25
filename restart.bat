@echo off
rem Restart APIPlatform service: kills main.py, daemon relaunches it in seconds
cd /d %~dp0
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8800 ^| findstr LISTENING') do taskkill /PID %%a /F >nul 2>&1
echo Restarting... daemon will relaunch in seconds. Logs: data\server.log

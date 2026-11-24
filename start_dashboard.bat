@echo off
chcp 65001 >nul
title WoW Forever - Cooking Dashboard
cd /d "%~dp0"

echo ============================================================
echo  WoW Forever - cooking dashboard  [EU PvP Horde]
echo  Site opens automatically: http://127.0.0.1:8080
echo  Stop the dashboard: close this window or press Ctrl+C
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 goto nopython

python "%~dp0dashboard_server.py" --port 8080
if errorlevel 1 goto failed
exit /b 0

:nopython
echo [ERROR] Python was not found in PATH.
echo Install Python 3.8+ and enable "Add Python to PATH".
pause
exit /b 1

:failed
echo.
echo Server exited with an error. See the messages above.
pause
exit /b 1

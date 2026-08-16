@echo off
powershell -NoProfile -WindowStyle Minimized -Command "Start-Sleep -Milliseconds 200"

cd %userprofile%\music-release-tracker
call python run.py


:check_server
timeout /t 2 >nul
curl -f -s -o nul http://127.0.0.1:7070
if %errorlevel% neq 0 goto :check_server
start http://127.0.0.1:7070
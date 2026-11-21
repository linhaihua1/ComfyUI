@echo off
setlocal
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0comfyui_launcher.ps1" -Install
pause
endlocal

@echo off
chcp 65001 >nul
cd /d "%~dp0"
"C:\Users\b2522\.workbuddy\binaries\python\envs\default\Scripts\python.exe" wechat_autoreply.py %*
pause

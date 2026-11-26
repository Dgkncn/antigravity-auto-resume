@echo off
chcp 65001 >nul
title 测试向 Antigravity 发送“接着执行”
cd /d "%~dp0"

set "PY_EXE="
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY_EXE=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PY_EXE (
    where py >nul 2>nul && set "PY_EXE=py"
)
if not defined PY_EXE set "PY_EXE=python"

echo 3秒后将自动定位 Antigravity 聊天框并测试发送一次“接着执行”...
timeout /t 3 >nul
"%PY_EXE%" agy_auto_resume_watcher.py --test-send
pause

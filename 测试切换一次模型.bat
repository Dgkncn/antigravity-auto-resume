@echo off
chcp 65001 >nul
title 测试在 Carbon / Argon / Hydrogen 之间轮换一次模型
cd /d "%~dp0"

set "PY_EXE="
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY_EXE=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PY_EXE (
    where py >nul 2>nul && set "PY_EXE=py"
)
if not defined PY_EXE set "PY_EXE=python"

echo 3秒后将自动点击 Antigravity 模型菜单并切换到下一个模型 (Carbon -^> Argon -^> Hydrogen)...
timeout /t 3 >nul
"%PY_EXE%" agy_auto_resume_watcher.py --test-rotate --no-restore-focus
pause

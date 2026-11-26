@echo off
chcp 65001 >nul
title Antigravity 误拦截自动“接着执行” + 三模型智能轮换守护程序
cd /d "%~dp0"

set "PY_EXE="
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY_EXE=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PY_EXE (
    where py >nul 2>nul && set "PY_EXE=py"
)
if not defined PY_EXE set "PY_EXE=python"

"%PY_EXE%" agy_auto_resume_watcher.py --retroactive %*
pause

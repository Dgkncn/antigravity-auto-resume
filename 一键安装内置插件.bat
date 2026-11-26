@echo off
chcp 65001 >nul
title 一键安装 Antigravity 内置 Auto-Resume 插件
cd /d "%~dp0"

set "PY_EXE="
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY_EXE=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PY_EXE (
    where py >nul 2>nul && set "PY_EXE=py"
)
if not defined PY_EXE set "PY_EXE=python"

"%PY_EXE%" install_plugin.py
echo.
echo 安装完成！现在可以双击【启动反重力自动接着执行.bat】开启后台监控与模型轮换。
pause

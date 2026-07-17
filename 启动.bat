@echo off
chcp 65001 >nul
cd /d %~dp0
title TokenLens - AI Token 消耗看板

python --version >nul 2>nul
if errorlevel 1 (
    py --version >nul 2>nul
    if errorlevel 1 (
        echo [错误] 未检测到 Python，请先安装：https://www.python.org/downloads/
        echo 安装时请勾选 "Add Python to PATH"
        pause
        exit /b 1
    )
    py server.py
) else (
    python server.py
)
pause

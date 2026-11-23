@echo off
cd /d "%~dp0"
title xm-touch
chcp 65001 >nul

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo  Python not found. Please install Python 3.8+ from https://www.python.org/downloads/
  echo  Remember to check "Add Python to PATH" during installation.
  echo.
  pause
  exit /b 1
)

python server.py %*
pause

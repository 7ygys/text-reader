@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 优先使用工作区内 .venv，其次系统 python
set "PYEXE=.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"

"%PYEXE%" main.py
pause

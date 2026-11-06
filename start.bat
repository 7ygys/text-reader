@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 后台运行使用 pythonw.exe（无控制台窗口）
set "PYEXE=.venv\Scripts\pythonw.exe"
if not exist "%PYEXE%" set "PYEXE=pythonw"

start "" "%PYEXE%" main.py

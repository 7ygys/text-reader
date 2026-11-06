@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo [1/2] 创建虚拟环境 .venv（依赖全部装在工作区内，不污染系统）
if exist ".venv\Scripts\python.exe" (
    echo      已存在 .venv，跳过创建
) else (
    python -m venv .venv
    if errorlevel 1 (
        echo [错误] 创建虚拟环境失败，请确认已安装 Python 3.8+ 并加入 PATH
        pause
        exit /b 1
    )
)

echo [2/2] 安装依赖（使用清华镜像源加速，pip 缓存放到工作区 .pip-cache）
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet --index-url https://pypi.tuna.tsinghua.edu.cn/simple
".venv\Scripts\python.exe" -m pip install ^
    --cache-dir "%~dp0.pip-cache" ^
    --index-url https://pypi.tuna.tsinghua.edu.cn/simple ^
    -r requirements.txt
if errorlevel 1 (
    echo [错误] 依赖安装失败
    pause
    exit /b 1
)

echo.
echo 依赖安装完成。运行方式：
echo   run.bat     前台运行（看日志）
echo   start.bat   后台静默运行
pause

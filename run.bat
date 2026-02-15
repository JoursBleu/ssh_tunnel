@echo off
chcp 65001 >nul
echo ================================
echo   SSH Tunnel VPN (Python + C)
echo ================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python
    pause
    exit /b 1
)

:: 安装依赖
pip install paramiko PySocks customtkinter -q 2>nul

:: 如果有C引擎就提示
if exist tun_relay.dll (
    echo [引擎] C 高性能引擎已加载
) else (
    echo [引擎] 使用 Python 模式 (可运行 build.bat 编译C引擎加速)
)
echo.

python main.py

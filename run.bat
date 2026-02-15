@echo off
chcp 65001 >nul
echo ================================
echo   SSH Tunnel VPN
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
echo.

python -m ssh_tunnel_vpn.main

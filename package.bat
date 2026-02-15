@echo off
chcp 65001 >nul
echo ========================================
echo   SSH Tunnel VPN - 打包脚本
echo ========================================
echo.

echo [1/2] 正在用 PyInstaller 生成 exe ...
pyinstaller ssh_tunnel_vpn.spec --noconfirm
if %errorlevel% neq 0 (
    echo 打包失败！
    pause
    exit /b 1
)

echo.
echo [2/2] 检查 Inno Setup ...
set ISCC=
if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" set ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe
if exist "C:\Program Files\Inno Setup 6\ISCC.exe" set ISCC=C:\Program Files\Inno Setup 6\ISCC.exe

if defined ISCC (
    echo 正在生成安装包 ...
    "%ISCC%" installer.iss
    if %errorlevel% neq 0 (
        echo 安装包生成失败！
        pause
        exit /b 1
    )
    echo.
    echo ✅ 安装包已生成到 installer_output\ 目录
) else (
    echo ⚠ 未检测到 Inno Setup 6，跳过安装包生成。
    echo   单文件 exe 已生成: dist\SSHTunnelVPN.exe
    echo   如需生成安装包，请安装 Inno Setup 6:
    echo   https://jrsoftware.org/isdl.php
)

echo.
echo ✅ 打包完成！
echo   单文件 exe: dist\SSHTunnelVPN.exe (%.1f MB)
for %%F in (dist\SSHTunnelVPN.exe) do echo   文件大小: %%~zF bytes
echo.
pause

@echo off
chcp 65001 >nul
echo ========================================
echo   SSH Tunnel VPN - 卸载脚本
echo ========================================
echo.

:: ── 1. 还原系统代理设置 ──
echo [1/4] 还原系统代理设置 ...
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyEnable /t REG_DWORD /d 0 /f >nul 2>&1
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyServer /f >nul 2>&1
echo       已关闭系统代理

:: ── 2. 删除配置文件 ──
echo [2/4] 删除配置文件 ...
set CONFIG_DIR=%APPDATA%\SSHTunnelVPN
if exist "%CONFIG_DIR%" (
    rmdir /s /q "%CONFIG_DIR%"
    echo       已删除 %CONFIG_DIR%
) else (
    echo       未找到配置目录，跳过
)

:: ── 3. 删除程序文件 ──
echo [3/4] 删除程序文件 ...
set EXE_PATH=%~dp0dist\SSHTunnelVPN.exe
if exist "%EXE_PATH%" (
    del /f /q "%EXE_PATH%"
    echo       已删除 %EXE_PATH%
) else (
    echo       未找到 exe，跳过
)

:: ── 4. 清理 PyInstaller 临时目录（如有残留）──
echo [4/4] 清理临时文件 ...
for /d %%i in (%TEMP%\_MEI*) do (
    rmdir /s /q "%%i" >nul 2>&1
)
echo       已清理

echo.
echo ========================================
echo   卸载完成！
echo   如需删除源码目录请手动操作。
echo ========================================
pause

@echo off
chcp 65001 >nul
echo ========================================
echo   编译 C 高性能引擎
echo ========================================
echo.

:: 检查 gcc
gcc --version >nul 2>&1
if errorlevel 1 (
    echo [INFO] 未找到 gcc，尝试使用 cl (MSVC)...
    goto :try_msvc
)

echo [1/2] 编译 tun_relay.dll (数据中继引擎)...
gcc -shared -O2 -o tun_relay.dll csrc\tun_relay.c -lws2_32
if errorlevel 1 (
    echo [ERROR] tun_relay.dll 编译失败
) else (
    echo [OK] tun_relay.dll
)

echo [2/2] 编译 socks5_proxy.exe (独立SOCKS5代理)...
gcc -O2 -o socks5_proxy.exe csrc\socks5_proxy.c -lws2_32
if errorlevel 1 (
    echo [ERROR] socks5_proxy.exe 编译失败
) else (
    echo [OK] socks5_proxy.exe
)

goto :done

:try_msvc
cl >nul 2>&1
if errorlevel 1 (
    echo [WARN] 未找到 C 编译器 (gcc 或 cl)
    echo        程序仍可运行，使用 Python 纯实现模式
    echo.
    echo 安装方法:
    echo   方法1: 安装 MinGW-w64  https://www.mingw-w64.org/
    echo   方法2: 安装 Visual Studio Build Tools
    goto :done
)

echo [1/2] 编译 tun_relay.dll ...
cl /LD /O2 csrc\tun_relay.c /link ws2_32.lib /out:tun_relay.dll
echo [2/2] 编译 socks5_proxy.exe ...
cl /O2 csrc\socks5_proxy.c /link ws2_32.lib /out:socks5_proxy.exe

:done
echo.
echo ========================================
echo   编译完成
echo ========================================
pause

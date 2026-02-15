"""
Windows 系统代理设置 - 通过注册表控制
支持 HTTP/HTTPS 代理 (浏览器等直接走 HTTP 代理，经由 SOCKS5 转发)
"""
import ctypes
import logging
import winreg

logger = logging.getLogger(__name__)

INTERNET_SETTINGS = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
INTERNET_OPTION_SETTINGS_CHANGED = 39
INTERNET_OPTION_REFRESH = 37


def _notify_system():
    try:
        fn = ctypes.windll.Wininet.InternetSetOptionW
        fn(0, INTERNET_OPTION_SETTINGS_CHANGED, 0, 0)
        fn(0, INTERNET_OPTION_REFRESH, 0, 0)
    except Exception as e:
        logger.warning(f"通知系统代理变更失败: {e}")


def set_system_proxy(http_port: int = 10801, socks_port: int = 10800) -> bool:
    """设置系统 HTTP + SOCKS 代理

    Windows 代理格式支持分协议设置:
      http=127.0.0.1:10801;https=127.0.0.1:10801;socks=127.0.0.1:10800
    浏览器/系统 HTTP 流量走 HTTP 代理 (10801)，其他走 SOCKS5 (10800)
    """
    try:
        proxy_addr = (
            f"http=127.0.0.1:{http_port};"
            f"https=127.0.0.1:{http_port};"
            f"socks=127.0.0.1:{socks_port}"
        )
        bypass = "localhost;127.*;10.*;192.168.*;<local>"
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, INTERNET_SETTINGS, 0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, proxy_addr)
        winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ, bypass)
        winreg.CloseKey(key)
        _notify_system()
        logger.info(f"系统代理已设置: HTTP={http_port}, SOCKS={socks_port}")
        return True
    except Exception as e:
        logger.error(f"设置系统代理失败: {e}")
        return False


def clear_system_proxy() -> bool:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, INTERNET_SETTINGS, 0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
        try:
            winreg.DeleteValue(key, "ProxyServer")
        except FileNotFoundError:
            pass
        winreg.CloseKey(key)
        _notify_system()
        logger.info("系统代理已清除")
        return True
    except Exception as e:
        logger.error(f"清除系统代理失败: {e}")
        return False

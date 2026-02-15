"""
C中继引擎的Python绑定 - 通过ctypes调用C共享库实现高性能数据转发
"""
import ctypes
import ctypes.util
import logging
import os
import platform
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


class CRelayEngine:
    """高性能C语言中继引擎"""

    def __init__(self):
        self._lib = None
        self._loaded = False

    @property
    def available(self) -> bool:
        """C引擎是否可用"""
        if not self._loaded:
            self._try_load()
        return self._lib is not None

    def _try_load(self):
        """尝试加载C共享库"""
        self._loaded = True
        lib_name = "tun_relay.dll" if platform.system() == "Windows" else "tun_relay.so"

        # 搜索路径
        search_dirs = [
            Path(__file__).parent,                    # 当前目录
            Path(__file__).parent / "csrc",           # csrc子目录
            Path(__file__).parent / "build",          # build子目录
        ]

        for d in search_dirs:
            lib_path = d / lib_name
            if lib_path.exists():
                try:
                    self._lib = ctypes.CDLL(str(lib_path))
                    self._setup_functions()
                    self._lib.relay_init()
                    logger.info(f"C中继引擎已加载: {lib_path}")
                    return
                except Exception as e:
                    logger.warning(f"加载C库失败 ({lib_path}): {e}")
                    self._lib = None

        logger.info("C中继引擎未找到，使用Python纯实现")

    def _setup_functions(self):
        """配置C函数签名"""
        lib = self._lib

        lib.relay_init.restype = ctypes.c_int
        lib.relay_init.argtypes = []

        lib.relay_cleanup.restype = None
        lib.relay_cleanup.argtypes = []

        lib.relay_start.restype = ctypes.c_int
        lib.relay_start.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]

        lib.relay_get_stats.restype = None
        lib.relay_get_stats.argtypes = [
            ctypes.POINTER(ctypes.c_longlong),
            ctypes.POINTER(ctypes.c_longlong),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
        ]

        lib.relay_reset_stats.restype = None
        lib.relay_reset_stats.argtypes = []

    def start_relay(self, fd_a: int, fd_b: int, timeout: int = 300) -> bool:
        """启动一对socket的双向中继"""
        if not self.available:
            return False
        return self._lib.relay_start(fd_a, fd_b, timeout) == 0

    def get_stats(self) -> dict:
        """获取中继统计数据"""
        if not self.available:
            return {"bytes_up": 0, "bytes_down": 0, "active": 0, "total": 0}

        up = ctypes.c_longlong(0)
        down = ctypes.c_longlong(0)
        active = ctypes.c_int(0)
        total = ctypes.c_int(0)

        self._lib.relay_get_stats(
            ctypes.byref(up), ctypes.byref(down),
            ctypes.byref(active), ctypes.byref(total)
        )

        return {
            "bytes_up": up.value,
            "bytes_down": down.value,
            "active": active.value,
            "total": total.value,
        }

    def cleanup(self):
        if self._lib:
            self._lib.relay_cleanup()


# 全局单例
c_engine = CRelayEngine()

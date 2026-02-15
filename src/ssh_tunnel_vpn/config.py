"""
配置管理 - JSON 格式保存/加载服务器配置
"""
import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home() / ".config")) / "SSHTunnelVPN"
CONFIG_FILE = CONFIG_DIR / "config.json"


@dataclass
class ServerConfig:
    host: str = ""
    port: int = 22
    username: str = ""
    password: str = ""
    use_key: bool = False
    key_path: str = ""
    key_passphrase: str = ""
    use_jump: bool = False
    jump_host: str = ""
    jump_port: int = 22
    jump_username: str = ""
    jump_password: str = ""
    jump_use_key: bool = False
    jump_key_path: str = ""
    jump_key_passphrase: str = ""
    socks_port: int = 10800
    http_port: int = 10801
    auto_set_proxy: bool = True


def save_config(config: ServerConfig) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(asdict(config), f, indent=2, ensure_ascii=False)


def load_config() -> ServerConfig:
    try:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return ServerConfig(**json.load(f))
    except Exception:
        pass
    return ServerConfig()


WINDOW_FILE = CONFIG_DIR / "window.json"


def save_window_geometry(geometry: str) -> None:
    """保存窗口 geometry 字符串，如 '960x520+100+200'"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(WINDOW_FILE, "w", encoding="utf-8") as f:
        json.dump({"geometry": geometry}, f)


def load_window_geometry() -> str:
    """读取上次保存的窗口 geometry，不存在则返回空串"""
    try:
        if WINDOW_FILE.exists():
            with open(WINDOW_FILE, "r", encoding="utf-8") as f:
                return json.load(f).get("geometry", "")
    except Exception:
        pass
    return ""

"""
SSH隧道管理器 + SOCKS5代理服务器
支持两种模式：
  1. 纯Python实现 (默认)
  2. C引擎加速的中继 (如果编译了C库)
"""
import logging
import socket
import select
import struct
import threading
import time
import subprocess
import sys
import os
from typing import Optional, Callable

import paramiko

from c_relay import c_engine
from http_proxy import HttpProxyServer

logger = logging.getLogger(__name__)


class Socks5Server:
    """本地SOCKS5代理服务器 - 将请求通过SSH通道转发"""

    def __init__(self, ssh_transport: paramiko.Transport, bind_port: int = 10800):
        self.transport = ssh_transport
        self.bind_port = bind_port
        self.server_socket: Optional[socket.socket] = None
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._use_c_engine = c_engine.available

        if self._use_c_engine:
            logger.info("使用C引擎加速数据中继")
        else:
            logger.info("使用Python数据中继")

    def start(self):
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.settimeout(1.0)
        self.server_socket.bind(("127.0.0.1", self.bind_port))
        self.server_socket.listen(128)
        self.running = True

        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        logger.info(f"SOCKS5代理已启动: 127.0.0.1:{self.bind_port}")

    def stop(self):
        self.running = False
        if self.server_socket:
            try:
                self.server_socket.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=3)
        logger.info("SOCKS5代理已停止")

    def _accept_loop(self):
        while self.running:
            try:
                client_socket, addr = self.server_socket.accept()
                t = threading.Thread(target=self._handle_client, args=(client_socket,), daemon=True)
                t.start()
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    logger.error(f"接受连接错误: {e}")
                break

    def _handle_client(self, client: socket.socket):
        try:
            # SOCKS5 握手
            header = client.recv(2)
            if len(header) < 2 or header[0] != 0x05:
                client.close()
                return

            num_methods = header[1]
            client.recv(num_methods)
            client.sendall(b"\x05\x00")

            # 连接请求
            request = client.recv(4)
            if len(request) < 4 or request[0] != 0x05 or request[1] != 0x01:
                client.sendall(b"\x05\x07\x00\x01" + b"\x00" * 6)
                client.close()
                return

            addr_type = request[3]

            if addr_type == 0x01:  # IPv4
                addr_bytes = client.recv(4)
                dest_addr = socket.inet_ntoa(addr_bytes)
            elif addr_type == 0x03:  # 域名
                addr_len = client.recv(1)[0]
                dest_addr = client.recv(addr_len).decode("utf-8")
            elif addr_type == 0x04:  # IPv6
                addr_bytes = client.recv(16)
                dest_addr = socket.inet_ntop(socket.AF_INET6, addr_bytes)
            else:
                client.sendall(b"\x05\x08\x00\x01" + b"\x00" * 6)
                client.close()
                return

            port_bytes = client.recv(2)
            dest_port = struct.unpack("!H", port_bytes)[0]

            # 通过SSH通道连接
            try:
                channel = self.transport.open_channel(
                    "direct-tcpip",
                    (dest_addr, dest_port),
                    ("127.0.0.1", 0),
                    timeout=10
                )
            except Exception as e:
                logger.debug(f"SSH通道失败 {dest_addr}:{dest_port}: {e}")
                client.sendall(b"\x05\x05\x00\x01" + b"\x00" * 6)
                client.close()
                return

            # 回复成功
            reply = b"\x05\x00\x00\x01" + socket.inet_aton("0.0.0.0") + struct.pack("!H", 0)
            client.sendall(reply)

            # 数据中继
            if self._use_c_engine:
                # 使用C引擎高性能中继
                client_fd = client.fileno()
                channel_fd = channel.fileno()
                if not c_engine.start_relay(client_fd, channel_fd, 300):
                    # 回退到Python实现
                    self._relay_python(client, channel)
            else:
                self._relay_python(client, channel)

        except Exception as e:
            logger.debug(f"SOCKS5处理错误: {e}")
        finally:
            try:
                client.close()
            except Exception:
                pass

    def _relay_python(self, client: socket.socket, channel: paramiko.Channel):
        """Python实现的双向数据中继"""
        channel.settimeout(0.0)
        client.settimeout(0.0)
        try:
            while self.running:
                r, _, _ = select.select([client, channel], [], [], 1.0)
                if client in r:
                    data = client.recv(65536)
                    if not data:
                        break
                    channel.sendall(data)
                if channel in r:
                    data = channel.recv(65536)
                    if not data:
                        break
                    client.sendall(data)
        except Exception:
            pass
        finally:
            try:
                channel.close()
            except Exception:
                pass


class SshTunnelManager:
    """SSH隧道管理器"""

    def __init__(self):
        self.ssh_client: Optional[paramiko.SSHClient] = None
        self.jump_client: Optional[paramiko.SSHClient] = None
        self._jump_channel: Optional[paramiko.Channel] = None
        self.socks_server: Optional[Socks5Server] = None
        self.http_proxy: Optional[HttpProxyServer] = None
        self._connected = False
        self._monitor_thread: Optional[threading.Thread] = None
        self._c_proxy_proc: Optional[subprocess.Popen] = None

        self.on_status_changed: Optional[Callable[[str, str], None]] = None
        self.on_log: Optional[Callable[[str], None]] = None

    @property
    def is_connected(self) -> bool:
        return self._connected and self.ssh_client is not None

    def connect(self, host: str, port: int, username: str, password: str,
                socks_port: int = 10800, http_port: int = 10801,
                use_key: bool = False, key_path: str = "", key_passphrase: str = "",
                use_jump: bool = False,
                jump_host: str = "", jump_port: int = 22,
                jump_username: str = "", jump_password: str = "",
                jump_use_key: bool = False, jump_key_path: str = "", jump_key_passphrase: str = ""):
        """连接SSH并启动SOCKS代理 + HTTP代理

        认证方式严格独立：
          - 目标机由 use_key 决定使用密码/私钥
          - 跳板机由 jump_use_key 决定使用密码/私钥

        即使 key_path/jump_key_path 有值，也不会自动切到私钥认证。
        """
        try:
            use_key = bool(use_key)
            jump_use_key = bool(jump_use_key)

            if use_jump:
                jump_username = jump_username or username
                jump_password = jump_password or password

            def _precheck_key(path: str, label: str):
                if not path:
                    return
                if path.lower().endswith(".pub"):
                    raise Exception(f"{label}选择的是公钥(.pub)，请改选私钥文件")
                try:
                    with open(path, "rb") as f:
                        head = f.read(256)
                except Exception as e:
                    raise Exception(f"{label}无法读取私钥文件: {e}")

                if b"PuTTY-User-Key-File-" in head:
                    raise Exception(f"{label}是 PuTTY .ppk 格式，Paramiko 不一定可用；请转换为 OpenSSH 私钥")

                if (
                    b"BEGIN OPENSSH PRIVATE KEY" in head
                    or b"BEGIN RSA PRIVATE KEY" in head
                    or b"BEGIN EC PRIVATE KEY" in head
                    or b"BEGIN DSA PRIVATE KEY" in head
                ):
                    return

                if head.startswith(b"ssh-") or head.startswith(b"ecdsa-"):
                    raise Exception(f"{label}文件看起来像公钥文本，请选择私钥文件")

            def _load_pkey(path: str, passphrase: str, label: str) -> paramiko.PKey:
                key_types = []
                if hasattr(paramiko, "Ed25519Key"):
                    key_types.append(paramiko.Ed25519Key)
                if hasattr(paramiko, "RSAKey"):
                    key_types.append(paramiko.RSAKey)
                if hasattr(paramiko, "ECDSAKey"):
                    key_types.append(paramiko.ECDSAKey)
                # DSSKey 在新版本可能被移除；存在才加入
                if hasattr(paramiko, "DSSKey"):
                    key_types.append(paramiko.DSSKey)

                last_exc = None
                for kt in key_types:
                    try:
                        if passphrase:
                            return kt.from_private_key_file(path, password=passphrase)
                        return kt.from_private_key_file(path)
                    except paramiko.PasswordRequiredException:
                        raise Exception(f"{label}私钥需要口令，请填写私钥口令")
                    except Exception as e:
                        last_exc = e

                raise Exception(f"{label}私钥无法解析/口令错误: {last_exc}")

            # 目标机认证校验
            if use_key:
                if not key_path:
                    raise Exception("目标机已选择私钥登录，但未提供私钥文件")
                if not os.path.exists(key_path):
                    raise Exception(f"目标机私钥文件不存在: {key_path}")
                _precheck_key(key_path, "目标机")

            # 跳板机认证校验
            if use_jump and jump_use_key:
                if not jump_key_path:
                    # 若跳板机未填 key，则尝试复用目标机 key
                    jump_key_path = key_path
                    jump_key_passphrase = jump_key_passphrase or key_passphrase
                if not jump_key_path:
                    raise Exception("跳板机已选择私钥登录，但未提供私钥文件")
                if not os.path.exists(jump_key_path):
                    raise Exception(f"跳板机私钥文件不存在: {jump_key_path}")
                _precheck_key(jump_key_path, "跳板机")

            def _connect_ssh(
                ssh_client: paramiko.SSHClient,
                *, hostname: str, port: int, username: str,
                password: str, use_key: bool, key_path: str, key_passphrase: str,
                sock=None,
            ):
                kwargs = dict(
                    hostname=hostname,
                    port=port,
                    username=username,
                    timeout=20,
                    look_for_keys=False,
                    allow_agent=False,
                    banner_timeout=20,
                )
                if sock is not None:
                    kwargs["sock"] = sock

                if use_key:
                    pkey = _load_pkey(key_path, key_passphrase, hostname)
                    kwargs["password"] = None
                    kwargs["pkey"] = pkey
                else:
                    kwargs["password"] = password

                ssh_client.connect(**kwargs)

            if use_jump:
                self._log(f"正在通过跳板机 {jump_host}:{jump_port} 连接目标 {host}:{port} ...")
            else:
                self._log(f"正在连接 {host}:{port} ...")
            self._notify_status("connecting", "正在连接...")

            self.disconnect()

            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            if use_jump:
                self._log(f"正在连接跳板机 {jump_host}:{jump_port} ...")
                jump_client = paramiko.SSHClient()
                jump_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                try:
                    _connect_ssh(
                        jump_client,
                        hostname=jump_host,
                        port=jump_port,
                        username=jump_username,
                        password=jump_password,
                        use_key=jump_use_key,
                        key_path=jump_key_path,
                        key_passphrase=jump_key_passphrase,
                    )
                except paramiko.AuthenticationException:
                    raise Exception("跳板机认证失败：用户名/密码/私钥不匹配")
                except Exception as e:
                    raise Exception(f"跳板机登录失败: {e}")

                jump_transport = jump_client.get_transport()
                if jump_transport is None or not jump_transport.is_active():
                    raise Exception("跳板机连接成功但 Transport 不可用")
                jump_transport.set_keepalive(30)
                self._log("跳板机会话已建立 ✓")

                self._log("正在通过跳板机建立目标会话...")
                try:
                    jump_channel = jump_transport.open_channel(
                        "direct-tcpip",
                        (host, port),
                        ("127.0.0.1", 0),
                        timeout=20,
                    )
                except Exception as e:
                    raise Exception(f"跳板机到目标机转发失败(请检查跳板机 AllowTcpForwarding): {e}")

                try:
                    _connect_ssh(
                        client,
                        hostname=host,
                        port=port,
                        username=username,
                        password=password,
                        use_key=use_key,
                        key_path=key_path,
                        key_passphrase=key_passphrase,
                        sock=jump_channel,
                    )
                except paramiko.AuthenticationException:
                    # 这里的认证失败应当按“目标机当前选择的方式”解释
                    if use_key:
                        raise Exception("目标机公钥认证失败：请确认目标机用户名正确，且公钥已加入 authorized_keys")
                    raise Exception("目标机密码认证失败：用户名或密码错误")
                except Exception as e:
                    raise Exception(f"目标机登录失败(经跳板机): {e}")

                self.jump_client = jump_client
                self._jump_channel = jump_channel
            else:
                self._log("正在建立SSH会话...")
                try:
                    _connect_ssh(
                        client,
                        hostname=host,
                        port=port,
                        username=username,
                        password=password,
                        use_key=use_key,
                        key_path=key_path,
                        key_passphrase=key_passphrase,
                    )
                except paramiko.AuthenticationException:
                    if use_key:
                        raise Exception("公钥认证失败：请确认用户名正确，且公钥已加入 authorized_keys")
                    raise Exception("密码认证失败：用户名或密码错误")
            self._log("SSH会话已建立 ✓")

            transport = client.get_transport()
            if transport is None:
                raise Exception("SSH Transport 创建失败")
            transport.set_keepalive(30)

            self.ssh_client = client

            # 启动SOCKS5代理
            self._log(f"正在启动SOCKS5代理 (端口: {socks_port})...")
            self.socks_server = Socks5Server(transport, socks_port)
            self.socks_server.start()

            engine_name = "C引擎" if c_engine.available else "Python"
            self._log(f"SOCKS5代理已启动 ✓ ({engine_name}加速)")
            self._log(f"SOCKS5 地址: 127.0.0.1:{socks_port}")

            # 启动HTTP代理（将HTTP/HTTPS流量通过SOCKS5转发）
            self._log(f"正在启动HTTP代理 (端口: {http_port})...")
            self.http_proxy = HttpProxyServer(listen_port=http_port, socks_port=socks_port)
            self.http_proxy.start()
            self._log(f"HTTP 代理已启动 ✓")
            self._log(f"HTTP/HTTPS 地址: 127.0.0.1:{http_port}")

            self._connected = True
            self._log(f"连接成功！流量将通过 {host} 转发")
            self._notify_status("connected", f"已连接: {host}")

            self._start_monitor()

        except paramiko.AuthenticationException:
            msg = "认证失败：用户名/密码/私钥不匹配"
            self._log(f"❌ {msg}")
            self._notify_status("disconnected", msg)
            raise Exception(msg)
        except socket.timeout:
            msg = "连接超时：请检查服务器地址和端口"
            self._log(f"❌ {msg}")
            self._notify_status("disconnected", msg)
            raise Exception(msg)
        except ConnectionRefusedError:
            msg = "连接被拒绝：请检查SSH服务"
            self._log(f"❌ {msg}")
            self._notify_status("disconnected", msg)
            raise Exception(msg)
        except Exception as e:
            msg = f"连接失败: {e}"
            self._log(f"❌ {msg}")
            self._notify_status("disconnected", msg)
            raise Exception(msg)

    def disconnect(self):
        self._connected = False

        if self._c_proxy_proc:
            try:
                self._c_proxy_proc.terminate()
                self._c_proxy_proc.wait(timeout=3)
            except Exception:
                pass
            self._c_proxy_proc = None

        if self.http_proxy:
            self.http_proxy.stop()
            self.http_proxy = None

        if self.socks_server:
            self.socks_server.stop()
            self.socks_server = None

        if self.ssh_client:
            try:
                self.ssh_client.close()
            except Exception:
                pass
            self.ssh_client = None

        if self._jump_channel:
            try:
                self._jump_channel.close()
            except Exception:
                pass
            self._jump_channel = None

        if self.jump_client:
            try:
                self.jump_client.close()
            except Exception:
                pass
            self.jump_client = None

        self._log("已断开连接")
        self._notify_status("disconnected", "未连接")

    def get_stats(self) -> dict:
        """获取流量统计"""
        if c_engine.available:
            return c_engine.get_stats()
        return {"bytes_up": 0, "bytes_down": 0, "active": 0, "total": 0}

    def _start_monitor(self):
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()

    def _monitor_loop(self):
        while self._connected:
            time.sleep(10)
            if not self._connected:
                break
            try:
                transport = self.ssh_client.get_transport() if self.ssh_client else None
                if transport is None or not transport.is_active():
                    self._log("⚠️ SSH连接已断开")
                    self._connected = False
                    self._notify_status("disconnected", "连接已中断")
                    break

                if self.jump_client:
                    jump_transport = self.jump_client.get_transport()
                    if jump_transport is None or not jump_transport.is_active():
                        self._log("⚠️ 跳板机连接已断开")
                        self._connected = False
                        self._notify_status("disconnected", "跳板机连接已中断")
                        break
            except Exception:
                self._connected = False
                self._notify_status("disconnected", "连接已中断")
                break

    def _log(self, message: str):
        logger.info(message)
        if self.on_log:
            self.on_log(message)

    def _notify_status(self, status: str, message: str):
        if self.on_status_changed:
            self.on_status_changed(status, message)

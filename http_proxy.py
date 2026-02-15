"""
本地 HTTP/HTTPS 代理服务器
接收浏览器的 HTTP/HTTPS 请求，通过上游 SOCKS5 代理转发

工作原理:
  - HTTP  请求: 解析 Host，通过 SOCKS5 连接目标，转发请求和响应
  - HTTPS 请求: 收到 CONNECT 方法后，通过 SOCKS5 建立隧道，双向透传数据
"""
import logging
import select
import socket
import struct
import threading
from typing import Optional

logger = logging.getLogger(__name__)


class HttpProxyServer:
    """本地 HTTP/HTTPS 代理，流量通过 SOCKS5 上游转发"""

    def __init__(self, listen_port: int = 10801, socks_port: int = 10800,
                 socks_host: str = "127.0.0.1"):
        self.listen_port = listen_port
        self.socks_port = socks_port
        self.socks_host = socks_host

        self._server: Optional[socket.socket] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # 流量统计
        self._lock = threading.Lock()
        self._bytes_up = 0
        self._bytes_down = 0
        self._active = 0
        self._total = 0

    def start(self):
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.settimeout(1.0)
        self._server.bind(("127.0.0.1", self.listen_port))
        self._server.listen(128)
        self._running = True

        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        logger.info(f"HTTP 代理已启动: 127.0.0.1:{self.listen_port} → SOCKS5 {self.socks_host}:{self.socks_port}")

    def stop(self):
        self._running = False
        if self._server:
            try:
                self._server.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=3)
        logger.info("HTTP 代理已停止")

    def get_stats(self) -> dict:
        with self._lock:
            return {
                "bytes_up": self._bytes_up,
                "bytes_down": self._bytes_down,
                "active": self._active,
                "total": self._total,
            }

    def _accept_loop(self):
        while self._running:
            try:
                client, addr = self._server.accept()
                client.settimeout(30)
                threading.Thread(target=self._handle_client, args=(client,), daemon=True).start()
            except socket.timeout:
                continue
            except OSError:
                break

    def _handle_client(self, client: socket.socket):
        with self._lock:
            self._active += 1
            self._total += 1
        try:
            # 读取第一行请求
            data = b""
            while b"\r\n" not in data and len(data) < 65536:
                chunk = client.recv(4096)
                if not chunk:
                    return
                data += chunk

            first_line = data.split(b"\r\n", 1)[0].decode("utf-8", errors="replace")
            parts = first_line.split()
            if len(parts) < 3:
                return

            method = parts[0].upper()
            target = parts[1]

            if method == "CONNECT":
                self._handle_connect(client, target, data)
            else:
                self._handle_http(client, method, target, data)

        except Exception as e:
            logger.debug(f"HTTP 代理处理错误: {e}")
        finally:
            try:
                client.close()
            except Exception:
                pass
            with self._lock:
                self._active -= 1

    def _handle_connect(self, client: socket.socket, target: str, initial_data: bytes):
        """处理 HTTPS CONNECT 隧道"""
        host, port = self._parse_host_port(target, default_port=443)
        if not host:
            client.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            return

        # 读完整个 CONNECT 请求头
        while b"\r\n\r\n" not in initial_data:
            chunk = client.recv(4096)
            if not chunk:
                return
            initial_data += chunk

        # 通过 SOCKS5 连接目标
        remote = self._connect_via_socks5(host, port)
        if remote is None:
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return

        # 告诉客户端隧道已建立
        client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")

        # 双向转发
        self._relay(client, remote)

    def _handle_http(self, client: socket.socket, method: str, target: str, initial_data: bytes):
        """处理普通 HTTP 请求"""
        # target 可能是 http://host:port/path 或 /path
        if target.startswith("http://"):
            # 绝对形式 — 典型的代理请求
            url_rest = target[7:]  # 去掉 http://
            slash_pos = url_rest.find("/")
            if slash_pos == -1:
                host_part = url_rest
                path = "/"
            else:
                host_part = url_rest[:slash_pos]
                path = url_rest[slash_pos:]
            host, port = self._parse_host_port(host_part, default_port=80)
        else:
            # 从 Host 头提取
            host, port, path = None, 80, target
            header_str = initial_data.decode("utf-8", errors="replace")
            for line in header_str.split("\r\n"):
                if line.lower().startswith("host:"):
                    host_val = line.split(":", 1)[1].strip()
                    host, port = self._parse_host_port(host_val, default_port=80)
                    break

        if not host:
            client.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            return

        # 通过 SOCKS5 连接目标
        remote = self._connect_via_socks5(host, port)
        if remote is None:
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return

        # 重写请求行: 把绝对 URL 改为相对路径
        first_line_end = initial_data.index(b"\r\n")
        original_first_line = initial_data[:first_line_end]
        parts = original_first_line.split(b" ", 2)
        new_first_line = parts[0] + b" " + path.encode("utf-8") + b" " + parts[2]
        rewritten = new_first_line + initial_data[first_line_end:]

        # 发送重写后的请求
        remote.sendall(rewritten)
        with self._lock:
            self._bytes_up += len(rewritten)

        # 双向转发
        self._relay(client, remote)

    def _connect_via_socks5(self, host: str, port: int) -> Optional[socket.socket]:
        """通过本地 SOCKS5 代理连接目标"""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(15)
            sock.connect((self.socks_host, self.socks_port))

            # SOCKS5 握手 — 无认证
            sock.sendall(b"\x05\x01\x00")
            resp = sock.recv(2)
            if resp != b"\x05\x00":
                sock.close()
                return None

            # SOCKS5 CONNECT 请求
            # 使用域名方式 (0x03)
            host_bytes = host.encode("utf-8")
            req = b"\x05\x01\x00\x03" + bytes([len(host_bytes)]) + host_bytes + struct.pack("!H", port)
            sock.sendall(req)

            resp = sock.recv(10)
            if len(resp) < 2 or resp[1] != 0x00:
                sock.close()
                return None

            # 如果是域名回复，需要读完剩余字节
            if len(resp) >= 4:
                atyp = resp[3]
                if atyp == 0x01:  # IPv4
                    remaining = 4 + 2 - (len(resp) - 4)
                elif atyp == 0x03:  # domain
                    if len(resp) > 4:
                        domain_len = resp[4]
                        remaining = 1 + domain_len + 2 - (len(resp) - 4)
                    else:
                        remaining = 1
                elif atyp == 0x04:  # IPv6
                    remaining = 16 + 2 - (len(resp) - 4)
                else:
                    remaining = 0
                if remaining > 0:
                    sock.recv(remaining)

            sock.settimeout(None)
            return sock

        except Exception as e:
            logger.debug(f"SOCKS5 连接失败 {host}:{port} — {e}")
            try:
                sock.close()
            except Exception:
                pass
            return None

    def _relay(self, client: socket.socket, remote: socket.socket):
        """双向数据中继"""
        client.setblocking(False)
        remote.setblocking(False)
        try:
            while self._running:
                r, _, _ = select.select([client, remote], [], [], 2.0)
                if client in r:
                    data = client.recv(65536)
                    if not data:
                        break
                    remote.sendall(data)
                    with self._lock:
                        self._bytes_up += len(data)
                if remote in r:
                    data = remote.recv(65536)
                    if not data:
                        break
                    client.sendall(data)
                    with self._lock:
                        self._bytes_down += len(data)
        except Exception:
            pass
        finally:
            try:
                remote.close()
            except Exception:
                pass

    @staticmethod
    def _parse_host_port(addr: str, default_port: int = 80) -> tuple:
        """解析 host:port 格式，支持 IPv6 [::1]:port"""
        if addr.startswith("["):
            # IPv6
            bracket_end = addr.find("]")
            if bracket_end == -1:
                return addr[1:], default_port
            host = addr[1:bracket_end]
            rest = addr[bracket_end + 1:]
            if rest.startswith(":"):
                try:
                    return host, int(rest[1:])
                except ValueError:
                    return host, default_port
            return host, default_port
        elif ":" in addr:
            parts = addr.rsplit(":", 1)
            try:
                return parts[0], int(parts[1])
            except ValueError:
                return addr, default_port
        return addr, default_port

# SSH Tunnel VPN — Python + C 实现

一个高性能的 SSH 隧道 VPN 工具，Python 负责 SSH 连接和 GUI，C 负责网络数据高速中继。

## 架构

```
┌─────────────────────────────────────────────┐
│              CustomTkinter GUI              │  ← Python
│              (main.py)                      │
├─────────────────────────────────────────────┤
│        SSH 隧道管理 + SOCKS5 代理           │  ← Python (Paramiko)
│        (ssh_tunnel.py)                      │
├─────────────────────────────────────────────┤
│    HTTP/HTTPS 代理 → SOCKS5 转发            │  ← Python
│    (http_proxy.py)                          │
├─────────────────────────────────────────────┤
│     C 高性能数据中继引擎 (可选)             │  ← C (ctypes 调用)
│     tun_relay.dll / socks5_proxy.exe        │
├─────────────────────────────────────────────┤
│       Windows 系统代理设置                  │  ← Python (winreg)
│       (proxy_settings.py)                   │
└─────────────────────────────────────────────┘
```

## 项目结构

```
ssh_tunnel_win/
├── main.py              # 入口 (GUI 窗口 + CLI 命令行 双模式)
├── ssh_tunnel.py        # SSH隧道 + SOCKS5代理服务器
├── http_proxy.py        # HTTP/HTTPS 代理 (通过 SOCKS5 转发)
├── c_relay.py           # C引擎 Python 绑定 (ctypes)
├── proxy_settings.py    # Windows 系统代理 (注册表)
├── config.py            # 配置管理 (JSON)
├── requirements.txt     # Python 依赖
├── build.bat            # C引擎编译脚本
├── run.bat              # 一键启动
├── csrc/
│   ├── socks5_proxy.c   # 独立 SOCKS5 代理 (C)
│   └── tun_relay.c      # 数据中继共享库 (C → DLL)
└── README.md
```

## 快速开始

```bash
pip install -r requirements.txt
```

### GUI 窗口模式（默认）

```bash
python main.py          # 打开图形界面
python main.py gui      # 同上
```

### CLI 命令行模式

```bash
# 指定服务器参数
python main.py cli -H 54.1.2.3 -u root -p 123456

# 使用已保存的配置（GUI 或 CLI 保存过的）
python main.py cli

# 通过跳板机连接目标服务器
python main.py cli -H 10.0.0.8 -u app -p apppw --jump-host 1.2.3.4 --jump-user jump --jump-password jpw

# 自定义 SOCKS 端口 + 不设置系统代理
python main.py cli -H 1.2.3.4 -u root -p pw -s 1080 --no-proxy

# 自定义 HTTP 代理端口
python main.py cli -H 1.2.3.4 -u root -p pw --http 8080

# 私钥登录（目标机）
python main.py cli -H 1.2.3.4 -u root --key C:/Users/you/.ssh/id_rsa

# 私钥登录（跳板机 + 目标机）
python main.py cli -H 10.0.0.8 -u app --key C:/keys/target_id_rsa \
      --jump-host 1.2.3.4 --jump-user jump --jump-key C:/keys/jump_id_rsa

# 查看完整参数
python main.py cli -h
```

CLI 模式支持的参数：

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `-H, --host` | 服务器 IP / 域名 | (必填或读配置) |
| `-P, --port` | SSH 端口 | 22 |
| `-u, --user` | 用户名 | (必填或读配置) |
| `-p, --password` | 密码 | (必填或读配置) |
| `--key` | 目标私钥文件路径 | 不使用 |
| `--key-passphrase` | 目标私钥口令 | 空 |
| `--jump-host` | 跳板机 IP / 域名 | 不使用 |
| `--jump-port` | 跳板机 SSH 端口 | 22 |
| `--jump-user` | 跳板机用户名 | (跳板机模式必填) |
| `--jump-password` | 跳板机密码 | (跳板机模式必填) |
| `--jump-key` | 跳板机私钥文件路径 | 不使用 |
| `--jump-key-passphrase` | 跳板机私钥口令 | 空 |
| `-s, --socks` | 本地 SOCKS5 端口 | 10800 |
| `--http` | 本地 HTTP 代理端口 | 10801 |
| `--proxy / --no-proxy` | 是否自动设置系统代理 | 自动设置 |
| `--no-save` | 不保存本次配置 | 保存 |

### 跳板机模式说明

- 当启用跳板机时，连接路径是：本地 → 跳板机 SSH → 目标 SSH。
- GUI 可在“使用 SSH 跳板机”区域填写跳板机参数。
- CLI 只要设置 `--jump-host`，即自动进入跳板机模式。

### C 引擎加速（可选）

```bash
# 编译 C 引擎后再运行，性能提升 10-50x
build.bat
python main.py          # 自动检测并启用 C 引擎
```

不编译也完全可用，所有功能通过 Python 实现。

## 代理工作原理

```
浏览器 HTTP/HTTPS 请求
        │
        ▼
  HTTP 代理 (10801)         ← http_proxy.py
        │
        ▼
  SOCKS5 代理 (10800)      ← ssh_tunnel.py
        │
        ▼
  SSH 加密隧道              ← paramiko
        │
        ▼
  远程 SSH 服务器
        │
        ▼
  目标网站
```

- **HTTP 代理** (端口 10801): 接收浏览器的 HTTP/HTTPS 请求，通过 SOCKS5 转发
- **SOCKS5 代理** (端口 10800): 通过 SSH direct-tcpip 通道连接目标
- **系统代理**: 自动设置 Windows 注册表，HTTP/HTTPS/SOCKS 全协议覆盖

## C 组件说明

### tun_relay.dll — 数据中继引擎

通过 `ctypes` 被 Python 调用，替代 Python 的 `select` + `recv/send` 循环：
- 多线程并发中继
- 64KB 缓冲区，零拷贝转发
- 内置流量统计（上传/下载字节数、活跃连接数）

### socks5_proxy.exe — 独立 SOCKS5 代理

可单独运行的高性能代理，支持上游 SOCKS5 链式转发：

```bash
# 直连模式
socks5_proxy.exe -l 1080

# 通过 SSH 隧道转发
socks5_proxy.exe -l 1080 -u 127.0.0.1:10800
```

## 编译要求

任选其一：
- **MinGW-w64** (gcc)：`gcc -shared -O2 -o tun_relay.dll csrc/tun_relay.c -lws2_32`
- **MSVC** (cl)：`cl /LD /O2 csrc/tun_relay.c /link ws2_32.lib`

或直接运行 `build.bat` 自动检测编译器。

## 服务器端配置

确保 SSH 服务器允许 TCP 转发：

```bash
# /etc/ssh/sshd_config
AllowTcpForwarding yes
GatewayPorts yes

sudo systemctl restart sshd
```

防火墙 / 安全组需开放 SSH 端口（默认 22）。
适用于任意 Linux 服务器（阿里云、腾讯云、Vultr、自建等）。

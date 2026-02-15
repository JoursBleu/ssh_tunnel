# SSH Tunnel VPN

一个 SSH 隧道 VPN 工具，支持 GUI 和 CLI 双模式，提供 SOCKS5 + HTTP/HTTPS 代理、跳板机、私钥登录、Windows 系统代理自动设置。

## 架构

```
┌─────────────────────────────────────────────┐
│              CustomTkinter GUI              │  ← Python
│              (main.py)                      │
├─────────────────────────────────────────────┤
│        SSH 隧道管理 + SOCKS5 代理           │  ← Paramiko
│        (ssh_tunnel.py)                      │
├─────────────────────────────────────────────┤
│    HTTP/HTTPS 代理 → SOCKS5 转发            │  ← Python
│    (http_proxy.py)                          │
├─────────────────────────────────────────────┤
│       Windows 系统代理设置                  │  ← winreg
│       (proxy_settings.py)                   │
└─────────────────────────────────────────────┘
```

## 项目结构

```
ssh_tunnel_win/
├── main.py              # 入口 (GUI 窗口 + CLI 命令行 双模式)
├── ssh_tunnel.py        # SSH隧道 + SOCKS5代理服务器
├── http_proxy.py        # HTTP/HTTPS 代理 (通过 SOCKS5 转发)
├── proxy_settings.py    # Windows 系统代理 (注册表)
├── config.py            # 配置管理 (JSON)
├── requirements.txt     # Python 依赖
├── run.bat              # 一键启动
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

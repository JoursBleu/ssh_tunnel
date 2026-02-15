/*
 * socks5_proxy.c - 高性能 SOCKS5 代理服务器 (C 实现)
 *
 * 编译: gcc -O2 -o socks5_proxy socks5_proxy.c -lws2_32 (Windows)
 *       gcc -O2 -o socks5_proxy socks5_proxy.c -lpthread  (Linux/Mac)
 *
 * 功能:
 *   - 监听本地端口，接收 SOCKS5 代理请求
 *   - 将请求通过指定的上游 SOCKS5 代理转发 (SSH 隧道)
 *   - 高性能多线程 select/poll 数据中继
 */

#ifdef _WIN32
    #define WIN32_LEAN_AND_MEAN
    #include <winsock2.h>
    #include <ws2tcpip.h>
    #include <windows.h>
    #pragma comment(lib, "ws2_32.lib")
    typedef SOCKET socket_t;
    typedef int socklen_t;
    #define CLOSESOCK closesocket
    #define THREAD_RET DWORD WINAPI
    #define THREAD_HANDLE HANDLE
    #define sleep_ms(ms) Sleep(ms)
#else
    #include <sys/socket.h>
    #include <sys/select.h>
    #include <netinet/in.h>
    #include <arpa/inet.h>
    #include <netdb.h>
    #include <unistd.h>
    #include <pthread.h>
    typedef int socket_t;
    #define INVALID_SOCKET -1
    #define SOCKET_ERROR -1
    #define CLOSESOCK close
    #define THREAD_RET void*
    #define THREAD_HANDLE pthread_t
    #define sleep_ms(ms) usleep((ms)*1000)
#endif

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <time.h>

/* ── 配置 ── */
#define BUF_SIZE        65536
#define MAX_CLIENTS     256
#define RELAY_TIMEOUT   300     /* 空闲超时(秒) */

/* ── 全局状态 ── */
static volatile int g_running = 1;
static long g_total_bytes_up = 0;
static long g_total_bytes_down = 0;
static int  g_active_conns = 0;

/* ── 日志 ── */
static void log_msg(const char *level, const char *fmt, ...) {
    time_t now = time(NULL);
    struct tm *t = localtime(&now);
    char timebuf[32];
    strftime(timebuf, sizeof(timebuf), "%H:%M:%S", t);

    fprintf(stderr, "[%s] [%s] ", timebuf, level);
    va_list args;
    va_start(args, fmt);
    vfprintf(stderr, fmt, args);
    va_end(args);
    fprintf(stderr, "\n");
    fflush(stderr);
}

#define LOG_INFO(...)  log_msg("INFO", __VA_ARGS__)
#define LOG_ERR(...)   log_msg("ERROR", __VA_ARGS__)
#define LOG_DBG(...)   /* log_msg("DEBUG", __VA_ARGS__) */

/* ── 网络初始化 ── */
static int net_init(void) {
#ifdef _WIN32
    WSADATA wsa;
    return WSAStartup(MAKEWORD(2, 2), &wsa);
#else
    signal(SIGPIPE, SIG_IGN);
    return 0;
#endif
}

static void net_cleanup(void) {
#ifdef _WIN32
    WSACleanup();
#endif
}

/* ── 创建监听socket ── */
static socket_t create_listener(const char *bind_addr, int port) {
    socket_t sock = socket(AF_INET, SOCK_STREAM, 0);
    if (sock == INVALID_SOCKET) {
        LOG_ERR("socket() 创建失败");
        return INVALID_SOCKET;
    }

    int opt = 1;
    setsockopt(sock, SOL_SOCKET, SO_REUSEADDR, (const char *)&opt, sizeof(opt));

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons((unsigned short)port);
    inet_pton(AF_INET, bind_addr, &addr.sin_addr);

    if (bind(sock, (struct sockaddr *)&addr, sizeof(addr)) == SOCKET_ERROR) {
        LOG_ERR("bind() 失败: 端口 %d", port);
        CLOSESOCK(sock);
        return INVALID_SOCKET;
    }

    if (listen(sock, 128) == SOCKET_ERROR) {
        LOG_ERR("listen() 失败");
        CLOSESOCK(sock);
        return INVALID_SOCKET;
    }

    LOG_INFO("SOCKS5 代理监听: %s:%d", bind_addr, port);
    return sock;
}

/* ── SOCKS5 握手处理 ── */

/* 读取完整的 n 字节 */
static int recv_exact(socket_t sock, unsigned char *buf, int n) {
    int total = 0;
    while (total < n) {
        int r = recv(sock, (char *)(buf + total), n - total, 0);
        if (r <= 0) return -1;
        total += r;
    }
    return total;
}

/*
 * 处理 SOCKS5 协商，解析出目标地址和端口
 * 返回 0=成功, -1=失败
 */
static int socks5_handshake(socket_t client, char *dest_host, int *dest_port) {
    unsigned char buf[512];

    /* ── 认证协商 ── */
    if (recv_exact(client, buf, 2) < 0) return -1;
    if (buf[0] != 0x05) return -1;

    int nmethods = buf[1];
    if (nmethods > 0) {
        if (recv_exact(client, buf, nmethods) < 0) return -1;
    }

    /* 回复: 无需认证 */
    unsigned char auth_reply[] = {0x05, 0x00};
    send(client, (const char *)auth_reply, 2, 0);

    /* ── 连接请求 ── */
    if (recv_exact(client, buf, 4) < 0) return -1;
    if (buf[0] != 0x05 || buf[1] != 0x01) return -1; /* 只支持 CONNECT */

    int atyp = buf[3];

    if (atyp == 0x01) {
        /* IPv4 */
        if (recv_exact(client, buf, 4) < 0) return -1;
        snprintf(dest_host, 256, "%d.%d.%d.%d", buf[0], buf[1], buf[2], buf[3]);
    } else if (atyp == 0x03) {
        /* 域名 */
        if (recv_exact(client, buf, 1) < 0) return -1;
        int dlen = buf[0];
        if (recv_exact(client, buf, dlen) < 0) return -1;
        buf[dlen] = '\0';
        strncpy(dest_host, (char *)buf, 255);
    } else if (atyp == 0x04) {
        /* IPv6 */
        if (recv_exact(client, buf, 16) < 0) return -1;
        inet_ntop(AF_INET6, buf, dest_host, 256);
    } else {
        return -1;
    }

    /* 端口 */
    if (recv_exact(client, buf, 2) < 0) return -1;
    *dest_port = (buf[0] << 8) | buf[1];

    return 0;
}

/* 发送 SOCKS5 连接回复 */
static void socks5_reply(socket_t client, int code) {
    unsigned char reply[] = {
        0x05, (unsigned char)code, 0x00, 0x01,
        0x00, 0x00, 0x00, 0x00, /* 绑定地址 */
        0x00, 0x00              /* 绑定端口 */
    };
    send(client, (const char *)reply, sizeof(reply), 0);
}

/* ── 通过上游SOCKS5代理连接目标 ── */
static socket_t connect_via_upstream(
    const char *upstream_host, int upstream_port,
    const char *dest_host, int dest_port
) {
    socket_t sock = socket(AF_INET, SOCK_STREAM, 0);
    if (sock == INVALID_SOCKET) return INVALID_SOCKET;

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons((unsigned short)upstream_port);
    inet_pton(AF_INET, upstream_host, &addr.sin_addr);

    /* 设置超时 */
#ifdef _WIN32
    int timeout = 10000;
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, (const char *)&timeout, sizeof(timeout));
    setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, (const char *)&timeout, sizeof(timeout));
#else
    struct timeval tv = {10, 0};
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
#endif

    if (connect(sock, (struct sockaddr *)&addr, sizeof(addr)) == SOCKET_ERROR) {
        CLOSESOCK(sock);
        return INVALID_SOCKET;
    }

    unsigned char buf[512];

    /* SOCKS5 认证 */
    unsigned char auth[] = {0x05, 0x01, 0x00};
    send(sock, (const char *)auth, 3, 0);
    if (recv_exact(sock, buf, 2) < 0 || buf[0] != 0x05) {
        CLOSESOCK(sock);
        return INVALID_SOCKET;
    }

    /* 构造连接请求 */
    int dlen = (int)strlen(dest_host);
    unsigned char *req = buf;
    int pos = 0;
    req[pos++] = 0x05;   /* VER */
    req[pos++] = 0x01;   /* CONNECT */
    req[pos++] = 0x00;   /* RSV */
    req[pos++] = 0x03;   /* DOMAINNAME */
    req[pos++] = (unsigned char)dlen;
    memcpy(req + pos, dest_host, dlen);
    pos += dlen;
    req[pos++] = (dest_port >> 8) & 0xFF;
    req[pos++] = dest_port & 0xFF;

    send(sock, (const char *)req, pos, 0);

    if (recv_exact(sock, buf, 4) < 0 || buf[1] != 0x00) {
        CLOSESOCK(sock);
        return INVALID_SOCKET;
    }

    /* 跳过绑定地址 */
    if (buf[3] == 0x01) {
        recv_exact(sock, buf + 4, 4 + 2); /* IPv4 + port */
    } else if (buf[3] == 0x03) {
        recv_exact(sock, buf + 4, 1);
        recv_exact(sock, buf + 5, buf[4] + 2);
    } else if (buf[3] == 0x04) {
        recv_exact(sock, buf + 4, 16 + 2); /* IPv6 + port */
    }

    /* 恢复非阻塞超时 */
#ifdef _WIN32
    timeout = 0;
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, (const char *)&timeout, sizeof(timeout));
    setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, (const char *)&timeout, sizeof(timeout));
#else
    tv.tv_sec = 0; tv.tv_usec = 0;
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
#endif

    return sock;
}

/* ── 直接连接目标（无上游代理时） ── */
static socket_t connect_direct(const char *host, int port) {
    struct addrinfo hints, *res, *p;
    memset(&hints, 0, sizeof(hints));
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;

    char port_str[16];
    snprintf(port_str, sizeof(port_str), "%d", port);

    if (getaddrinfo(host, port_str, &hints, &res) != 0) {
        return INVALID_SOCKET;
    }

    socket_t sock = INVALID_SOCKET;
    for (p = res; p; p = p->ai_next) {
        sock = socket(p->ai_family, p->ai_socktype, p->ai_protocol);
        if (sock == INVALID_SOCKET) continue;
        if (connect(sock, p->ai_addr, (int)p->ai_addrlen) == 0) break;
        CLOSESOCK(sock);
        sock = INVALID_SOCKET;
    }
    freeaddrinfo(res);
    return sock;
}

/* ── 数据中继 ── */
typedef struct {
    socket_t client;
    socket_t remote;
} relay_ctx_t;

static THREAD_RET relay_thread(void *arg) {
    relay_ctx_t *ctx = (relay_ctx_t *)arg;
    socket_t client = ctx->client;
    socket_t remote = ctx->remote;
    free(ctx);

    g_active_conns++;

    char *buf = (char *)malloc(BUF_SIZE);
    if (!buf) {
        CLOSESOCK(client);
        CLOSESOCK(remote);
        g_active_conns--;
        return 0;
    }

    fd_set rfds;
    struct timeval tv;
    socket_t maxfd = (client > remote) ? client : remote;

    while (g_running) {
        FD_ZERO(&rfds);
        FD_SET(client, &rfds);
        FD_SET(remote, &rfds);

        tv.tv_sec = RELAY_TIMEOUT;
        tv.tv_usec = 0;

        int ret = select((int)(maxfd + 1), &rfds, NULL, NULL, &tv);
        if (ret <= 0) break; /* 超时或错误 */

        if (FD_ISSET(client, &rfds)) {
            int n = recv(client, buf, BUF_SIZE, 0);
            if (n <= 0) break;
            if (send(remote, buf, n, 0) <= 0) break;
            g_total_bytes_up += n;
        }

        if (FD_ISSET(remote, &rfds)) {
            int n = recv(remote, buf, BUF_SIZE, 0);
            if (n <= 0) break;
            if (send(client, buf, n, 0) <= 0) break;
            g_total_bytes_down += n;
        }
    }

    free(buf);
    CLOSESOCK(client);
    CLOSESOCK(remote);
    g_active_conns--;

    return 0;
}

/* ── 客户端处理线程 ── */
typedef struct {
    socket_t client;
    char upstream_host[64];
    int  upstream_port;
    int  use_upstream;
} client_ctx_t;

static THREAD_RET client_thread(void *arg) {
    client_ctx_t *ctx = (client_ctx_t *)arg;
    socket_t client = ctx->client;
    char dest_host[256] = {0};
    int  dest_port = 0;

    /* SOCKS5协商 */
    if (socks5_handshake(client, dest_host, &dest_port) < 0) {
        CLOSESOCK(client);
        free(ctx);
        return 0;
    }

    LOG_DBG("连接请求: %s:%d", dest_host, dest_port);

    /* 连接目标 */
    socket_t remote;
    if (ctx->use_upstream) {
        remote = connect_via_upstream(ctx->upstream_host, ctx->upstream_port,
                                      dest_host, dest_port);
    } else {
        remote = connect_direct(dest_host, dest_port);
    }

    if (remote == INVALID_SOCKET) {
        LOG_DBG("连接失败: %s:%d", dest_host, dest_port);
        socks5_reply(client, 0x05); /* 连接被拒 */
        CLOSESOCK(client);
        free(ctx);
        return 0;
    }

    /* 回复成功 */
    socks5_reply(client, 0x00);

    /* 启动中继线程 */
    relay_ctx_t *rctx = (relay_ctx_t *)malloc(sizeof(relay_ctx_t));
    rctx->client = client;
    rctx->remote = remote;

#ifdef _WIN32
    CreateThread(NULL, 0, relay_thread, rctx, 0, NULL);
#else
    pthread_t tid;
    pthread_attr_t attr;
    pthread_attr_init(&attr);
    pthread_attr_setdetachstate(&attr, PTHREAD_CREATE_DETACHED);
    pthread_create(&tid, &attr, relay_thread, rctx);
    pthread_attr_destroy(&attr);
#endif

    free(ctx);
    return 0;
}

/* ── 信号处理 ── */
static void signal_handler(int sig) {
    (void)sig;
    g_running = 0;
    LOG_INFO("收到退出信号，正在关闭...");
}

/* ── 状态输出线程 ── */
static THREAD_RET stats_thread(void *arg) {
    (void)arg;
    while (g_running) {
        sleep_ms(30000); /* 每30秒输出一次 */
        if (!g_running) break;
        LOG_INFO("状态: 活跃连接=%d  上传=%.2fMB  下载=%.2fMB",
                 g_active_conns,
                 g_total_bytes_up / (1024.0 * 1024.0),
                 g_total_bytes_down / (1024.0 * 1024.0));
    }
    return 0;
}

/* ── 打印用法 ── */
static void usage(const char *prog) {
    fprintf(stderr,
        "用法: %s [选项]\n"
        "\n"
        "选项:\n"
        "  -l <port>       本地监听端口 (默认: 1080)\n"
        "  -b <addr>       绑定地址 (默认: 127.0.0.1)\n"
        "  -u <host:port>  上游SOCKS5代理 (SSH隧道的端口)\n"
        "  -h              显示帮助\n"
        "\n"
        "示例:\n"
        "  %s -l 1080 -u 127.0.0.1:10800\n"
        "  将本地1080端口的请求通过SSH隧道(10800)转发\n"
        "\n",
        prog, prog);
}

/* ── 主函数 ── */
int main(int argc, char *argv[]) {
    int listen_port = 1080;
    char bind_addr[64] = "127.0.0.1";
    char upstream_host[64] = "";
    int  upstream_port = 0;
    int  use_upstream = 0;

    /* 解析参数 */
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "-l") == 0 && i + 1 < argc) {
            listen_port = atoi(argv[++i]);
        } else if (strcmp(argv[i], "-b") == 0 && i + 1 < argc) {
            strncpy(bind_addr, argv[++i], sizeof(bind_addr) - 1);
        } else if (strcmp(argv[i], "-u") == 0 && i + 1 < argc) {
            i++;
            char *colon = strrchr(argv[i], ':');
            if (colon) {
                *colon = '\0';
                strncpy(upstream_host, argv[i], sizeof(upstream_host) - 1);
                upstream_port = atoi(colon + 1);
                use_upstream = 1;
            } else {
                fprintf(stderr, "错误: 上游代理格式应为 host:port\n");
                return 1;
            }
        } else if (strcmp(argv[i], "-h") == 0 || strcmp(argv[i], "--help") == 0) {
            usage(argv[0]);
            return 0;
        }
    }

    /* 平台初始化 */
    if (net_init() != 0) {
        LOG_ERR("网络初始化失败");
        return 1;
    }

    signal(SIGINT, signal_handler);
    signal(SIGTERM, signal_handler);

    LOG_INFO("SSH Tunnel SOCKS5 Proxy (C Engine)");
    if (use_upstream) {
        LOG_INFO("上游代理: %s:%d", upstream_host, upstream_port);
    } else {
        LOG_INFO("直连模式 (无上游代理)");
    }

    /* 创建监听 */
    socket_t listener = create_listener(bind_addr, listen_port);
    if (listener == INVALID_SOCKET) {
        net_cleanup();
        return 1;
    }

    /* 启动状态线程 */
#ifdef _WIN32
    CreateThread(NULL, 0, stats_thread, NULL, 0, NULL);
#else
    pthread_t stid;
    pthread_create(&stid, NULL, stats_thread, NULL);
    pthread_detach(stid);
#endif

    /* 主循环 */
    LOG_INFO("等待连接...");

    while (g_running) {
        fd_set fds;
        struct timeval tv = {1, 0};
        FD_ZERO(&fds);
        FD_SET(listener, &fds);

        int ret = select((int)(listener + 1), &fds, NULL, NULL, &tv);
        if (ret <= 0) continue;

        struct sockaddr_in caddr;
        socklen_t clen = sizeof(caddr);
        socket_t client = accept(listener, (struct sockaddr *)&caddr, &clen);
        if (client == INVALID_SOCKET) continue;

        if (g_active_conns >= MAX_CLIENTS) {
            LOG_ERR("连接数已满 (%d)", MAX_CLIENTS);
            CLOSESOCK(client);
            continue;
        }

        client_ctx_t *ctx = (client_ctx_t *)malloc(sizeof(client_ctx_t));
        ctx->client = client;
        ctx->use_upstream = use_upstream;
        if (use_upstream) {
            strncpy(ctx->upstream_host, upstream_host, sizeof(ctx->upstream_host) - 1);
            ctx->upstream_port = upstream_port;
        }

#ifdef _WIN32
        CreateThread(NULL, 0, client_thread, ctx, 0, NULL);
#else
        pthread_t tid;
        pthread_attr_t attr;
        pthread_attr_init(&attr);
        pthread_attr_setdetachstate(&attr, PTHREAD_CREATE_DETACHED);
        pthread_create(&tid, &attr, client_thread, ctx);
        pthread_attr_destroy(&attr);
#endif
    }

    /* 清理 */
    CLOSESOCK(listener);
    net_cleanup();
    LOG_INFO("已退出");

    return 0;
}

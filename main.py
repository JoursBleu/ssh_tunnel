"""\
SSH Tunnel VPN - Python + C 版

支持两种运行模式:
  1) GUI 窗口模式(默认): python main.py
  2) CLI 命令行模式:     python main.py cli -H 1.2.3.4 -u user -p pass
                        python main.py cli  (使用已保存的配置)

功能:
  - SSH 隧道 SOCKS5 代理
  - HTTP/HTTPS 代理(通过 SOCKS5 转发)
  - Windows 系统代理自动设置
  - 支持跳板机(二跳)
  - 支持密码/私钥登录(目标机/跳板机)
"""

import argparse
import logging
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime

from config import ServerConfig, load_config, save_config, load_window_geometry, save_window_geometry
from proxy_settings import clear_system_proxy, set_system_proxy
from ssh_tunnel import SshTunnelManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)


def _detect_default_private_key_path() -> str:
    """返回本机默认私钥路径（若存在），否则返回空串。"""
    ssh_dir = Path.home() / ".ssh"
    candidates = [
        ssh_dir / "id_ed25519",
        ssh_dir / "id_rsa",
        ssh_dir / "id_ecdsa",
        ssh_dir / "id_dsa",
    ]
    for p in candidates:
        try:
            if p.exists() and p.is_file():
                return str(p)
        except Exception:
            continue
    return ""


class SSHTunnelApp:
    """GUI 窗口模式 — CustomTkinter"""

    GREEN = "#22c55e"
    RED = "#ef4444"
    ORANGE = "#f59e0b"
    GREY = "#6b7280"

    def __init__(self):
        import customtkinter as ctk

        self.ctk = ctk
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")

        self.root = ctk.CTk()
        self.root.title("SSH Tunnel VPN  (Python + C)")
        saved_geo = load_window_geometry()
        self.root.geometry(saved_geo if saved_geo else "960x520")
        self.root.minsize(900, 460)

        self.tunnel = SshTunnelManager()
        self.tunnel.on_log = self._on_log
        self.tunnel.on_status_changed = self._on_status_changed

        self.is_connected = False
        self.proxy_enabled = False
        self._stats_job = None
        self._inputs_enabled = True

        self._build_ui()
        self._load_saved_config()
        self._apply_auth_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def run(self):
        self.root.mainloop()

    def _build_ui(self):
        ctk = self.ctk

        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(0, weight=1)

        import tkinter as tk

        # 外层 canvas + scrollbar，滚动条按需显示
        container = ctk.CTkFrame(self.root, corner_radius=0, fg_color="transparent")
        container.grid(row=0, column=0, sticky="nsew")
        container.grid_rowconfigure(0, weight=1)
        container.grid_columnconfigure(0, weight=1)

        canvas = tk.Canvas(container, highlightthickness=0, bd=0, bg=self.root.cget("bg"))
        self._scroll_canvas = canvas
        scrollbar = ctk.CTkScrollbar(container, command=canvas.yview)
        self._scrollbar = scrollbar

        canvas.grid(row=0, column=0, sticky="nsew")
        # scrollbar 初始不显示
        canvas.configure(yscrollcommand=self._on_scroll_set)

        scroll = ctk.CTkFrame(canvas, corner_radius=0, fg_color="transparent")
        self._scroll_frame = scroll
        win_id = canvas.create_window((0, 0), window=scroll, anchor="nw")

        def _on_frame_configure(e):
            canvas.configure(scrollregion=canvas.bbox("all"))
            # 只在内容超出可视区时显示滚动条
            if scroll.winfo_reqheight() > canvas.winfo_height():
                scrollbar.grid(row=0, column=1, sticky="ns")
            else:
                scrollbar.grid_remove()

        def _on_canvas_configure(e):
            canvas.itemconfig(win_id, width=e.width)
            # 窗口大小变化时也重新判断
            if scroll.winfo_reqheight() > e.height:
                scrollbar.grid(row=0, column=1, sticky="ns")
            else:
                scrollbar.grid_remove()

        scroll.bind("<Configure>", _on_frame_configure)
        canvas.bind("<Configure>", _on_canvas_configure)

        # 鼠标滚轮
        def _on_mousewheel(e):
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        scroll.grid_columnconfigure(0, weight=1)
        r = 0

        top = ctk.CTkFrame(scroll, fg_color="transparent")
        top.grid(row=r, column=0, padx=8, pady=(2, 2), sticky="ew")
        top.grid_columnconfigure(0, weight=2)
        top.grid_columnconfigure(1, weight=3)

        banner = ctk.CTkFrame(top, corner_radius=8, fg_color=("#1e40af", "#1e3a5f"))
        banner.grid(row=0, column=0, padx=(0, 4), pady=0, sticky="nsew")
        banner.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            banner,
            text="🔒  SSH Tunnel VPN",
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color="white",
            anchor="w",
        ).grid(row=0, column=0, padx=8, pady=(2, 0), sticky="w")
        ctk.CTkLabel(
            banner,
            text="安全加密隧道",
            font=ctk.CTkFont(size=11),
            text_color="#93c5fd",
            anchor="w",
        ).grid(row=1, column=0, padx=8, pady=(0, 2), sticky="w")

        sc = ctk.CTkFrame(top, corner_radius=8)
        sc.grid(row=0, column=1, padx=(4, 0), pady=0, sticky="nsew")
        sc.grid_columnconfigure(0, weight=1)
        si = ctk.CTkFrame(sc, fg_color="transparent")
        si.grid(row=0, column=0, padx=4, pady=2)

        top_status = ctk.CTkFrame(si, fg_color="transparent")
        top_status.pack()
        self.status_dot = ctk.CTkLabel(top_status, text="●", font=ctk.CTkFont(size=16), text_color=self.GREY, width=22)
        self.status_dot.pack(side="left", padx=(0, 4))
        self.status_label = ctk.CTkLabel(top_status, text="未连接", font=ctk.CTkFont(size=14, weight="bold"), text_color=self.GREY)
        self.status_label.pack(side="left")
        self.status_detail = ctk.CTkLabel(si, text="请输入服务器信息并连接", font=ctk.CTkFont(size=11), text_color=self.GREY)
        self.status_detail.pack(pady=(0, 0))
        self.stats_label = ctk.CTkLabel(si, text="", font=ctk.CTkFont(size=10), text_color=self.GREY)
        self.stats_label.pack(pady=(0, 0))
        r += 1

        # ── 服务器 & 跳板机 并排两列 ──
        cfg_row = ctk.CTkFrame(scroll, fg_color="transparent")
        cfg_row.grid(row=r, column=0, padx=8, pady=4, sticky="ew")
        cfg_row.grid_columnconfigure(0, weight=1, uniform="cfg")
        cfg_row.grid_columnconfigure(1, weight=1, uniform="cfg")

        # ── 左列：服务器配置 ──
        left_card = ctk.CTkFrame(cfg_row, corner_radius=10)
        left_card.grid(row=0, column=0, padx=(0, 4), pady=0, sticky="nsew")
        left_card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(left_card, text="服务器配置", font=ctk.CTkFont(size=14, weight="bold"), anchor="w").grid(
            row=0, column=0, padx=10, pady=(4, 1), sticky="w"
        )

        form_l = ctk.CTkFrame(left_card, fg_color="transparent")
        form_l.grid(row=1, column=0, padx=10, pady=(0, 2), sticky="ew")
        form_l.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(form_l, text="服务器 IP", font=ctk.CTkFont(size=12), width=70).grid(row=0, column=0, sticky="w", pady=(2, 1))
        self.host_entry = ctk.CTkEntry(form_l, placeholder_text="例: 54.123.45.67", height=28)
        self.host_entry.grid(row=0, column=1, sticky="ew", pady=(2, 1), padx=(4, 0))

        pf = ctk.CTkFrame(form_l, fg_color="transparent")
        pf.grid(row=1, column=0, columnspan=2, sticky="ew", pady=1)
        pf.grid_columnconfigure(1, weight=1)
        pf.grid_columnconfigure(3, weight=1)
        ctk.CTkLabel(pf, text="SSH 端口", font=ctk.CTkFont(size=12), width=70).grid(row=0, column=0, sticky="w")
        self.port_entry = ctk.CTkEntry(pf, placeholder_text="22", height=28, width=60)
        self.port_entry.grid(row=0, column=1, sticky="ew", padx=(4, 8))
        self.port_entry.insert(0, "22")
        ctk.CTkLabel(pf, text="SOCKS", font=ctk.CTkFont(size=12), width=44).grid(row=0, column=2, sticky="w")
        self.socks_entry = ctk.CTkEntry(pf, placeholder_text="10800", height=28, width=60)
        self.socks_entry.grid(row=0, column=3, sticky="ew", padx=(4, 0))
        self.socks_entry.insert(0, "10800")

        hf = ctk.CTkFrame(form_l, fg_color="transparent")
        hf.grid(row=2, column=0, columnspan=2, sticky="ew", pady=1)
        hf.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(hf, text="HTTP 端口", font=ctk.CTkFont(size=12), width=70).grid(row=0, column=0, sticky="w")
        self.http_entry = ctk.CTkEntry(hf, placeholder_text="10801", height=28, width=60)
        self.http_entry.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        self.http_entry.insert(0, "10801")

        ctk.CTkLabel(form_l, text="用户名", font=ctk.CTkFont(size=12), width=70).grid(row=3, column=0, sticky="w", pady=1)
        self.user_entry = ctk.CTkEntry(form_l, placeholder_text="root / ubuntu", height=28)
        self.user_entry.grid(row=3, column=1, sticky="ew", pady=1, padx=(4, 0))

        ctk.CTkLabel(form_l, text="密码", font=ctk.CTkFont(size=12), width=70).grid(row=4, column=0, sticky="w", pady=1)
        self.pass_entry = ctk.CTkEntry(form_l, placeholder_text="••••••••", show="●", height=28)
        self.pass_entry.grid(row=4, column=1, sticky="ew", pady=1, padx=(4, 0))

        self.use_key_var = ctk.BooleanVar(value=False)
        self.key_switch = ctk.CTkSwitch(
            form_l,
            text="使用私钥登录",
            variable=self.use_key_var,
            font=ctk.CTkFont(size=12),
            command=self._apply_auth_ui,
        )
        self.key_switch.grid(row=5, column=0, columnspan=2, sticky="w", pady=(3, 1))

        self.key_file_frame = ctk.CTkFrame(form_l, fg_color="transparent")
        self.key_file_frame.grid(row=6, column=0, columnspan=2, sticky="ew", pady=1)
        self.key_file_frame.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(self.key_file_frame, text="私钥文件", font=ctk.CTkFont(size=12), width=70).grid(row=0, column=0, sticky="w")
        self.key_path_entry = ctk.CTkEntry(self.key_file_frame, placeholder_text="~/.ssh/id_rsa", height=28)
        self.key_path_entry.grid(row=0, column=1, sticky="ew", padx=(4, 4))
        ctk.CTkButton(self.key_file_frame, text="选择", width=46, height=28, command=self._browse_target_key).grid(row=0, column=2)

        self.key_pass_label = ctk.CTkLabel(form_l, text="私钥口令", font=ctk.CTkFont(size=12), width=70)
        self.key_pass_label.grid(row=7, column=0, sticky="w", pady=1)
        self.key_pass_entry = ctk.CTkEntry(form_l, placeholder_text="(可选)", show="●", height=28)
        self.key_pass_entry.grid(row=7, column=1, sticky="ew", pady=1, padx=(4, 0))

        self.auto_proxy_var = ctk.BooleanVar(value=True)
        ctk.CTkSwitch(left_card, text="自动设置系统全局代理", variable=self.auto_proxy_var, font=ctk.CTkFont(size=12)).grid(
            row=2, column=0, padx=12, pady=(1, 1), sticky="w"
        )

        # 跳板机开关放在左卡片底部，方便在右列折叠时仍可操作
        self.use_jump_var = ctk.BooleanVar(value=False)
        self.jump_switch = ctk.CTkSwitch(
            left_card, text="启用跳板机", variable=self.use_jump_var,
            font=ctk.CTkFont(size=12), command=self._apply_jump_ui,
        )
        self.jump_switch.grid(row=3, column=0, padx=12, pady=(1, 4), sticky="w")

        # 保存 cfg_row 引用，用于折叠时调整列权重
        self._cfg_row = cfg_row

        # ── 右列：跳板机配置 ──
        self._right_card = ctk.CTkFrame(cfg_row, corner_radius=10)
        self._right_card.grid(row=0, column=1, padx=(4, 0), pady=0, sticky="nsew")
        self._right_card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self._right_card, text="跳板机配置", font=ctk.CTkFont(size=14, weight="bold"), anchor="w").grid(
            row=0, column=0, padx=10, pady=(4, 1), sticky="w"
        )

        form_r = ctk.CTkFrame(self._right_card, fg_color="transparent")
        form_r.grid(row=1, column=0, padx=10, pady=(0, 4), sticky="ew")
        form_r.grid_columnconfigure(1, weight=1)

        self._jump_ip_label = ctk.CTkLabel(form_r, text="跳板机 IP", font=ctk.CTkFont(size=12), width=70)
        self._jump_ip_label.grid(row=1, column=0, sticky="w", pady=1)
        self.jump_host_entry = ctk.CTkEntry(form_r, placeholder_text="例: 1.2.3.4", height=28)
        self.jump_host_entry.grid(row=1, column=1, sticky="ew", pady=1, padx=(4, 0))

        jf = ctk.CTkFrame(form_r, fg_color="transparent")
        jf.grid(row=2, column=0, columnspan=2, sticky="ew", pady=1)
        jf.grid_columnconfigure(1, weight=1)
        jf.grid_columnconfigure(3, weight=1)
        ctk.CTkLabel(jf, text="端口", font=ctk.CTkFont(size=12), width=70).grid(row=0, column=0, sticky="w")
        self.jump_port_entry = ctk.CTkEntry(jf, placeholder_text="22", height=28, width=60)
        self.jump_port_entry.grid(row=0, column=1, sticky="ew", padx=(4, 8))
        self.jump_port_entry.insert(0, "22")
        ctk.CTkLabel(jf, text="用户", font=ctk.CTkFont(size=12), width=36).grid(row=0, column=2, sticky="w")
        self.jump_user_entry = ctk.CTkEntry(jf, placeholder_text="(留空复用)", height=28, width=60)
        self.jump_user_entry.grid(row=0, column=3, sticky="ew", padx=(4, 0))

        self._jump_pass_label = ctk.CTkLabel(form_r, text="密码", font=ctk.CTkFont(size=12), width=70)
        self._jump_pass_label.grid(row=3, column=0, sticky="w", pady=1)
        self.jump_pass_entry = ctk.CTkEntry(form_r, placeholder_text="(留空复用)", show="●", height=28)
        self.jump_pass_entry.grid(row=3, column=1, sticky="ew", pady=1, padx=(4, 0))

        self.jump_use_key_var = ctk.BooleanVar(value=False)
        self.jump_key_switch = ctk.CTkSwitch(
            form_r,
            text="使用私钥登录",
            variable=self.jump_use_key_var,
            font=ctk.CTkFont(size=12),
            command=self._apply_auth_ui,
        )
        self.jump_key_switch.grid(row=4, column=0, columnspan=2, sticky="w", pady=(3, 1))

        self.jump_key_file_frame = ctk.CTkFrame(form_r, fg_color="transparent")
        self.jump_key_file_frame.grid(row=5, column=0, columnspan=2, sticky="ew", pady=1)
        self.jump_key_file_frame.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(self.jump_key_file_frame, text="私钥文件", font=ctk.CTkFont(size=12), width=70).grid(row=0, column=0, sticky="w")
        self.jump_key_path_entry = ctk.CTkEntry(self.jump_key_file_frame, placeholder_text="留空复用目标私钥", height=28)
        self.jump_key_path_entry.grid(row=0, column=1, sticky="ew", padx=(4, 4))
        ctk.CTkButton(self.jump_key_file_frame, text="选择", width=46, height=28, command=self._browse_jump_key).grid(row=0, column=2)

        self.jump_key_pass_label = ctk.CTkLabel(form_r, text="私钥口令", font=ctk.CTkFont(size=12), width=70)
        self.jump_key_pass_label.grid(row=6, column=0, sticky="w", pady=1)
        self.jump_key_pass_entry = ctk.CTkEntry(form_r, placeholder_text="(留空复用)", show="●", height=28)
        self.jump_key_pass_entry.grid(row=6, column=1, sticky="ew", pady=1, padx=(4, 0))

        r += 1

        btn_row = ctk.CTkFrame(scroll, fg_color="transparent")
        btn_row.grid(row=r, column=0, padx=8, pady=6, sticky="ew")
        btn_row.grid_columnconfigure(0, weight=1)

        btn_inner = ctk.CTkFrame(btn_row, fg_color="transparent")
        btn_inner.grid(row=0, column=0)

        self.connect_btn = ctk.CTkButton(
            btn_inner,
            text="🚀 连接",
            font=ctk.CTkFont(size=15, weight="bold"),
            width=150,
            height=36,
            corner_radius=10,
            command=self._toggle,
        )
        self.connect_btn.grid(row=0, column=0, padx=(0, 6), pady=0)

        self.chrome_btn = ctk.CTkButton(
            btn_inner,
            text="🌐 Chrome",
            font=ctk.CTkFont(size=13, weight="bold"),
            width=150,
            height=36,
            corner_radius=10,
            command=self._open_chrome_with_proxy,
            state="disabled",
        )
        self.chrome_btn.grid(row=0, column=1, padx=(6, 0), pady=0)
        r += 1

        tf = ctk.CTkFrame(scroll, fg_color="transparent")
        tf.grid(row=r, column=0, padx=8, pady=(0, 2), sticky="ew")
        tf.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(tf, text="外观主题", font=ctk.CTkFont(size=12)).grid(row=0, column=0, sticky="w")
        seg = ctk.CTkSegmentedButton(tf, values=["深色", "浅色", "跟随系统"], command=self._theme, font=ctk.CTkFont(size=12))
        seg.set("浅色")
        seg.grid(row=0, column=1, sticky="e")
        r += 1

        lc = ctk.CTkFrame(scroll, corner_radius=10)
        lc.grid(row=r, column=0, padx=8, pady=(4, 8), sticky="ew")
        lc.grid_columnconfigure(0, weight=1)
        lh = ctk.CTkFrame(lc, fg_color="transparent")
        lh.grid(row=0, column=0, padx=10, pady=(6, 0), sticky="ew")
        lh.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(lh, text="连接日志", font=ctk.CTkFont(size=13, weight="bold"), anchor="w").grid(row=0, column=0, sticky="w")
        ctk.CTkButton(
            lh,
            text="清除",
            width=50,
            height=26,
            font=ctk.CTkFont(size=11),
            fg_color="transparent",
            hover_color=("gray80", "gray30"),
            text_color=("gray40", "gray70"),
            command=self._clear_log,
        ).grid(row=0, column=1, sticky="e")

        self.log_box = ctk.CTkTextbox(
            lc,
            height=140,
            font=ctk.CTkFont(family="Consolas", size=12),
            corner_radius=6,
            border_width=0,
            state="disabled",
        )
        self.log_box.grid(row=1, column=0, padx=8, pady=(4, 8), sticky="ew")
        self._append_log("等待连接...")

        # 初始应用折叠/置灰逻辑
        self._apply_auth_ui()

    def _on_scroll_set(self, first, last):
        """滚动条位置回调：内容没溢出时隐藏滚动条"""
        if float(first) <= 0.0 and float(last) >= 1.0:
            self._scrollbar.grid_remove()
        else:
            self._scrollbar.grid(row=0, column=1, sticky="ns")
        self._scrollbar.set(first, last)

    def _apply_auth_ui(self):
        self._apply_target_auth_ui()
        self._apply_jump_ui()

    def _apply_jump_ui(self):
        """根据跳板机开关折叠/展开整个右列跳板机卡片"""
        use_jump = bool(self.use_jump_var.get())
        if use_jump:
            self._right_card.grid()
            self._cfg_row.grid_columnconfigure(1, weight=1, uniform="cfg")
            self._apply_jump_auth_ui()
        else:
            self._right_card.grid_remove()
            self._cfg_row.grid_columnconfigure(1, weight=0, uniform="")

    def _apply_target_auth_ui(self):
        use_key = bool(self.use_key_var.get())

        if use_key:
            self.key_file_frame.grid()
            self.key_pass_label.grid()
            self.key_pass_entry.grid()
        else:
            self.key_file_frame.grid_remove()
            self.key_pass_label.grid_remove()
            self.key_pass_entry.grid_remove()

        # 密码框：私钥模式置灰不可填；密码模式可填（但仍受整体输入开关影响）
        if not self._inputs_enabled or use_key:
            self.pass_entry.configure(state="disabled")
        else:
            self.pass_entry.configure(state="normal")

    def _apply_jump_auth_ui(self):
        use_key = bool(self.jump_use_key_var.get())

        if use_key:
            self.jump_key_file_frame.grid()
            self.jump_key_pass_label.grid()
            self.jump_key_pass_entry.grid()
        else:
            self.jump_key_file_frame.grid_remove()
            self.jump_key_pass_label.grid_remove()
            self.jump_key_pass_entry.grid_remove()

        if not self._inputs_enabled or use_key:
            self.jump_pass_entry.configure(state="disabled")
        else:
            self.jump_pass_entry.configure(state="normal")

    def _browse_target_key(self):
        from tkinter import filedialog

        init_dir = str(Path.home() / ".ssh")
        path = filedialog.askopenfilename(title="选择目标私钥文件", initialdir=init_dir)
        if path:
            self.key_path_entry.delete(0, "end")
            self.key_path_entry.insert(0, path)
            self.use_key_var.set(True)
            self._apply_auth_ui()

    def _browse_jump_key(self):
        from tkinter import filedialog

        init_dir = str(Path.home() / ".ssh")
        path = filedialog.askopenfilename(title="选择跳板机私钥文件", initialdir=init_dir)
        if path:
            self.jump_key_path_entry.delete(0, "end")
            self.jump_key_path_entry.insert(0, path)
            self.jump_use_key_var.set(True)
            self._apply_auth_ui()

    def _load_saved_config(self):
        try:
            c = load_config()
            if c.host:
                self.host_entry.insert(0, c.host)
            if c.username:
                self.user_entry.insert(0, c.username)
            if c.password:
                self.pass_entry.delete(0, "end")
                self.pass_entry.insert(0, c.password)

            self.use_key_var.set(c.use_key)
            if c.key_path:
                self.key_path_entry.insert(0, c.key_path)
            if c.key_passphrase:
                self.key_pass_entry.delete(0, "end")
                self.key_pass_entry.insert(0, c.key_passphrase)
            if not self.key_path_entry.get().strip():
                default_key = _detect_default_private_key_path()
                if default_key:
                    self.key_path_entry.insert(0, default_key)

            if c.port != 22:
                self.port_entry.delete(0, "end")
                self.port_entry.insert(0, str(c.port))
            if c.socks_port != 10800:
                self.socks_entry.delete(0, "end")
                self.socks_entry.insert(0, str(c.socks_port))
            if c.http_port != 10801:
                self.http_entry.delete(0, "end")
                self.http_entry.insert(0, str(c.http_port))

            self.use_jump_var.set(c.use_jump)
            if c.jump_host:
                self.jump_host_entry.insert(0, c.jump_host)
            if c.jump_port != 22:
                self.jump_port_entry.delete(0, "end")
                self.jump_port_entry.insert(0, str(c.jump_port))
            if c.jump_username:
                self.jump_user_entry.insert(0, c.jump_username)
            if c.jump_password:
                self.jump_pass_entry.delete(0, "end")
                self.jump_pass_entry.insert(0, c.jump_password)

            self.jump_use_key_var.set(c.jump_use_key)
            if c.jump_key_path:
                self.jump_key_path_entry.insert(0, c.jump_key_path)
            if c.jump_key_passphrase:
                self.jump_key_pass_entry.delete(0, "end")
                self.jump_key_pass_entry.insert(0, c.jump_key_passphrase)
            if not self.jump_key_path_entry.get().strip():
                # 跳板机也默认填充本机默认私钥（更省事）；隧道层仍支持留空复用目标私钥
                default_key = self.key_path_entry.get().strip() or _detect_default_private_key_path()
                if default_key:
                    self.jump_key_path_entry.insert(0, default_key)

            self.auto_proxy_var.set(c.auto_set_proxy)
        except Exception:
            pass

        self._apply_auth_ui()

        # 即使配置加载失败，也尽量预填默认私钥路径
        try:
            if not self.key_path_entry.get().strip():
                default_key = _detect_default_private_key_path()
                if default_key:
                    self.key_path_entry.insert(0, default_key)
            if not self.jump_key_path_entry.get().strip():
                default_key = self.key_path_entry.get().strip() or _detect_default_private_key_path()
                if default_key:
                    self.jump_key_path_entry.insert(0, default_key)
        except Exception:
            pass

    def _save_cfg(self):
        try:
            save_config(
                ServerConfig(
                    host=self.host_entry.get().strip(),
                    port=int(self.port_entry.get().strip() or "22"),
                    username=self.user_entry.get().strip(),
                    password=self.pass_entry.get().strip(),
                    use_key=self.use_key_var.get(),
                    key_path=self.key_path_entry.get().strip(),
                    key_passphrase=self.key_pass_entry.get().strip(),
                    use_jump=self.use_jump_var.get(),
                    jump_host=self.jump_host_entry.get().strip(),
                    jump_port=int(self.jump_port_entry.get().strip() or "22"),
                    jump_username=self.jump_user_entry.get().strip(),
                    jump_password=self.jump_pass_entry.get().strip(),
                    jump_use_key=self.jump_use_key_var.get(),
                    jump_key_path=self.jump_key_path_entry.get().strip(),
                    jump_key_passphrase=self.jump_key_pass_entry.get().strip(),
                    socks_port=int(self.socks_entry.get().strip() or "10800"),
                    http_port=int(self.http_entry.get().strip() or "10801"),
                    auto_set_proxy=self.auto_proxy_var.get(),
                )
            )
        except Exception:
            pass

    def _toggle(self):
        if self.is_connected:
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        host = self.host_entry.get().strip()
        user = self.user_entry.get().strip()
        pwd = self.pass_entry.get().strip()

        use_key = self.use_key_var.get()
        key_path = self.key_path_entry.get().strip()
        key_pass = self.key_pass_entry.get().strip()

        use_jump = self.use_jump_var.get()
        jump_host = self.jump_host_entry.get().strip()
        jump_user = self.jump_user_entry.get().strip()
        jump_pwd = self.jump_pass_entry.get().strip()

        jump_use_key = self.jump_use_key_var.get()
        jump_key_path = self.jump_key_path_entry.get().strip()
        jump_key_pass = self.jump_key_pass_entry.get().strip()

        for entry, name in [(self.host_entry, "服务器IP"), (self.user_entry, "用户名")]:
            if not entry.get().strip():
                entry.configure(border_color=self.RED)
                self._append_log(f"❌ 请输入{name}")
                self.root.after(1500, lambda e=entry: e.configure(border_color=("gray50", "gray30")))
                return

        if not use_key and not pwd:
            self.pass_entry.configure(border_color=self.RED)
            self._append_log("❌ 密码/私钥至少提供一种")
            self.root.after(1500, lambda e=self.pass_entry: e.configure(border_color=("gray50", "gray30")))
            return

        try:
            port = int(self.port_entry.get().strip() or "22")
            socks = int(self.socks_entry.get().strip() or "10800")
            http = int(self.http_entry.get().strip() or "10801")
            jump_port = int(self.jump_port_entry.get().strip() or "22")
        except ValueError:
            self._append_log("❌ 端口号必须是数字")
            return

        if use_key and not key_path:
            self._append_log("❌ 已启用私钥登录，请选择目标私钥文件")
            return

        if use_jump and not jump_host:
            self._append_log("❌ 已启用跳板机，请填写跳板机 IP")
            return

        if jump_use_key and not (jump_key_path or key_path):
            self._append_log("❌ 跳板机已启用私钥登录，请选择跳板机私钥文件（或先选择目标私钥以复用）")
            return

        self._save_cfg()
        self.connect_btn.configure(state="disabled", text="⏳  连接中...")
        self._set_inputs(False)

        def work():
            try:
                self.tunnel.connect(
                    host,
                    port,
                    user,
                    pwd,
                    socks,
                    http,
                    use_key=use_key,
                    key_path=key_path,
                    key_passphrase=key_pass,
                    use_jump=use_jump,
                    jump_host=jump_host,
                    jump_port=jump_port,
                    jump_username=jump_user,
                    jump_password=jump_pwd,
                    jump_use_key=jump_use_key,
                    jump_key_path=jump_key_path,
                    jump_key_passphrase=jump_key_pass,
                )
                if self.auto_proxy_var.get():
                    self.root.after(0, lambda: self._set_proxy(http, socks))
            except Exception as e:
                msg = str(e)
                self.root.after(0, lambda m=msg: self._conn_err(m))

        threading.Thread(target=work, daemon=True).start()

    def _set_proxy(self, http_port, socks_port):
        if set_system_proxy(http_port, socks_port):
            self.proxy_enabled = True
            self._append_log(f"✅ 系统代理 → HTTP=127.0.0.1:{http_port}  SOCKS=127.0.0.1:{socks_port}")
        else:
            self._append_log("⚠️ 设置系统代理失败")

    def _conn_err(self, msg: str):
        self.connect_btn.configure(state="normal", text="🚀  连  接")
        self._set_inputs(True)
        from tkinter import messagebox

        messagebox.showerror("连接失败", msg)

    def _disconnect(self):
        if self.proxy_enabled:
            clear_system_proxy()
            self.proxy_enabled = False
            self._append_log("系统代理已清除")
        threading.Thread(target=self.tunnel.disconnect, daemon=True).start()

    def _set_inputs(self, on: bool):
        self._inputs_enabled = on
        s = "normal" if on else "disabled"
        for w in (
            self.host_entry,
            self.port_entry,
            self.socks_entry,
            self.http_entry,
            self.user_entry,
            self.pass_entry,
            self.key_path_entry,
            self.key_pass_entry,
            self.jump_host_entry,
            self.jump_port_entry,
            self.jump_user_entry,
            self.jump_pass_entry,
            self.jump_key_path_entry,
            self.jump_key_pass_entry,
        ):
            w.configure(state=s)
        self.key_switch.configure(state=s)
        self.jump_switch.configure(state=s)
        self.jump_key_switch.configure(state=s)

        # 在整体启用/禁用后，再应用互斥逻辑（避免把密码框错误地重新启用）
        self._apply_auth_ui()

    def _on_log(self, msg: str):
        self.root.after(0, lambda: self._append_log(msg))

    def _on_status_changed(self, status: str, msg: str):
        self.root.after(0, lambda: self._update_status(status, msg))

    def _update_status(self, status: str, msg: str):
        if status == "connected":
            self.is_connected = True
            self.status_dot.configure(text_color=self.GREEN)
            self.status_label.configure(text="已连接", text_color=self.GREEN)
            self.status_detail.configure(text="HTTP/HTTPS 流量通过 SSH 隧道加密转发")
            self.connect_btn.configure(state="normal", text="⏹  断开连接", fg_color=self.RED, hover_color="#dc2626")
            self.chrome_btn.configure(state="normal")
            self._set_inputs(False)
            self._start_stats()
        elif status == "connecting":
            self.status_dot.configure(text_color=self.ORANGE)
            self.status_label.configure(text="连接中…", text_color=self.ORANGE)
            self.status_detail.configure(text=msg)
        else:
            self.is_connected = False
            self.status_dot.configure(text_color=self.GREY)
            self.status_label.configure(text="未连接", text_color=self.GREY)
            self.status_detail.configure(text="请输入服务器信息并连接")
            self.stats_label.configure(text="")
            self.connect_btn.configure(
                state="normal",
                text="🚀  连  接",
                fg_color=("#3b82f6", "#1d4ed8"),
                hover_color=("#2563eb", "#1e40af"),
            )
            self.chrome_btn.configure(state="disabled")
            self._set_inputs(True)
            self._stop_stats()
            if self.proxy_enabled:
                clear_system_proxy()
                self.proxy_enabled = False

    def _start_stats(self):
        self._stop_stats()
        self._update_stats()

    def _stop_stats(self):
        if self._stats_job:
            self.root.after_cancel(self._stats_job)
            self._stats_job = None

    def _update_stats(self):
        if not self.is_connected:
            return
        stats = self.tunnel.get_stats()
        up_mb = stats["bytes_up"] / (1024 * 1024)
        down_mb = stats["bytes_down"] / (1024 * 1024)
        active = stats["active"]
        self.stats_label.configure(text=f"↑ {up_mb:.1f} MB   ↓ {down_mb:.1f} MB   活跃连接: {active}")
        self._stats_job = self.root.after(3000, self._update_stats)

    def _append_log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{ts}] {msg}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def _theme(self, choice: str):
        self.ctk.set_appearance_mode({"深色": "dark", "浅色": "light", "跟随系统": "system"}.get(choice, "light"))
        self.root.after(100, lambda: self._scroll_canvas.configure(bg=self.root.cget("bg")))

    def _open_chrome_with_proxy(self):
        """用无痕模式 + 代理参数启动 Chrome，不影响已有窗口"""
        try:
            http_port = int(self.http_entry.get().strip() or "10801")
        except ValueError:
            self._append_log("❌ HTTP 端口必须是数字")
            return

        candidates = [
            os.path.join(os.environ.get("ProgramFiles", r"C:\\Program Files"), r"Google\\Chrome\\Application\\chrome.exe"),
            os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\\Program Files (x86)"), r"Google\\Chrome\\Application\\chrome.exe"),
            os.path.join(os.environ.get("LocalAppData", ""), r"Google\\Chrome\\Application\\chrome.exe"),
        ]
        chrome_path = next((p for p in candidates if p and os.path.exists(p)), None)
        if not chrome_path:
            self._append_log("❌ 未找到 Chrome，请确认已安装 Google Chrome")
            return

        proxy_arg = f"--proxy-server=http=127.0.0.1:{http_port};https=127.0.0.1:{http_port}"
        args = [
            "--incognito",
            proxy_arg,
            "--proxy-bypass-list=<-loopback>",
            "--disable-quic",
            "--new-window",
            "https://www.google.com",
        ]

        try:
            subprocess.Popen([chrome_path, *args], close_fds=True)
            self._append_log(f"✅ 已启动 Chrome(无痕+代理): HTTP=127.0.0.1:{http_port}")
        except Exception as e:
            self._append_log(f"❌ 启动 Chrome 失败: {e}")

    def _on_close(self):
        try:
            save_window_geometry(self.root.geometry())
        except Exception:
            pass
        if self.is_connected:
            from tkinter import messagebox

            if not messagebox.askyesno("确认退出", "VPN 正在运行，确定关闭吗？"):
                return
            if self.proxy_enabled:
                clear_system_proxy()
            self.tunnel.disconnect()
        self.root.destroy()
        sys.exit(0)


class SSHTunnelCLI:
    """CLI 命令行模式"""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        socks_port: int = 10800,
        http_port: int = 10801,
        use_key: bool = False,
        key_path: str = "",
        key_passphrase: str = "",
        use_jump: bool = False,
        jump_host: str = "",
        jump_port: int = 22,
        jump_username: str = "",
        jump_password: str = "",
        jump_use_key: bool = False,
        jump_key_path: str = "",
        jump_key_passphrase: str = "",
        set_proxy: bool = True,
        save: bool = True,
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.socks_port = socks_port
        self.http_port = http_port

        self.use_key = bool(use_key or key_path)
        self.key_path = key_path
        self.key_passphrase = key_passphrase

        self.use_jump = use_jump
        self.jump_host = jump_host
        self.jump_port = jump_port
        self.jump_username = jump_username or username
        self.jump_password = jump_password or password

        self.jump_use_key = bool(jump_use_key or jump_key_path)
        self.jump_key_path = jump_key_path
        self.jump_key_passphrase = jump_key_passphrase

        self.set_proxy = set_proxy
        self.save = save

        self.tunnel = SshTunnelManager()
        self.tunnel.on_log = self._on_log
        self.tunnel.on_status_changed = self._on_status

        self._running = threading.Event()
        self._proxy_set = False

    def start(self):
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        print(f"\n{'=' * 56}")
        print(f"  SSH Tunnel VPN  — 命令行模式")
        print(f"{'=' * 56}")
        print(f"  服务器:    {self.host}:{self.port}")
        print(f"  用户名:    {self.username}")
        print(f"  登录方式:  {'私钥' if self.use_key else '密码'}")
        if self.use_jump:
            print(f"  跳板机:    {self.jump_host}:{self.jump_port} ({self.jump_username})")
            print(f"  跳板机登录:{'私钥' if self.jump_use_key else '密码'}")
        print(f"  SOCKS端口: 127.0.0.1:{self.socks_port}")
        print(f"  HTTP端口:  127.0.0.1:{self.http_port}")
        print(f"  系统代理:  {'自动设置' if self.set_proxy else '不设置'}")
        print(f"{'=' * 56}\n")

        if self.save:
            try:
                save_config(
                    ServerConfig(
                        host=self.host,
                        port=self.port,
                        username=self.username,
                        password=self.password,
                        use_key=self.use_key,
                        key_path=self.key_path,
                        key_passphrase=self.key_passphrase,
                        use_jump=self.use_jump,
                        jump_host=self.jump_host,
                        jump_port=self.jump_port,
                        jump_username=self.jump_username,
                        jump_password=self.jump_password,
                        jump_use_key=self.jump_use_key,
                        jump_key_path=self.jump_key_path,
                        jump_key_passphrase=self.jump_key_passphrase,
                        socks_port=self.socks_port,
                        http_port=self.http_port,
                        auto_set_proxy=self.set_proxy,
                    )
                )
                logger.info("配置已保存")
            except Exception:
                pass

        try:
            self.tunnel.connect(
                self.host,
                self.port,
                self.username,
                self.password,
                self.socks_port,
                self.http_port,
                use_key=self.use_key,
                key_path=self.key_path,
                key_passphrase=self.key_passphrase,
                use_jump=self.use_jump,
                jump_host=self.jump_host,
                jump_port=self.jump_port,
                jump_username=self.jump_username,
                jump_password=self.jump_password,
                jump_use_key=self.jump_use_key,
                jump_key_path=self.jump_key_path,
                jump_key_passphrase=self.jump_key_passphrase,
            )
        except Exception as e:
            logger.error(f"连接失败: {e}")
            sys.exit(1)

        if self.set_proxy:
            if set_system_proxy(self.http_port, self.socks_port):
                self._proxy_set = True
                logger.info(f"系统代理 → HTTP=127.0.0.1:{self.http_port}  SOCKS=127.0.0.1:{self.socks_port}")
            else:
                logger.warning("设置系统代理失败")

        print("\n✅ 隧道已建立！按 Ctrl+C 断开\n")

        self._running.set()
        try:
            while self._running.is_set():
                time.sleep(5)
                if self.tunnel.is_connected:
                    stats = self.tunnel.get_stats()
                    up = stats["bytes_up"] / (1024 * 1024)
                    down = stats["bytes_down"] / (1024 * 1024)
                    active = stats["active"]
                    sys.stdout.write(f"\r  ↑ {up:.1f} MB  ↓ {down:.1f} MB  活跃连接: {active}    ")
                    sys.stdout.flush()
                else:
                    logger.warning("连接已断开")
                    break
        except KeyboardInterrupt:
            pass
        finally:
            self._cleanup()

    def _handle_signal(self, signum, frame):
        print("\n\n⏹  收到终止信号，正在断开...")
        self._running.clear()

    def _cleanup(self):
        if self._proxy_set:
            clear_system_proxy()
            self._proxy_set = False
            logger.info("系统代理已清除")
        self.tunnel.disconnect()
        print("\n🔌 已断开连接，再见！\n")

    @staticmethod
    def _on_log(msg: str):
        logger.info(msg)

    @staticmethod
    def _on_status(status: str, msg: str):
        if status == "connected":
            logger.info(f"[已连接] {msg}")
        elif status == "connecting":
            logger.info(f"[连接中] {msg}")
        else:
            logger.info(f"[已断开] {msg}")


def _run_uninstall():
    """卸载：还原系统代理、删除配置文件、清理临时目录"""
    import winreg

    print("=" * 40)
    print("  SSH Tunnel VPN - 卸载")
    print("=" * 40)
    print()

    # 1. 还原系统代理
    print("[1/3] 还原系统代理设置 ...")
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            0, winreg.KEY_SET_VALUE,
        )
        winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
        try:
            winreg.DeleteValue(key, "ProxyServer")
        except FileNotFoundError:
            pass
        winreg.CloseKey(key)
        print("      已关闭系统代理")
    except Exception as e:
        print(f"      跳过（{e}）")

    # 2. 删除配置文件
    print("[2/3] 删除配置文件 ...")
    config_dir = Path(os.environ.get("APPDATA", Path.home() / ".config")) / "SSHTunnelVPN"
    if config_dir.exists():
        shutil.rmtree(config_dir, ignore_errors=True)
        print(f"      已删除 {config_dir}")
    else:
        print("      未找到配置目录，跳过")

    # 3. 清理 PyInstaller 临时目录
    print("[3/3] 清理临时文件 ...")
    tmp = Path(tempfile.gettempdir())
    count = 0
    for d in tmp.glob("_MEI*"):
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
            count += 1
    print(f"      已清理 {count} 个临时目录")

    print()
    print("=" * 40)
    print("  卸载完成！可删除本程序文件。")
    print("=" * 40)


def main():
    parser = argparse.ArgumentParser(
        description="SSH Tunnel VPN — 安全加密隧道 (Python + C)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="mode")

    sub.add_parser("gui", help="图形界面模式 (默认)")

    sub.add_parser("uninstall", help="卸载：清理配置、还原系统代理")

    cli_p = sub.add_parser("cli", help="命令行模式")
    cli_p.add_argument("-H", "--host", type=str, default=None, help="服务器 IP / 域名")
    cli_p.add_argument("-P", "--port", type=int, default=22, help="SSH 端口 (默认 22)")
    cli_p.add_argument("-u", "--user", type=str, default=None, help="用户名")
    cli_p.add_argument("-p", "--password", type=str, default=None, help="密码(私钥登录可留空)")
    cli_p.add_argument("--key", type=str, default=None, help="目标私钥文件路径")
    cli_p.add_argument("--key-passphrase", type=str, default=None, help="目标私钥口令(可选)")

    cli_p.add_argument("--jump-host", type=str, default=None, help="跳板机 IP / 域名")
    cli_p.add_argument("--jump-port", type=int, default=22, help="跳板机 SSH 端口 (默认 22)")
    cli_p.add_argument("--jump-user", type=str, default=None, help="跳板机用户名(可选，留空复用)")
    cli_p.add_argument("--jump-password", type=str, default=None, help="跳板机密码(可选，留空复用)")
    cli_p.add_argument("--jump-key", type=str, default=None, help="跳板机私钥文件路径")
    cli_p.add_argument("--jump-key-passphrase", type=str, default=None, help="跳板机私钥口令(可选)")

    cli_p.add_argument("-s", "--socks", type=int, default=10800, help="本地 SOCKS5 端口 (默认 10800)")
    cli_p.add_argument("--http", type=int, default=10801, help="本地 HTTP 代理端口 (默认 10801)")
    cli_p.add_argument("--proxy", dest="proxy", action="store_true", default=True, help="自动设置系统代理 (默认)")
    cli_p.add_argument("--no-proxy", dest="proxy", action="store_false", help="不设置系统代理")
    cli_p.add_argument("--no-save", dest="save_cfg", action="store_false", default=True, help="不保存配置")

    args = parser.parse_args()

    if args.mode is None or args.mode == "gui":
        app = SSHTunnelApp()
        app.run()
        return

    if args.mode == "uninstall":
        _run_uninstall()
        return

    saved = load_config()

    host = args.host or saved.host
    user = args.user or saved.username
    pwd = args.password if args.password is not None else saved.password

    key_path = args.key if args.key is not None else (saved.key_path if saved.use_key else "")
    key_pass = args.key_passphrase if args.key_passphrase is not None else (saved.key_passphrase if saved.use_key else "")
    if not key_path and not pwd:
        # CLI 未提供密码也未指定 --key 时，自动尝试使用默认私钥
        key_path = _detect_default_private_key_path()
    use_key = bool(key_path)

    if args.port == 22 and saved.port != 22:
        args.port = saved.port
    if args.socks == 10800 and saved.socks_port != 10800:
        args.socks = saved.socks_port
    if args.http == 10801 and saved.http_port != 10801:
        args.http = saved.http_port

    jump_host = args.jump_host or (saved.jump_host if saved.use_jump else "")
    use_jump = bool(jump_host)
    jump_user = args.jump_user if args.jump_user is not None else (saved.jump_username if saved.use_jump else "")
    jump_pwd = args.jump_password if args.jump_password is not None else (saved.jump_password if saved.use_jump else "")

    if args.jump_port == 22 and saved.jump_port != 22:
        args.jump_port = saved.jump_port

    jump_key_path = args.jump_key if args.jump_key is not None else (saved.jump_key_path if saved.jump_use_key else "")
    jump_key_pass = (
        args.jump_key_passphrase
        if args.jump_key_passphrase is not None
        else (saved.jump_key_passphrase if saved.jump_use_key else "")
    )
    if use_jump and not jump_key_path:
        # 跳板机未指定 --jump-key 时：优先复用目标私钥，否则探测默认私钥
        jump_key_path = key_path or _detect_default_private_key_path()
    jump_use_key = bool(jump_key_path)

    if not host or not user:
        print("❌ 错误: CLI 模式需要提供 --host 和 --user (或已保存过配置)")
        sys.exit(1)

    if not use_key and not pwd:
        print("❌ 错误: 请提供 --password 或 --key")
        sys.exit(1)

    cli = SSHTunnelCLI(
        host=host,
        port=args.port,
        username=user,
        password=pwd or "",
        socks_port=args.socks,
        http_port=args.http,
        use_key=use_key,
        key_path=key_path or "",
        key_passphrase=key_pass or "",
        use_jump=use_jump,
        jump_host=jump_host or "",
        jump_port=args.jump_port,
        jump_username=jump_user or "",
        jump_password=jump_pwd or "",
        jump_use_key=jump_use_key,
        jump_key_path=jump_key_path or "",
        jump_key_passphrase=jump_key_pass or "",
        set_proxy=args.proxy,
        save=args.save_cfg,
    )
    cli.start()


if __name__ == "__main__":
    main()

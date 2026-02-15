# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec 文件 - SSH Tunnel VPN
生成单文件 exe，含 CustomTkinter 资源
"""
import os
import sys

# customtkinter 资源路径
import customtkinter
ctk_path = os.path.dirname(customtkinter.__file__)

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        (ctk_path, 'customtkinter'),   # CustomTkinter 主题/JSON 资源
    ],
    hiddenimports=[
        'paramiko',
        'paramiko.transport',
        'paramiko.rsakey',
        'paramiko.ecdsakey',
        'paramiko.ed25519key',
        'paramiko.agent',
        'paramiko.auth_handler',
        'paramiko.channel',
        'paramiko.sftp',
        'paramiko.sftp_client',
        'paramiko.sftp_server',
        'paramiko.hostkeys',
        'paramiko.config',
        'paramiko.proxy',
        'paramiko.ssh_exception',
        'pysocks',
        'socks',
        'customtkinter',
        'PIL',
        'PIL._tkinter_finder',
        'tkinter',
        'tkinter.filedialog',
        'tkinter.messagebox',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='SSHTunnelVPN',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # 无控制台窗口（GUI 应用）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,              # 可替换为 .ico 图标路径
)

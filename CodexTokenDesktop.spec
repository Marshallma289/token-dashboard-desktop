# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path

is_macos = sys.platform == 'darwin'
platform_imports = ['webview.platforms.cocoa'] if is_macos else [
    'webview.platforms.edgechromium', 'webview.platforms.winforms'
]
icon_path = 'assets/app-icon.icns' if is_macos else 'assets/app-icon.ico'
version = Path('VERSION').read_text(encoding='utf-8').strip()

a = Analysis(
    ['desktop.py'],
    pathex=[],
    binaries=[],
    datas=[('web', 'web'), ('LICENSE', '.'), ('THIRD_PARTY_NOTICES.md', '.')],
    hiddenimports=['webview', *platform_imports],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='CodexTokenDesktop',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=not is_macos,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    icon=icon_path,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=not is_macos,
    upx_exclude=[],
    name='CodexTokenDesktop',
)

if is_macos:
    app = BUNDLE(
        coll,
        name='CodexTokenDesktop.app',
        icon=icon_path,
        bundle_identifier='local.codextokendashboard',
        info_plist={
            'CFBundleName': 'Codex Token',
            'CFBundleDisplayName': 'Codex Token',
            'CFBundleShortVersionString': version,
            'CFBundleVersion': version,
            'LSMinimumSystemVersion': '14.0',
            'NSHighResolutionCapable': True,
            'NSAppTransportSecurity': {'NSAllowsLocalNetworking': True},
        },
    )

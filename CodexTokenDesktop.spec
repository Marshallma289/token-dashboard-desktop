# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path

is_macos = sys.platform == 'darwin'
platform_imports = ['webview.platforms.cocoa'] if is_macos else [
    'webview.platforms.edgechromium', 'webview.platforms.winforms'
]
icon_path = 'assets/app-icon.icns' if is_macos else 'assets/app-icon.ico'
version = Path('VERSION').read_text(encoding='utf-8').strip()

# These renderers are not used by either supported desktop package. Keep the
# WinForms/MSHTML fallback on Windows: WinForms hosts EdgeChromium as well.
excluded_modules = [
    'webview.platforms.android', 'webview.platforms.gtk',
    'webview.platforms.qt', 'webview.platforms.cef',
    'qtpy', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6',
    'gi', 'kivy', 'cefpython3',
]
if is_macos:
    excluded_modules += [
        'webview.platforms.winforms', 'webview.platforms.edgechromium',
        'webview.platforms.mshtml', 'webview.platforms.win32',
    ]
else:
    excluded_modules += [
        'webview.platforms.cocoa',
        # pythonnet/clr_loader use CFFI's cdef/dlopen ABI mode. Its optional
        # C compiler/build-system branch is not used by the application.
        'cffi.recompiler', 'cffi.verifier', 'cffi.ffiplatform',
        'cffi.setuptools_ext', 'cffi._shimmed_dist_utils',
        'setuptools', 'distutils', '_distutils_hack',
    ]

a = Analysis(
    ['desktop.py'],
    pathex=[],
    binaries=[],
    datas=[('web', 'web'), ('VERSION', '.'), ('build-info.json', '.'), ('LICENSE', '.'), ('THIRD_PARTY_NOTICES.md', '.')],
    hiddenimports=['webview', *platform_imports],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excluded_modules,
    noarchive=False,
    optimize=0,
)

if not is_macos:
    # The upstream hook also collects Android and editor-only resources.
    # Keep all WebView2Loader directories: edgechromium looks up all three
    # architectures unconditionally when extending PATH during import.
    interop_arch = 'x64' if sys.maxsize > 2**32 else 'x86'
    clr_arch = 'amd64' if sys.maxsize > 2**32 else 'x86'

    def retain_runtime_resource(entry):
        name = entry[0].replace('\\', '/')
        if name in {
            'webview/lib/pywebview-android.jar',
            'pythonnet/runtime/Python.Runtime.xml',
        }:
            return False
        if name in {
            'webview/lib/WebBrowserInterop.x64.dll',
            'webview/lib/WebBrowserInterop.x86.dll',
        }:
            return name.endswith(f'.{interop_arch}.dll')
        if name.startswith('clr_loader/ffi/dlls/'):
            architecture = name.split('/')[3]
            if architecture in {'amd64', 'x86'}:
                return architecture == clr_arch
        return True

    a.binaries = [entry for entry in a.binaries if retain_runtime_resource(entry)]
    a.datas = [entry for entry in a.datas if retain_runtime_resource(entry)]

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

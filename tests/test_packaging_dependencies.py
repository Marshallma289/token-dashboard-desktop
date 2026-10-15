"""Keep the native runtime while excluding optional GUI/compiler branches."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SPEC = Path(__file__).resolve().parents[1] / "CodexTokenDesktop.spec"
RESOURCES = [
    "webview/lib/Microsoft.Web.WebView2.Core.dll",
    "webview/lib/Microsoft.Web.WebView2.WinForms.dll",
    "webview/lib/runtimes/win-x64/native/WebView2Loader.dll",
    "webview/lib/runtimes/win-x86/native/WebView2Loader.dll",
    "webview/lib/runtimes/win-arm64/native/WebView2Loader.dll",
    "webview/lib/WebBrowserInterop.x64.dll",
    "webview/lib/WebBrowserInterop.x86.dll",
    "webview/lib/pywebview-android.jar",
    "clr_loader/ffi/dlls/amd64/ClrLoader.dll",
    "clr_loader/ffi/dlls/x86/ClrLoader.dll",
    "pythonnet/runtime/Python.Runtime.dll",
    "pythonnet/runtime/Python.Runtime.deps.json",
    "pythonnet/runtime/Python.Runtime.xml",
    "webview/js/api.js",
    "web/styles.css",
    "libssl-3-x64.dll",
    "sqlite3.dll",
]


class PackagingDependencyTests(unittest.TestCase):
    def evaluate_spec(self, platform, maxsize):
        arguments = {}

        def analysis(*args, **kwargs):
            arguments.update(kwargs)
            entries = [(name, "source", "BINARY") for name in RESOURCES]
            return SimpleNamespace(pure=[], scripts=[], binaries=entries[:], datas=entries[:])

        namespace = {
            "Analysis": analysis,
            "PYZ": lambda *args, **kwargs: None,
            "EXE": lambda *args, **kwargs: None,
            "COLLECT": lambda *args, **kwargs: None,
            "BUNDLE": lambda *args, **kwargs: None,
        }
        code = compile(SPEC.read_bytes(), str(SPEC), "exec")
        with patch.object(sys, "platform", platform), patch.object(sys, "maxsize", maxsize), patch.object(Path, "read_text", return_value="1.7.0"):
            exec(code, namespace)
        return namespace["a"], arguments

    def test_windows_keeps_native_desktop_and_application_import_paths(self):
        analysis, arguments = self.evaluate_spec("win32", 2**63 - 1)
        required = (
            "webview", "webview.platforms.edgechromium", "webview.platforms.winforms",
            "webview.platforms.mshtml", "webview.platforms.win32", "clr", "pythonnet",
            "clr_loader", "cffi", "cffi.cparser", "pycparser", "_cffi_backend",
            "ssl", "sqlite3", "urllib.request", "zipfile", "updater", "pricing", "backend",
        )
        for module in required:
            self.assertFalse(any(module == excluded or module.startswith(excluded + ".") for excluded in arguments["excludes"]), module)
        self.assertIn("webview.platforms.edgechromium", arguments["hiddenimports"])
        self.assertIn("webview.platforms.winforms", arguments["hiddenimports"])
        self.assertIn("setuptools", arguments["excludes"])
        kept = {entry[0] for entry in analysis.datas}
        self.assertTrue({
            "pythonnet/runtime/Python.Runtime.dll", "pythonnet/runtime/Python.Runtime.deps.json",
            "webview/lib/Microsoft.Web.WebView2.Core.dll", "webview/lib/Microsoft.Web.WebView2.WinForms.dll",
            "webview/js/api.js", "web/styles.css", "libssl-3-x64.dll", "sqlite3.dll",
        }.issubset(kept))

    def test_windows_native_resources_follow_interpreter_architecture(self):
        for maxsize, interop, clr in (
            (2**63 - 1, "x64", "amd64"),
            (2**31 - 1, "x86", "x86"),
        ):
            with self.subTest(bits=interop):
                analysis, _ = self.evaluate_spec("win32", maxsize)
                for entries in (analysis.binaries, analysis.datas):
                    kept = {entry[0] for entry in entries}
                    loaders = {name for name in kept if "/runtimes/" in name}
                    self.assertEqual(loaders, {
                        f"webview/lib/runtimes/{runtime}/native/WebView2Loader.dll"
                        for runtime in ("win-arm64", "win-x64", "win-x86")
                    })
                    self.assertEqual({name for name in kept if "WebBrowserInterop" in name}, {f"webview/lib/WebBrowserInterop.{interop}.dll"})
                    self.assertEqual({name for name in kept if "ClrLoader.dll" in name}, {f"clr_loader/ffi/dlls/{clr}/ClrLoader.dll"})
                    self.assertNotIn("webview/lib/pywebview-android.jar", kept)
                    self.assertNotIn("pythonnet/runtime/Python.Runtime.xml", kept)

    def test_macos_keeps_cocoa_and_does_not_apply_windows_resource_filter(self):
        analysis, arguments = self.evaluate_spec("darwin", 2**63 - 1)
        self.assertIn("webview.platforms.cocoa", arguments["hiddenimports"])
        self.assertNotIn("webview.platforms.cocoa", arguments["excludes"])
        self.assertNotIn("setuptools", arguments["excludes"])
        self.assertEqual({entry[0] for entry in analysis.datas}, set(RESOURCES))


if __name__ == "__main__":
    unittest.main()

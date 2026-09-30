"""Shared Windows and macOS desktop shell for the Codex Token Dashboard."""
from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from typing import Any, Iterable, Mapping, Optional

from backend import DashboardDB, DashboardService, create_server, safe_csv_row, local_data_dir
from updater import Updater, confirm_startup


CSV_FIELDS = [
    "date",
    "model",
    "provider",
    "provider_label",
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "reasoning_output_tokens",
    "total_tokens",
    "request_count",
    "estimated_cost_usd",
    "pricing_status",
    "pricing_model",
    "pricing_rate_band",
]


def dashboard_csv(rows: Iterable[Mapping[str, Any]]) -> str:
    """Return an Excel-friendly CSV for the currently filtered model rows."""
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(safe_csv_row(row) for row in rows)
    return "\ufeff" + output.getvalue()


def application_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _wait_for_webview_cleanup() -> None:
    """Avoid WebView2's short-lived renderer handoff after a rapid restart."""
    if sys.platform != "win32":
        return
    marker = local_data_dir() / "last-clean-exit"
    try:
        elapsed = time.time() - float(marker.read_text(encoding="ascii"))
    except (OSError, ValueError):
        return
    if 0 <= elapsed < 5.0:
        time.sleep(5.0 - elapsed)


def _record_clean_exit() -> None:
    if sys.platform != "win32":
        return
    try:
        folder = local_data_dir()
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "last-clean-exit").write_text(str(time.time()), encoding="ascii")
    except OSError:
        pass


class DesktopRuntime:
    """Own the scanner and loopback-only service used by the desktop window."""

    def __init__(
        self,
        database: Optional[DashboardDB] = None,
        *,
        db_path: Optional[Path] = None,
        providers_path: Optional[Path] = None,
        interval: float = 2.0,
    ) -> None:
        data_dir = local_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        if providers_path is None:
            candidates = [data_dir / "providers.json"]
            if sys.platform != "darwin":
                candidates.insert(0, application_dir() / "providers.json")
            providers_path = next((path for path in candidates if path.is_file()), None)
        self.database = database or DashboardDB(
            db_path or data_dir / "usage.sqlite3",
            providers_path=providers_path,
        )
        self._owns_database = database is None
        self.service = DashboardService(self.database, interval=interval)
        self.server = create_server(self.service, "127.0.0.1", 0)
        self.thread: Optional[threading.Thread] = None
        self._stopped = threading.Event()

    @property
    def url(self) -> str:
        port = int(self.server.server_address[1])
        return f"http://127.0.0.1:{port}/?desktop=1"

    def start(self) -> str:
        if self.thread and self.thread.is_alive():
            return self.url
        # Show the window immediately. Existing cached totals are readable
        # while the first fresh scan runs in the background.
        self.service.start(initial_refresh=False)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="codex-token-desktop-http",
            daemon=True,
        )
        self.thread.start()
        return self.url

    def stop(self) -> None:
        if self._stopped.is_set():
            return
        self._stopped.set()
        if self.thread and self.thread.is_alive():
            self.server.shutdown()
            self.thread.join(timeout=5)
        self.server.server_close()
        self.service.stop()
        if self._owns_database:
            self.database.close()


class DesktopBridge:
    """Small native bridge; analytics remain in the local dashboard app."""

    def __init__(self) -> None:
        # pywebview exposes public attributes from ``js_api`` to JavaScript.
        # Keep the native window private; exposing it makes the bridge walker
        # recursively inspect WinForms/WebView2 objects in frozen builds.
        self._window: Any = None
        self._preferences_lock = threading.Lock()
        self._updater: Optional[Updater] = None

    def _update_manager(self) -> Updater:
        if self._updater is None:
            self._updater = Updater(local_data_dir(), lambda: self._window.destroy())
        return self._updater

    def get_update_status(self) -> Mapping[str, Any]:
        return self._update_manager().status()

    def check_for_update(self) -> Mapping[str, Any]:
        return self._update_manager().check()

    def start_update(self) -> Mapping[str, Any]:
        return self._update_manager().start()

    def load_preferences(self) -> Mapping[str, Any]:
        """Read only supported UI preferences, independent of the web origin."""
        with self._preferences_lock:
            try:
                value = json.loads((local_data_dir() / "preferences.json").read_text(encoding="utf-8"))
                return {"theme": value["theme"]} if isinstance(value, dict) and value.get("theme") in ("light", "dark") else {}
            except (OSError, ValueError):
                return {}

    def save_preferences(self, preferences: Mapping[str, Any]) -> Mapping[str, Any]:
        if not isinstance(preferences, dict) or preferences.get("theme") not in ("light", "dark"):
            return {"ok": False, "error": "无效的主题设置"}
        with self._preferences_lock:
            try:
                folder = local_data_dir()
                folder.mkdir(parents=True, exist_ok=True)
                temporary = folder / f"preferences.{os.getpid()}.tmp"
                temporary.write_text(json.dumps({"theme": preferences["theme"]}), encoding="utf-8")
                temporary.replace(folder / "preferences.json")
                return {"ok": True}
            except OSError:
                return {"ok": False, "error": "无法保存主题设置"}

    def attach_window(self, window: Any) -> None:
        self._window = window

    def save_csv(self, filename: str, content: str) -> Mapping[str, Any]:
        if self._window is None:
            return {"ok": False, "error": "窗口尚未就绪"}
        safe_name = re.sub(r"[^\w.\-\u4e00-\u9fff]", "-", filename or "codex-token.csv")
        if not safe_name.lower().endswith(".csv"):
            safe_name += ".csv"
        try:
            import webview

            selected = self._window.create_file_dialog(
                webview.SAVE_DIALOG,
                directory=str(Path.home()),
                save_filename=safe_name,
                file_types=("CSV 表格 (*.csv)",),
            )
            if not selected:
                return {"ok": False, "cancelled": True}
            path = Path(selected[0] if isinstance(selected, (tuple, list)) else selected)
            path.write_text(content, encoding="utf-8")
            return {"ok": True, "path": str(path)}
        except (OSError, RuntimeError) as error:
            return {"ok": False, "error": str(error)}


def _show_startup_error(message: str) -> None:
    if os.name == "nt":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "Codex Token 启动失败", 0x10)
    elif sys.platform == "darwin":
        script = 'on run argv\ndisplay alert "Codex Token 启动失败" message (item 1 of argv) as critical\nend run'
        try:
            subprocess.run(["osascript", "-e", script, message], check=False, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            print(message, file=sys.stderr)
    else:
        print(message, file=sys.stderr)


def main() -> int:
    try:
        import webview
    except ImportError:
        command = "py" if sys.platform == "win32" else "python3"
        _show_startup_error(f"桌面组件尚未安装。请运行：{command} -m pip install -r requirements-desktop.txt")
        return 1

    runtime: Optional[DesktopRuntime] = None
    bridge: Optional[DesktopBridge] = None
    try:
        _wait_for_webview_cleanup()
        runtime = DesktopRuntime()
        url = runtime.start()
        bridge = DesktopBridge()
        window = webview.create_window(
            "Codex Token · 实时看板",
            url,
            js_api=bridge,
            width=1440,
            height=920,
            min_size=(980, 680),
            maximized=True,
            background_color="#0e151c",
            text_select=True,
        )
        if window is None:
            raise RuntimeError("无法创建客户端窗口")
        bridge.attach_window(window)
        if '--update-job' in sys.argv:
            job_path = Path(sys.argv[sys.argv.index('--update-job') + 1])
            window.events.loaded += lambda: confirm_startup(job_path)
        webview.start(
            gui="edgechromium" if sys.platform == "win32" else None,
            debug=False,
            # An isolated profile avoids carrying browser cache or credentials
            # inside the portable package.
            private_mode=True,
        )
        return 0
    except Exception as error:
        _show_startup_error(f"客户端无法启动：\n{error}")
        return 1
    finally:
        if runtime is not None:
            runtime.stop()
            _record_clean_exit()
        if bridge is not None and bridge._updater is not None:
            try:
                bridge._updater.launch_installer()
            except Exception as error:
                _show_startup_error(f'无法启动更新安装器，原程序仍在：\n{error}')


if __name__ == "__main__":
    raise SystemExit(main())

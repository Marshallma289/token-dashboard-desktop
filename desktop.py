"""Shared Windows and macOS desktop shell for the Codex Token Dashboard."""
from __future__ import annotations

from contextlib import contextmanager
import csv
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
import tempfile
from typing import Any, Iterable, Mapping, Optional
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from backend import APP_VERSION, DashboardDB, DashboardService, create_server, safe_csv_row, local_data_dir
from skills_service import check_dependencies as check_skills_dependencies
from skills_service import forget_endpoint as forget_skills_endpoint
from skills_service import publish_endpoint as publish_skills_endpoint
from updater import Updater, cleanup_completed_updates, confirm_startup, installation_dir


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
    "speed_output_tokens",
    "speed_duration_ms",
    "speed_sample_count",
    "output_tokens_per_second",
    "cost_before_discount_usd",
    "cost_after_discount_usd",
    "cost_display_mode",
    "displayed_cost_usd",
    "fresh_input_tokens",
    "cache_read_available_count",
    "cache_write_available_count",
    "cache_read_input_tokens",
    "cache_hit_rate",
    "cache_savings_before_discount_usd",
    "cache_savings_after_discount_usd",
    "ttft_median_ms",
    "ttft_p90_ms",
    "ttft_sample_count",
    "ttft_coverage",
]

_PREFERENCES_LOCK = threading.RLock()
_UI_PREFERENCES = {"theme": ("light", "dark"), "cost_mode": ("before", "after")}


def _validated_layout(value: Any) -> Optional[dict[str, Any]]:
    """Keep bounded UI data only; never persist arbitrary bridge arguments."""
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1:
        return None
    views = ("overview", "trends", "usage", "pricing")
    pages = value.get("pages")
    if not isinstance(pages, dict):
        return None
    result: dict[str, Any] = {"version": 1, "pages": {}}
    result["view"] = value.get("view") if value.get("view") in views else "overview"
    result["usage"] = value.get("usage") if value.get("usage") in ("daily", "requests", "sessions", "tasks", "all") else "daily"
    result["pricing"] = value.get("pricing") if value.get("pricing") in ("rules", "diagnostics", "all") else "rules"
    for view in views:
        items = pages.get(view, [])
        if not isinstance(items, list) or len(items) > 30:
            return None
        accepted = []
        seen = set()
        for item in items:
            if not isinstance(item, dict):
                return None
            identity, span, height = item.get("id"), item.get("span"), item.get("height")
            if not isinstance(identity, str) or not re.fullmatch(view + r"-[a-z0-9-]{1,64}", identity):
                return None
            if identity in seen or type(span) is not int or not 3 <= span <= 12:
                return None
            if type(height) is not int or not (height == 0 or 200 <= height <= 2400):
                return None
            accepted.append({"id": identity, "span": span, "height": height})
            seen.add(identity)
        result["pages"][view] = accepted
    return result


def _read_preferences() -> dict[str, Any]:
    path = local_data_dir() / "preferences.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError("设置文件内容无效，请检查 preferences.json 或其备份")
    return value


def _write_preferences(changes: Mapping[str, Any]) -> None:
    """Merge preferences atomically so changing theme keeps the data location."""
    with _PREFERENCES_LOCK:
        value = _read_preferences()
        value.update(changes)
        folder = local_data_dir()
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / "preferences.json"
        temporary = folder / f"preferences.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            if destination.is_file():
                shutil.copy2(destination, folder / "preferences.json.bak")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)


def _configured_database() -> Path:
    with _PREFERENCES_LOCK:
        value = _read_preferences().get("database_path")
    if value is None:
        return local_data_dir() / "usage.sqlite3"
    if not isinstance(value, str) or not Path(value).expanduser().is_absolute():
        raise ValueError("历史数据保存路径无效，请检查 preferences.json 或其备份")
    path = Path(value).expanduser().resolve()
    # A missing custom database may be a disconnected drive. Never create an
    # empty replacement that makes the user's retained history appear lost.
    if not path.is_file():
        raise ValueError(f"历史数据库暂时不可用，请连接保存数据的磁盘后重试：\n{path}")
    return path


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
        interval: Optional[float] = None,
        hermes_db: Optional[Path] = None,
    ) -> None:
        data_dir = local_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        if providers_path is None:
            candidates = [data_dir / "providers.json"]
            if sys.platform != "darwin":
                candidates.insert(0, application_dir() / "providers.json")
            providers_path = next((path for path in candidates if path.is_file()), None)
        from hermes_source import resolve_hermes_paths
        self.database = database or DashboardDB(
            db_path or _configured_database(),
            providers_path=providers_path,
            hermes_db=resolve_hermes_paths(hermes_db)[0],
        )
        self._owns_database = database is None
        self._data_dir = data_dir
        self.service = DashboardService(self.database, interval=interval)
        self.server = create_server(self.service, "127.0.0.1", 0, skills_data_dir=data_dir)
        self.thread: Optional[threading.Thread] = None
        self._stopped = threading.Event()
        self._skills_autosync = None

    @property
    def url(self) -> str:
        port = int(self.server.server_address[1])
        return f"http://127.0.0.1:{port}/?desktop=1"

    def start(self) -> str:
        if self._stopped.is_set():
            raise RuntimeError("Desktop runtime has already stopped")
        if self.thread and self.thread.is_alive():
            return self.url
        self._skills_autosync = self.server.skills_service.autosync
        self._skills_autosync.start()
        # Show the window immediately. Existing cached totals are readable
        # while the first fresh scan runs in the background.
        self.service.start(initial_refresh=False)
        self.service.request_refresh()
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="codex-token-desktop-http",
            daemon=True,
        )
        self.thread.start()
        try:
            publish_skills_endpoint(self._data_dir, self.server.server_port)
        except OSError:
            pass  # Explicit --port still works when the discovery hint cannot be saved.
        return self.url

    def stop(self) -> None:
        if self._stopped.is_set():
            return
        self._stopped.set()
        if self._skills_autosync is not None:
            self._skills_autosync.stop()
        if self.thread and self.thread.is_alive():
            self.server.shutdown()
            self.thread.join(timeout=5)
        self.server.server_close()
        try:
            forget_skills_endpoint(self._data_dir, self.server.server_port)
        except OSError:
            pass
        self.service.stop()
        if self._owns_database:
            self.database.close()


class DesktopBridge:
    """Small native bridge; analytics remain in the local dashboard app."""

    def __init__(self, runtime: Optional[DesktopRuntime] = None) -> None:
        # pywebview exposes public attributes from ``js_api`` to JavaScript.
        # Keep the native window private; exposing it makes the bridge walker
        # recursively inspect WinForms/WebView2 objects in frozen builds.
        self._window: Any = None
        self._preferences_lock = _PREFERENCES_LOCK
        self._runtime = runtime
        self._data_lock = threading.Lock()
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
                value = _read_preferences()
                return {key: value[key] for key, allowed in _UI_PREFERENCES.items() if value.get(key) in allowed}
            except (OSError, ValueError):
                return {}

    def save_preferences(self, preferences: Mapping[str, Any]) -> Mapping[str, Any]:
        changes = {key: preferences[key] for key in _UI_PREFERENCES if key in preferences} if isinstance(preferences, dict) else {}
        if not changes or any(value not in _UI_PREFERENCES[key] for key, value in changes.items()):
            return {"ok": False, "error": "无效的显示设置"}
        with self._preferences_lock:
            try:
                _write_preferences(changes)
                return {"ok": True}
            except (OSError, ValueError):
                return {"ok": False, "error": "无法保存显示设置"}

    def load_layout(self) -> Mapping[str, Any]:
        """Layout survives private WebView sessions and changing loopback ports."""
        with self._preferences_lock:
            try:
                stored = _read_preferences().get("layout")
                layout = _validated_layout(stored)
                if stored is not None and layout is None:
                    return {"ok": False, "layout": None, "error": "已保存的布局暂时无法读取，原设置已保留"}
                return {"ok": True, "layout": layout}
            except (OSError, ValueError):
                return {"ok": False, "layout": None, "error": "无法读取布局设置"}

    def save_layout(self, layout: Mapping[str, Any]) -> Mapping[str, Any]:
        validated = _validated_layout(layout)
        if validated is None:
            return {"ok": False, "error": "无效的布局设置"}
        with self._preferences_lock:
            try:
                _write_preferences({"layout": validated})
                return {"ok": True}
            except (OSError, ValueError):
                return {"ok": False, "error": "无法保存布局设置"}

    def _data_database(self) -> DashboardDB:
        if self._runtime is None:
            raise RuntimeError("数据服务尚未就绪")
        return self._runtime.database

    def get_data_settings(self) -> Mapping[str, Any]:
        try:
            return {"ok": True, "settings": self._data_database().data_settings()}
        except Exception as error:
            return {"ok": False, "error": f"无法读取数据设置：{error}"}

    def preview_data_cleanup(self, days: int) -> Mapping[str, Any]:
        try:
            return {"ok": True, "preview": self._data_database().preview_cleanup(days)}
        except Exception as error:
            return {"ok": False, "error": f"无法预览清理范围：{error}"}

    def clean_history(self, days: int, cutoff_day: str) -> Mapping[str, Any]:
        if not self._data_lock.acquire(blocking=False):
            return {"ok": False, "error": "数据管理操作正在进行，请稍后再试"}
        try:
            if not isinstance(cutoff_day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", cutoff_day):
                raise ValueError("请先预览清理范围，再确认清理")
            result = self._data_database().cleanup_history(days, expected_cutoff=cutoff_day)
            self._runtime.service.notify_change()
            return result
        except Exception as error:
            return {"ok": False, "error": f"清理未完成：{error}"}
        finally:
            self._data_lock.release()

    def choose_data_directory(self) -> Mapping[str, Any]:
        if self._window is None:
            return {"ok": False, "error": "窗口尚未就绪"}
        try:
            import webview

            selected = self._window.create_file_dialog(
                webview.FileDialog.FOLDER,
                directory=str(self._data_database().db_path.parent),
                allow_multiple=False,
            )
            if not selected:
                return {"ok": False, "cancelled": True}
            return {"ok": True, "path": str(selected[0] if isinstance(selected, (list, tuple)) else selected)}
        except Exception as error:
            return {"ok": False, "error": f"无法选择文件夹：{error}"}

    def _is_skills_dialog_origin(self) -> bool:
        if self._window is None or self._runtime is None:
            return False
        current = self._window.get_current_url()
        if not isinstance(current, str):
            return False
        active, expected = urlsplit(current), urlsplit(self._runtime.url)
        return (active.scheme, active.netloc) == (expected.scheme, expected.netloc)

    def _choose_skills_path(self, agent: str, *, archive: bool) -> Mapping[str, Any]:
        # Selection returns only a path. The skills preview/import service owns
        # validation and all writes, regardless of how the path was entered.
        if agent not in ("codex", "hermes"):
            return {"ok": False, "error": "invalid_agent"}
        try:
            if not self._is_skills_dialog_origin():
                return {"ok": False, "error": "forbidden_origin"}
            import webview

            options = {"file_types": ("技能压缩包 (*.zip;*.skill)",)} if archive else {}
            selected = self._window.create_file_dialog(
                webview.FileDialog.OPEN if archive else webview.FileDialog.FOLDER,
                directory=str(Path.home()), allow_multiple=False, **options,
            )
            if not self._is_skills_dialog_origin():
                return {"ok": False, "error": "forbidden_origin"}
            if not selected:
                return {"ok": False, "cancelled": True}
            path = selected[0] if isinstance(selected, (list, tuple)) else selected
            return {"ok": True, "path": str(path)}
        except Exception:
            return {"ok": False, "error": "choose_archive_failed" if archive else "choose_directory_failed"}

    def choose_skills_directory(self, agent: str) -> Mapping[str, Any]:
        return self._choose_skills_path(agent, archive=False)

    def choose_skills_archive(self, agent: str) -> Mapping[str, Any]:
        return self._choose_skills_path(agent, archive=True)

    def set_data_directory(self, directory: str) -> Mapping[str, Any]:
        if not self._data_lock.acquire(blocking=False):
            return {"ok": False, "error": "数据管理操作正在进行，请稍后再试"}
        try:
            if not isinstance(directory, str) or not directory.strip():
                raise ValueError("请输入历史数据保存目录")
            path = Path(directory.strip()).expanduser()
            if not path.is_absolute():
                raise ValueError("请选择文件夹或输入完整的绝对路径")
            path = path.resolve()
            protected = application_dir()
            if getattr(sys, "frozen", False) and sys.platform == "darwin":
                protected = next((item for item in protected.parents if item.suffix == ".app"), protected)
            if path == protected or protected in path.parents:
                raise ValueError("请选择应用安装目录以外的位置，以免更新时影响历史数据")
            path.mkdir(parents=True, exist_ok=True)
            result = self._data_database().relocate_database(
                path, persist=lambda new_path: _write_preferences({"database_path": str(new_path)})
            )
            self._runtime.service.notify_change()
            return result
        except Exception as error:
            return {"ok": False, "error": f"迁移未完成：{error}"}
        finally:
            self._data_lock.release()

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


def run_self_test() -> Mapping[str, Any]:
    """Exercise the shipped backend and assets using only synthetic data.

    This deliberately does not initialize preferences, the updater or a GUI.
    The frozen executable can run this check on native build machines too.
    """
    checks = []
    if getattr(sys, "frozen", False):
        check_skills_dependencies()
        checks.append("frozen_skills_imports")
    with tempfile.TemporaryDirectory(prefix="codex-token-self-test-") as folder:
        root = Path(folder)
        sessions = root / "sessions"
        sessions.mkdir()
        def event(minute, second, kind, payload):
            return {"timestamp": f"2026-10-05T12:{minute:02d}:{second:02d}Z",
                    "type": kind, "payload": payload}
        rows = [event(0, 0, "session_meta", {"id": "self-test-thread", "cwd": "Self-test workspace",
                                              "model_provider": "openai"})]
        for index, duration in enumerate((20000, 40000, None), 1):
            turn_id = f"self-test-turn-{index}"
            rows.extend([
                event(index, 0, "event_msg", {"type": "task_started", "turn_id": turn_id}),
                event(index, 0, "turn_context", {"turn_id": turn_id,
                     "model": "self-test-model" if duration else "self-test-pending"}),
                event(index, 9, "response_item", {"type": "message", "role": "assistant", "content": []}),
                event(index, 10, "token_usage_record", {
                    "thread_id": "self-test-thread", "turn_id": turn_id, "response_id": f"self-test-response-{index}",
                    "usage": {"input_tokens": 5000, "cached_input_tokens": 4575,
                              "output_tokens": 1000 if duration else 250,
                              "reasoning_output_tokens": 100, "total_tokens": 6000 if duration else 5250}}),
                event(index, 12, "event_msg", {"type": "token_count", "info": {"last_token_usage": {
                    "input_tokens": 5000, "cached_input_tokens": 4575,
                    "output_tokens": 1000 if duration else 250,
                    "reasoning_output_tokens": 100, "total_tokens": 6000 if duration else 5250}}}),
            ])
            if duration:
                rows.append(event(index, 50, "event_msg", {"type": "task_complete", "turn_id": turn_id,
                                                           "duration_ms": duration,
                                                           "time_to_first_token_ms": 1500 * index}))
        log = sessions / "self-test.jsonl"
        log.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        shutil.copy2(log, sessions / "duplicate.jsonl")
        database_path = root / "usage.sqlite3"
        db = DashboardDB(database_path, roots=[sessions], providers_path=root / "providers.json", timezone_name="UTC")
        server = None
        thread = None
        try:
            service = DashboardService(db)
            service.refresh()
            server = create_server(service, "127.0.0.1", 0, skills_data_dir=root / "skills-data")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{server.server_port}"
            def read(path):
                with urlopen(url + path, timeout=5) as response:
                    return response.read().decode("utf-8")
            def write(path, value):
                request = Request(url + path, data=json.dumps(value).encode("utf-8"),
                                  headers={"Content-Type": "application/json", "X-Dashboard-Request": "1",
                                           "Origin": url}, method="POST")
                with urlopen(request, timeout=5) as response:
                    return json.loads(response.read().decode("utf-8"))
            try:
                write("/api/skills", {})
            except HTTPError as error:
                with error:
                    if error.code != 400 or json.load(error) != {"ok": False, "error": "invalid_body"}:
                        raise RuntimeError("Self-test skills route failed") from None
            else:
                raise RuntimeError("Self-test skills route accepted an invalid request")
            checks.append("skills_route")
            autosync_request = {"agent": "codex", "op": "autosync_status", "payload": {}}
            autosync_response = write("/api/skills", autosync_request)
            autosync_status = autosync_response["result"]["status"]
            if (autosync_response.get("ok") is not True or autosync_status.get("agent") != "codex"
                    or autosync_status.get("enabled") is not False or autosync_status.get("state") != "disabled"):
                raise RuntimeError("Self-test automatic sync must be disabled by default")
            checks.append("skills_autosync_disabled")
            if getattr(sys, "frozen", False):
                cli_name = "CodexTokenSkills.exe" if sys.platform == "win32" else "CodexTokenSkills"
                cli = Path(sys.executable).resolve().with_name(cli_name)
                bundled = Path(__file__).resolve().parent / "assets" / "codex-token-skill-manager" / "SKILL.md"
                if not bundled.is_file():
                    raise RuntimeError("Frozen skill manager asset is missing")
                completed = subprocess.run([str(cli), "--port", str(server.server_port), "--agent", "codex",
                                            "--op", "autosync_status"], capture_output=True, check=True, timeout=20)
                if json.loads(completed.stdout) != autosync_response:
                    raise RuntimeError("Frozen skills CLI check failed")
                checks.append("frozen_skills_cli")
            health = json.loads(read("/api/health"))
            if health["status"] != "ok":
                raise RuntimeError("Self-test scanner is unhealthy")
            if not service.auto_scan or service.interval != 60:
                raise RuntimeError("Self-test automatic scan defaults failed")
            checks.append("sixty_second_automatic_scan")
            data = json.loads(read("/api/dashboard?days=0"))
            summary = data["summary"]
            if (summary["speed_sample_count"], summary["speed_output_tokens"], summary["speed_duration_ms"],
                    summary["request_count"], summary["output_tokens_per_second"]) != (2, 2000, 60000, 3, 33.3333):
                raise RuntimeError("Self-test speed aggregation or duplicate handling failed")
            checks.extend(["backend_http", "weighted_output_speed", "duplicate_log_deduplication"])
            pending = json.loads(read("/api/dashboard?days=0&model=self-test-pending"))["summary"]
            if pending["output_tokens_per_second"] is not None or pending["speed_sample_count"] != 0:
                raise RuntimeError("Incomplete turn was included in speed")
            checks.append("model_filter_and_incomplete_turn")
            html, script, styles = read("/"), read("/app.js"), read("/styles.css")
            if "轮次平均输出速度" not in html or "speed_sample_count" not in script or not styles:
                raise RuntimeError("Shipped dashboard assets are missing speed UI")
            checks.append("shipped_web_assets")
            layout = read("/layout.js")
            if "CodexDashboardLayout" not in layout or any(
                    f'id="view-{view}"' not in html for view in ("overview", "trends", "usage", "pricing")):
                raise RuntimeError("Shipped dashboard navigation assets are missing")
            checks.append("shipped_layout_navigation")
            exported = list(csv.DictReader(io.StringIO(dashboard_csv(data["daily_model_usage"]).lstrip("\ufeff"))))
            if sorted(row["output_tokens_per_second"] for row in exported) != ["", "33.3333"]:
                raise RuntimeError("Self-test CSV speed export failed")
            checks.append("csv_speed_and_missing_value")
            if summary["ttft_sample_count"] != 2 or summary["ttft_median_ms"] != 2250:
                raise RuntimeError("Self-test first-token latency samples failed")
            if "未提供" not in read("/analytics.js"):
                raise RuntimeError("Shipped analytics assets are missing")
            if "formatPercent" not in read("/cache-metrics.js"):
                raise RuntimeError("Shipped cache formatting asset is missing")
            requests = json.loads(read("/api/requests?days=0&page=1&page_size=2&sort=token_desc"))
            if requests["total"] != 3 or len(requests["data"]) != 2:
                raise RuntimeError("Self-test request pagination failed")
            for item in requests["data"]:
                if item.get("request_duration_estimate_ms") != 10000 or item.get("request_output_tokens_per_second_estimate") != 100:
                    raise RuntimeError("Self-test per-request estimated speed failed")
                detail = json.loads(read("/api/request?id=" + item["request_id"]))["request"]
                if detail.get("request_output_tokens_per_second_estimate") != 100:
                    raise RuntimeError("Self-test per-request speed detail failed")
            checks.append("per_request_log_estimate_includes_wait")
            if not json.loads(read("/api/tasks?days=0"))["data"]:
                raise RuntimeError("Self-test task summaries failed")
            json.loads(read("/api/diagnostics"))
            checks.extend(["first_token_latency", "analytics_assets", "request_pagination", "task_summary", "diagnostics"])
            write("/api/pricing", {"rules": [{"id": "self-test-rule", "provider": "openai",
                  "model": "self-test-model", "effective_at": "2020-01-01T00:00:00Z",
                  "input": 2, "cached_input": .2, "cache_write": 2.5, "output": 10,
                  "multiplier": .8, "source": "Synthetic self-test", "note": ""}]})
            unchanged = json.loads(read("/api/dashboard?days=0"))["summary"]
            if unchanged["cost_after_discount_usd"] != summary["cost_after_discount_usd"]:
                raise RuntimeError("Editing prices unexpectedly changed retained history")
            preview = write("/api/reprice/preview", {"days": 0})
            result = write("/api/reprice", {"preview_token": preview["preview_token"]})
            if not Path(result["backup_path"]).is_file():
                raise RuntimeError("Self-test repricing backup is missing")
            summary = json.loads(read("/api/dashboard?days=0"))["summary"]
            if summary["cost_before_discount_usd"] <= 0 or abs(
                    summary["cost_after_discount_usd"] - summary["cost_before_discount_usd"] * .8) > 1e-8:
                raise RuntimeError("Self-test discounted repricing failed")
            checks.extend(["editable_prices", "frozen_history_prices", "preview_and_backup_repricing", "discount_before_after"])
        finally:
            if server is not None:
                if thread is not None:
                    server.shutdown()
                    thread.join(timeout=5)
                server.server_close()
            db.close()
        for path in sessions.glob("*.jsonl"):
            path.unlink()
        db = DashboardDB(database_path, roots=[sessions], providers_path=root / "providers.json", timezone_name="UTC")
        try:
            db.scan()
            retained = db.dashboard(0)["summary"]
            if retained != summary:
                raise RuntimeError("Self-test retained history changed after log deletion")
            checks.append("retained_speed_history")
        finally:
            db.close()
    return {"status": "passed", "app_version": health["app_version"], "frozen": bool(getattr(sys, "frozen", False)),
            "gui_tested": False, "speed_sample_count": 2, "output_tokens_per_second": 33.3333, "checks": checks}


def _create_dashboard_window(webview, url, bridge):
    """Use the exact same native window and real JS bridge in smoke and production."""
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
    return window


def _start_desktop_gui(webview, *, func=None, storage_path=None) -> None:
    webview.start(
        func=func,
        gui="edgechromium" if sys.platform == "win32" else None,
        debug=False,
        private_mode=True,
        storage_path=storage_path,
    )


@contextmanager
def _gui_smoke_profile(root: Path):
    # Several runtime components independently resolve their data paths. Give
    # all of them a temporary user profile, including the WebView browser data.
    paths = {
        "HOME": root / "home",
        "USERPROFILE": root / "home",
        "LOCALAPPDATA": root / "local",
        "APPDATA": root / "roaming",
        "XDG_CACHE_HOME": root / "cache",
        "CODEX_HOME": root / "codex",
        "HERMES_HOME": root / "hermes",
    }
    previous = {key: os.environ.get(key) for key in paths}
    try:
        for key, path in paths.items():
            path.mkdir(exist_ok=True)
            os.environ[key] = str(path)
        sessions = paths["CODEX_HOME"] / "sessions"
        sessions.mkdir()
        (paths["CODEX_HOME"] / "archived_sessions").mkdir()
        yield sessions
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class _GuiSmokeBridge(DesktopBridge):
    """Keep the real native bridge but never contact the updater in CI."""

    def get_update_status(self) -> Mapping[str, Any]:
        return {"state": "unsupported", "message": "Disabled in GUI smoke test"}

    check_for_update = start_update = get_update_status


_GUI_SMOKE_TOKENS = 173


def _check_gui_smoke_window(window, *, timeout=40) -> int:
    deadline = time.monotonic() + timeout
    if not window.events.shown.wait(max(0, deadline - time.monotonic())):
        raise RuntimeError("Native dashboard window was not shown")
    if not window.events.loaded.wait(max(0, deadline - time.monotonic())):
        raise RuntimeError("Dashboard WebView did not load")
    # This is the value rendered by the production frontend, not API data.
    # The default date range must contain today's synthetic usage.
    probe = r"""(() => {
        const value = index => document.querySelector(
            '#kpiGrid article[data-metric="' + index + '"] .kpi-value'
        )?.textContent.trim();
        return {
            ready: typeof window.pywebview?.api?.load_preferences === 'function',
            total: value(1) ?? '',
            requests: value(3) ?? '',
            dashboardVisible: Boolean(document.querySelector('#view-overview')?.getClientRects().length)
        };
    })()"""
    state = None
    while time.monotonic() < deadline:
        state = window.evaluate_js(probe)
        if state == {"ready": True, "total": str(_GUI_SMOKE_TOKENS), "requests": "1", "dashboardVisible": True}:
            break
        time.sleep(.1)
    else:
        raise RuntimeError(f"Dashboard did not render {_GUI_SMOKE_TOKENS} Tokens / 1 request: {state!r}")
    if window.evaluate_js(r"""(() => {
        const tab = document.querySelector('#viewTabTrends');
        tab.click();
        return tab.getAttribute('aria-selected') === 'true' &&
            Boolean(document.querySelector('#view-trends').getClientRects().length);
    })()""") is not True:
        raise RuntimeError("Trends tab did not show after clicking the native WebView control")
    if window.evaluate_js(r"""(() => {
        const tab = document.querySelector('#viewTabOverview');
        tab.click();
        return tab.getAttribute('aria-selected') === 'true' &&
            Boolean(document.querySelector('#view-overview').getClientRects().length);
    })()""") is not True:
        raise RuntimeError("Overview tab did not show after clicking the native WebView control")
    return int(state["total"])


def run_gui_smoke_test() -> Mapping[str, Any]:
    """Launch a real desktop window against one synthetic Codex request."""
    import webview

    with tempfile.TemporaryDirectory(prefix="codex-token-gui-smoke-") as folder:
        root = Path(folder).resolve()
        with _gui_smoke_profile(root) as sessions:
            timestamp = datetime.now(timezone.utc).isoformat()
            rows = [
                ("session_meta", {"id": "gui-smoke-thread", "cwd": "GUI smoke workspace", "model_provider": "openai"}),
                ("turn_context", {"turn_id": "gui-smoke-turn", "model": "gpt-5.4"}),
                ("token_usage_record", {
                    "thread_id": "gui-smoke-thread", "turn_id": "gui-smoke-turn", "response_id": "gui-smoke-response",
                    "usage": {"input_tokens": 100, "output_tokens": 73, "total_tokens": _GUI_SMOKE_TOKENS},
                }),
            ]
            (sessions / "rollout-gui-smoke.jsonl").write_text(
                "".join(json.dumps({"timestamp": timestamp, "type": kind, "payload": payload}) + "\n"
                        for kind, payload in rows), encoding="utf-8",
            )
            runtime = DesktopRuntime(db_path=root / "usage.sqlite3", providers_path=root / "providers.json",
                                     hermes_db=root / "missing-hermes.sqlite3")
            try:
                url = runtime.start()
                bridge = _GuiSmokeBridge(runtime)
                window = _create_dashboard_window(webview, url, bridge)
                result: dict[str, Any] = {}

                def check_and_close() -> None:
                    try:
                        result["rendered_total_tokens"] = _check_gui_smoke_window(window)
                    except Exception as error:
                        result["error"] = str(error)
                    finally:
                        window.destroy()

                _start_desktop_gui(webview, func=check_and_close, storage_path=str(root / "webview"))
                if result.get("rendered_total_tokens") != _GUI_SMOKE_TOKENS:
                    raise RuntimeError(result.get("error") or "Native GUI exited before smoke verification")
                return {"status": "passed", "app_version": APP_VERSION, "gui_tested": True,
                        "frozen": bool(getattr(sys, "frozen", False)), "renderer": webview.renderer,
                        "rendered_total_tokens": result["rendered_total_tokens"],
                        "checks": ["native_window_shown", "rendered_synthetic_usage", "navigate_trends_and_overview"]}
            finally:
                runtime.stop()


def probe_desktop_dependencies() -> Mapping[str, Any]:
    """Load native dependencies without accessing history or opening a window."""
    import webview
    from webview.guilib import initialize
    imports = ["webview"]
    gui = "cocoa" if sys.platform == "darwin" else "edgechromium"
    renderer = "wkwebview" if sys.platform == "darwin" else "edgechromium"
    backend = initialize(gui)
    if getattr(backend, "renderer", None) != renderer:
        raise RuntimeError(f"Desktop renderer unavailable: {renderer}")
    if sys.platform == "win32":
        import clr
        import clr_loader
        import cffi
        import pycparser
        from System.Windows.Forms import Form
        from Microsoft.Web.WebView2.WinForms import WebView2
        imports.extend(["clr", "clr_loader", "cffi", "pycparser", "WinForms", "WebView2"])
    return {"status": "passed", "frozen": bool(getattr(sys, "frozen", False)),
            "renderer": renderer, "imports": imports, "gui_tested": False}


def main() -> int:
    if "--gui-smoke-test" in sys.argv:
        import argparse
        parser = argparse.ArgumentParser(description="Open the native dashboard with isolated synthetic data.")
        parser.add_argument("--gui-smoke-test", action="store_true")
        parser.add_argument("--gui-smoke-report", type=Path)
        args = parser.parse_args()
        try:
            report, code = run_gui_smoke_test(), 0
        except Exception as error:
            report, code = {"status": "failed", "gui_tested": False, "error": str(error)}, 1
        output = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.gui_smoke_report:
            args.gui_smoke_report.write_text(output, encoding="utf-8")
        elif sys.stdout is not None:
            print(output, end="")
        return code
    if "--probe-desktop-dependencies" in sys.argv:
        import argparse
        parser = argparse.ArgumentParser(description="Check native desktop dependencies without reading personal data.")
        parser.add_argument("--probe-desktop-dependencies", action="store_true")
        parser.add_argument("--probe-report", type=Path)
        args = parser.parse_args()
        try:
            report, code = probe_desktop_dependencies(), 0
        except Exception as error:
            report, code = {"status": "failed", "gui_tested": False, "error": str(error)}, 1
        output = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.probe_report:
            args.probe_report.write_text(output, encoding="utf-8")
        elif sys.stdout is not None:
            print(output, end="")
        return code
    if "--self-test" in sys.argv:
        import argparse
        parser = argparse.ArgumentParser(description="Validate shipped analytics with isolated synthetic data; no GUI.")
        parser.add_argument("--self-test", action="store_true")
        parser.add_argument("--self-test-report", type=Path)
        args = parser.parse_args()
        try:
            report = run_self_test()
            code = 0
        except Exception as error:
            report = {"status": "failed", "gui_tested": False, "error": str(error)}
            code = 1
        output = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.self_test_report:
            args.self_test_report.write_text(output, encoding="utf-8")
        elif sys.stdout is not None:
            print(output, end="")
        return code
    try:
        import webview
    except ImportError:
        command = "py" if sys.platform == "win32" else "python3"
        _show_startup_error(f"桌面组件尚未安装。请运行：{command} -m pip install -r requirements-desktop.txt")
        return 1

    runtime: Optional[DesktopRuntime] = None
    bridge: Optional[DesktopBridge] = None
    try:
        # Older helpers launch the replacement from the update staging folder.
        # Leave that folder before the cleanup worker removes it on Windows.
        if getattr(sys, 'frozen', False) and '--update-job' in sys.argv:
            os.chdir(installation_dir())
        _wait_for_webview_cleanup()
        runtime = (DesktopRuntime(hermes_db=Path(sys.argv[sys.argv.index('--hermes-db') + 1]))
                   if '--hermes-db' in sys.argv else DesktopRuntime())
        url = runtime.start()
        bridge = DesktopBridge(runtime)
        window = _create_dashboard_window(webview, url, bridge)
        job_path = Path(sys.argv[sys.argv.index('--update-job') + 1]) if '--update-job' in sys.argv else None
        if job_path is not None:
            window.events.loaded += lambda: confirm_startup(job_path)
        if getattr(sys, 'frozen', False):
            threading.Thread(target=cleanup_completed_updates, args=(installation_dir(), local_data_dir(), job_path), daemon=True, name='desktop-update-cleanup').start()
        _start_desktop_gui(webview)
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

import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import desktop


class GuiSmokeTests(unittest.TestCase):
    def test_cocoa_evaluation_returns_native_result_while_window_is_open(self):
        view = Mock(closed=threading.Event())
        view.webview.evaluateJavaScript_completionHandler_.side_effect = (
            lambda script, complete: complete('{"value": 173}', None)
        )
        schedule = lambda callback: callback()
        self.assertEqual(
            desktop._evaluate_cocoa_js(view, "173", True, schedule, threading.Event()), {"value": 173}
        )
        self.assertEqual(
            desktop._evaluate_cocoa_js(view, "173", False, schedule, threading.Event()), '{"value": 173}'
        )

    def test_cocoa_evaluation_releases_pending_bridge_call_on_close(self):
        view = Mock(closed=threading.Event())
        stopped = threading.Event()

        def schedule(callback):
            callback()
            view.closed.set()

        self.assertIsNone(desktop._evaluate_cocoa_js(view, "reply()", False, schedule, stopped))
        view.webview.evaluateJavaScript_completionHandler_.assert_called_once()

    def test_cocoa_evaluation_does_not_dispatch_after_gui_loop_stops(self):
        view = Mock(closed=threading.Event())
        stopped = threading.Event()
        stopped.set()
        schedule = Mock()
        self.assertIsNone(desktop._evaluate_cocoa_js(view, "reply()", False, schedule, stopped))
        schedule.assert_not_called()

    def test_desktop_installs_the_close_aware_evaluator_on_macos(self):
        view = Mock(closed=threading.Event())
        view.webview.evaluateJavaScript_completionHandler_.side_effect = (
            lambda script, complete: complete('true', None)
        )
        schedule = Mock(side_effect=lambda callback: callback())
        cocoa = SimpleNamespace(BrowserView=SimpleNamespace(), AppHelper=SimpleNamespace(callAfter=schedule))
        webview = Mock()
        webview.start.side_effect = lambda **_: self.assertIs(cocoa.BrowserView.evaluate_js(view, "true", True), True)
        with patch.object(desktop.sys, "platform", "darwin"), \
                patch.dict(desktop.sys.modules, {"webview.platforms": SimpleNamespace(cocoa=cocoa)}):
            desktop._start_desktop_gui(webview)
        self.assertIsNone(cocoa.BrowserView.evaluate_js(view, "reply()", False))
        schedule.assert_called_once()

    def test_routes_cli_through_native_smoke_and_writes_report(self):
        with tempfile.TemporaryDirectory() as folder:
            report_path = Path(folder) / "gui.json"
            argv = ["desktop.py", "--gui-smoke-test", "--gui-smoke-report", str(report_path)]
            result = {"status": "passed", "frozen": True, "gui_tested": True, "rendered_total_tokens": 173}
            with patch.object(desktop.sys, "argv", argv), patch.object(desktop, "run_gui_smoke_test", return_value=result):
                self.assertEqual(desktop.main(), 0)
            self.assertEqual(json.loads(report_path.read_text(encoding="utf-8")), result)

    def test_cli_reports_gui_failure_without_startup_alert(self):
        with tempfile.TemporaryDirectory() as folder:
            report_path = Path(folder) / "gui.json"
            argv = ["desktop.py", "--gui-smoke-test", "--gui-smoke-report", str(report_path)]
            with patch.object(desktop.sys, "argv", argv), \
                    patch.object(desktop, "run_gui_smoke_test", side_effect=RuntimeError("no window")), \
                    patch.object(desktop, "_show_startup_error", side_effect=AssertionError("blocking startup alert")):
                self.assertEqual(desktop.main(), 1)
            report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertFalse(report["gui_tested"])
        self.assertIn("no window", report["error"])

    def test_checks_rendered_usage_and_visible_navigation(self):
        window = Mock()
        window.events.shown.wait.return_value = True
        window.events.loaded.wait.return_value = True
        window.evaluate_js.side_effect = [
            {"ready": True, "total": "173", "requests": "1", "dashboardVisible": True},
            True, True,
        ]
        self.assertEqual(desktop._check_gui_smoke_window(window, timeout=1), 173)
        self.assertEqual(window.evaluate_js.call_count, 3)
        script = window.evaluate_js.call_args_list[0].args[0]
        self.assertIn("#kpiGrid", script)
        self.assertIn("getClientRects", script)

    def test_zero_usage_does_not_pass_when_the_window_loads(self):
        window = Mock()
        window.events.shown.wait.return_value = True
        window.events.loaded.wait.return_value = True
        window.evaluate_js.return_value = {"ready": True, "total": "0", "requests": "0", "dashboardVisible": True}
        with patch.object(desktop.time, "monotonic", side_effect=[0, 0, 0, 0, 1]):
            with self.assertRaisesRegex(RuntimeError, "render.*173"):
                desktop._check_gui_smoke_window(window, timeout=.5)

    def test_real_runtime_reads_only_synthetic_profiles_and_disables_updater(self):
        env_before = {key: os.environ.get(key) for key in ("HOME", "USERPROFILE", "LOCALAPPDATA", "CODEX_HOME", "HERMES_HOME")}
        with patch.dict("sys.modules", {"webview": Mock(renderer="qt")}), \
                patch.object(desktop, "_create_dashboard_window") as create_window, \
                patch.object(desktop, "_start_desktop_gui") as start_gui, \
                patch.object(desktop, "_check_gui_smoke_window", return_value=173):
            def run_callback(webview, **kw):
                root = Path(os.environ["CODEX_HOME"]).parent
                for key in env_before:
                    self.assertTrue(Path(os.environ[key]).is_relative_to(root))
                sessions = list((Path(os.environ["CODEX_HOME"]) / "sessions").glob("*.jsonl"))
                self.assertEqual(len(sessions), 1)
                self.assertIn("gui-smoke", sessions[0].read_text(encoding="utf-8"))
                bridge = create_window.call_args.args[2]
                self.assertEqual(bridge.check_for_update()["state"], "unsupported")
                self.assertIsNone(bridge._updater)
                kw["func"]()

            start_gui.side_effect = run_callback
            result = desktop.run_gui_smoke_test()
        self.assertTrue(result["gui_tested"])
        self.assertEqual(result["rendered_total_tokens"], 173)
        self.assertEqual({key: os.environ.get(key) for key in env_before}, env_before)
        create_window.return_value.destroy.assert_called_once()

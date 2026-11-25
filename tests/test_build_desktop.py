import hashlib
import json
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch
import zipfile

import build_desktop as builder


SELF_TEST_REPORT = {
    "status": "passed",
    "frozen": True,
    "gui_tested": False,
    "checks": [],
}

DEPENDENCY_REPORT = {
    "status": "passed",
    "frozen": True,
    "renderer": "edgechromium",
    "gui_tested": False,
}


GUI_SMOKE_REPORT = {
    "status": "passed",
    "frozen": True,
    "gui_tested": True,
    "renderer": "edgechromium",
    "app_version": "1.2.0",
    "rendered_total_tokens": 173,
}


class BuildDesktopTests(unittest.TestCase):
    def make_source(self, root):
        root.mkdir()
        for name in builder.RUNTIME_FILES:
            (root / name).write_bytes(b"1.2.0" if name == "VERSION" else b"shared source\n")
        (root / "web").mkdir()
        (root / "web" / "app.js").write_bytes(b"const ready = true;\n")
        for name in builder.PORTABLE_FILES:
            if not (root / name).exists():
                (root / name).write_bytes(b"package documentation\n")

    def test_staging_excludes_cached_data_and_private_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "original"
            self.make_source(source)
            cache = source / "codex-token-dashboard.sqlite3"
            cache.write_bytes(b"private history")
            (source / "providers.json").write_bytes(b"private aliases")
            (source / "personal-settings.json").write_bytes(b"private settings")
            for directory in ("release", "tests/__pycache__", "web/work"):
                target = source / directory
                target.mkdir(parents=True)
                (target / "personal.sqlite3-wal").write_bytes(b"private history")
            (source / "tests" / "fixture.jsonl").write_bytes(b"fixture\n")
            stage = root / "stage"
            builder.stage_source(source, stage)
            names = {path.relative_to(stage).as_posix() for path in stage.rglob("*") if path.is_file()}
            self.assertIn("tests/fixture.jsonl", names)
            self.assertIn("providers.json.example", names)
            self.assertFalse(any("sqlite" in name or "__pycache__" in name for name in names))
            self.assertNotIn("providers.json", names)
            self.assertNotIn("personal-settings.json", names)
            self.assertEqual(cache.read_bytes(), b"private history")
            self.assertEqual(builder.source_digest(source), builder.source_digest(stage))

    def test_digest_tracks_raw_content_and_relative_file_names(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source"
            self.make_source(source)
            original = builder.source_digest(source)
            script = source / "web" / "app.js"
            script.write_bytes(script.read_bytes().replace(b"\n", b"\r\n"))
            self.assertNotEqual(original, builder.source_digest(source))
            changed = builder.source_digest(source)
            script.rename(source / "web" / "renamed.js")
            self.assertNotEqual(changed, builder.source_digest(source))

    def test_windows_build_runs_checks_in_isolated_staging_and_packages_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            self.make_source(source)
            original_cache = source / "codex-token-dashboard.sqlite3"
            original_cache.write_bytes(b"do not touch")
            calls = []

            def fake_run(command, cwd, env=None, *, timeout=None):
                calls.append((command, cwd, env))
                if "PyInstaller" in command:
                    output = Path(command[command.index("--distpath") + 1]) / "CodexTokenDesktop"
                    output.mkdir(parents=True)
                    (output / "CodexTokenDesktop.exe").write_bytes(b"fake executable")
                if "--self-test" in command:
                    report_path = Path(command[command.index("--self-test-report") + 1])
                    report_path.write_text(json.dumps(SELF_TEST_REPORT), encoding="utf-8")
                if "--gui-smoke-test" in command:
                    Path(command[-1]).write_text(json.dumps(GUI_SMOKE_REPORT), encoding="utf-8")
                    self.assertEqual(timeout, 90)
                if "--probe-desktop-dependencies" in command:
                    report_path = Path(command[command.index("--probe-report") + 1])
                    report_path.write_text(json.dumps(DEPENDENCY_REPORT), encoding="utf-8")

            with patch.object(builder, "SOURCE_DIR", source), patch.object(builder.sys, "platform", "win32"), patch.object(builder, "run", side_effect=fake_run):
                archive, manifest_path = builder.build(root / "release")
            self.assertEqual(len(calls), 6)
            self.assertTrue(all(cwd != source for _, cwd, _ in calls))
            self.assertIn("unittest", calls[0][0])
            self.assertEqual(calls[1][0][:2], ["node", "--check"])
            self.assertIn("--self-test", calls[3][0])
            self.assertIn("--self-test-report", calls[3][0])
            self.assertIn("--probe-desktop-dependencies", calls[4][0])
            self.assertIn("--gui-smoke-test", calls[5][0])
            self.assertNotEqual(calls[0][2]["CODEX_HOME"], str(source))
            self.assertEqual(original_cache.read_bytes(), b"do not touch")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["source_digest"], builder.source_digest(source))
            self.assertEqual(manifest["self_test"], SELF_TEST_REPORT)
            self.assertEqual(manifest["desktop_dependencies"], DEPENDENCY_REPORT)
            self.assertEqual(manifest["gui_smoke_test"], GUI_SMOKE_REPORT)
            exe = next(item for item in manifest["files"] if item["path"] == "CodexTokenDesktop.exe")
            self.assertEqual(exe["sha256"], hashlib.sha256(b"fake executable").hexdigest())
            self.assertEqual(exe["size_bytes"], len(b"fake executable"))
            self.assertEqual(manifest["payload_size_bytes"], sum(item.get("size_bytes", 0) for item in manifest["files"]))
            with zipfile.ZipFile(archive) as package:
                self.assertIn("CodexTokenDesktop/build-manifest.json", package.namelist())
                self.assertFalse(any("sqlite" in name for name in package.namelist()))

    def test_unsupported_host_does_not_create_output(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "release"
            with patch.object(builder.sys, "platform", "linux"), self.assertRaises(RuntimeError):
                builder.build(output)
            self.assertFalse(output.exists())

    def test_macos_rejects_non_native_apple_silicon_before_creating_output(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "release"
            with patch.object(builder.sys, "platform", "darwin"), patch.object(builder.platform, "machine", return_value="x86_64"), self.assertRaises(RuntimeError):
                builder.build(output)
            self.assertFalse(output.exists())

    def test_macos_uses_native_icon_tools_and_preserves_bundle_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            self.make_source(source)
            (source / "assets").mkdir()
            (source / "assets" / "app-icon.png").write_bytes(b"fake PNG")
            calls = []

            def fake_run(command, cwd, env=None, *, timeout=None):
                calls.append((command, cwd, env))
                if command[0] == "iconutil":
                    Path(command[-1]).write_bytes(b"fake ICNS")
                elif "PyInstaller" in command:
                    package = Path(command[command.index("--distpath") + 1]) / "CodexTokenDesktop.app"
                    (package / "Contents").mkdir(parents=True)
                    (package / "Contents" / "Info.plist").write_bytes(b"bundle metadata")
                elif "--self-test" in command:
                    report_path = Path(command[command.index("--self-test-report") + 1])
                    report_path.write_text(json.dumps(SELF_TEST_REPORT), encoding="utf-8")
                elif "--gui-smoke-test" in command:
                    report = Path(command[-1])
                    report.write_text(json.dumps({**GUI_SMOKE_REPORT, "renderer": "wkwebview"}), encoding="utf-8")
                    report.with_suffix(".trace").write_text("Native event loop returned\n", encoding="utf-8")
                elif command[0] == "ditto":
                    Path(command[-1]).write_bytes(b"fake archive")

            with patch.object(builder, "SOURCE_DIR", source), patch.object(builder.sys, "platform", "darwin"), patch.object(builder.platform, "machine", return_value="arm64"), patch.object(builder, "run", side_effect=fake_run), patch.dict(builder.os.environ, {"CODEX_TOKEN_GUI_SMOKE_REPEAT": "1"}):
                archive, manifest_path = builder.build(root / "release", skip_tests=True)
            self.assertEqual(sum(command[0] == "sips" for command, _, _ in calls), 10)
            self.assertEqual(sum(command[0] == "iconutil" for command, _, _ in calls), 1)
            command = calls[-1][0]
            self.assertEqual(command[:5], ["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent"])
            self_test_calls = [command for command, _, _ in calls if "--self-test" in command]
            self.assertEqual(len(self_test_calls), 1)
            self.assertIn("--self-test-report", self_test_calls[0])
            smoke_reports = [Path(command[-1]).name for command, _, _ in calls if "--gui-smoke-test" in command]
            self.assertEqual(smoke_reports, [
                "gui-smoke-report.json", "gui-smoke-report-2.json", "gui-smoke-report-3.json",
                "gui-smoke-report-4.json", "gui-smoke-report-5.json",
            ])
            package = Path(command[-2])
            self.assertFalse((package / "build-manifest.json").exists())
            self.assertTrue(archive.is_file())
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["self_test"], SELF_TEST_REPORT)
            self.assertIsNone(manifest["desktop_dependencies"])
            self.assertEqual(manifest["gui_smoke_test"], {**GUI_SMOKE_REPORT, "renderer": "wkwebview"})
            self.assertEqual(manifest["architecture"], "arm64")
            self.assertEqual(manifest["validation"], "skipped")
            self.assertEqual(manifest["source_digest"], builder.source_digest(source))

    def test_failed_packaged_self_test_rejects_build(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            self.make_source(source)

            def fake_run(command, cwd, env=None, *, timeout=None):
                if "PyInstaller" in command:
                    output = Path(command[command.index("--distpath") + 1]) / "CodexTokenDesktop"
                    output.mkdir(parents=True)
                    (output / "CodexTokenDesktop.exe").write_bytes(b"fake executable")
                if "--self-test" in command:
                    report_path = Path(command[command.index("--self-test-report") + 1])
                    report_path.write_text(json.dumps({**SELF_TEST_REPORT, "status": "failed"}), encoding="utf-8")

            with patch.object(builder, "SOURCE_DIR", source), patch.object(builder.sys, "platform", "win32"), patch.object(builder, "run", side_effect=fake_run):
                with self.assertRaisesRegex(RuntimeError, "self-test did not pass"):
                    builder.build(root / "release", skip_tests=True)

    def test_windows_build_rejects_unverified_desktop_dependencies(self):
        reports = [
            {**DEPENDENCY_REPORT, "status": "failed"},
            {**DEPENDENCY_REPORT, "frozen": False},
            {**DEPENDENCY_REPORT, "renderer": "mshtml"},
        ]
        for report in reports:
            with self.subTest(report=report), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                source = root / "source"
                self.make_source(source)

                def fake_run(command, cwd, env=None, *, timeout=None):
                    if "PyInstaller" in command:
                        output = Path(command[command.index("--distpath") + 1]) / "CodexTokenDesktop"
                        output.mkdir(parents=True)
                        (output / "CodexTokenDesktop.exe").write_bytes(b"fake executable")
                    elif "--self-test" in command:
                        Path(command[-1]).write_text(json.dumps(SELF_TEST_REPORT), encoding="utf-8")
                    elif "--probe-desktop-dependencies" in command:
                        Path(command[-1]).write_text(json.dumps(report), encoding="utf-8")

                with patch.object(builder, "SOURCE_DIR", source), patch.object(builder.sys, "platform", "win32"), patch.object(builder, "run", side_effect=fake_run):
                    with self.assertRaisesRegex(RuntimeError, "dependency probe did not pass"):
                        builder.build(root / "release", skip_tests=True)
                self.assertFalse(list((root / "release").glob("*.zip")))


    def test_windows_build_rejects_gui_failure_timeout_or_incorrect_rendered_usage(self):
        reports = [
            {**GUI_SMOKE_REPORT, "gui_tested": False},
            {**GUI_SMOKE_REPORT, "renderer": "mshtml"},
            {**GUI_SMOKE_REPORT, "rendered_total_tokens": 0},
            {**GUI_SMOKE_REPORT, "app_version": "1.1.0"},
            "timeout",
            "startup error",
        ]
        for report in reports:
            with self.subTest(report=report), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                source = root / "source"
                self.make_source(source)

                def fake_run(command, cwd, env=None, *, timeout=None):
                    if "PyInstaller" in command:
                        output = Path(command[command.index("--distpath") + 1]) / "CodexTokenDesktop"
                        output.mkdir(parents=True)
                        (output / "CodexTokenDesktop.exe").write_bytes(b"fake executable")
                    elif "--self-test" in command:
                        Path(command[-1]).write_text(json.dumps(SELF_TEST_REPORT), encoding="utf-8")
                    elif "--probe-desktop-dependencies" in command:
                        Path(command[-1]).write_text(json.dumps(DEPENDENCY_REPORT), encoding="utf-8")
                    elif "--gui-smoke-test" in command:
                        if report == "timeout":
                            raise subprocess.TimeoutExpired(command, timeout)
                        if report == "startup error":
                            Path(command[-1]).write_text(json.dumps({"error": "window failed"}), encoding="utf-8")
                            raise subprocess.CalledProcessError(1, command)
                        Path(command[-1]).write_text(json.dumps(report), encoding="utf-8")

                with patch.object(builder, "SOURCE_DIR", source), patch.object(builder.sys, "platform", "win32"), patch.object(builder, "run", side_effect=fake_run):
                    with self.assertRaisesRegex(RuntimeError, "GUI smoke"):
                        builder.build(root / "release", skip_tests=True)
                self.assertFalse(list((root / "release").glob("*.zip")))


class GuiSmokeTimeoutDiagnosticsTests(unittest.TestCase):
    def test_timeout_includes_child_trace_for_native_deadlock(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            report = root / "gui-smoke-report.json"
            report.with_suffix(".trace").write_text(
                "Current thread: blocked in Cocoa callback", encoding="utf-8"
            )
            report.write_text('{"status": "rendered-but-not-exited"}', encoding="utf-8")
            with patch.object(builder, "run", side_effect=subprocess.TimeoutExpired("smoke", 90)):
                with self.assertRaisesRegex(RuntimeError, r"(?s)blocked in Cocoa callback.*rendered-but-not-exited"):
                    builder.run_packaged_gui_smoke(root / "app", root, {}, report)

if __name__ == "__main__":
    unittest.main()

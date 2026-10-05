import hashlib
import json
from pathlib import Path
import tempfile
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

            def fake_run(command, cwd, env=None):
                calls.append((command, cwd, env))
                if "PyInstaller" in command:
                    output = Path(command[command.index("--distpath") + 1]) / "CodexTokenDesktop"
                    output.mkdir(parents=True)
                    (output / "CodexTokenDesktop.exe").write_bytes(b"fake executable")
                if "--self-test" in command:
                    report_path = Path(command[command.index("--self-test-report") + 1])
                    report_path.write_text(json.dumps(SELF_TEST_REPORT), encoding="utf-8")

            with patch.object(builder, "SOURCE_DIR", source), patch.object(builder.sys, "platform", "win32"), patch.object(builder, "run", side_effect=fake_run):
                archive, manifest_path = builder.build(root / "release")
            self.assertEqual(len(calls), 4)
            self.assertTrue(all(cwd != source for _, cwd, _ in calls))
            self.assertIn("unittest", calls[0][0])
            self.assertEqual(calls[1][0][:2], ["node", "--check"])
            self.assertIn("--self-test", calls[3][0])
            self.assertIn("--self-test-report", calls[3][0])
            self.assertNotEqual(calls[0][2]["CODEX_HOME"], str(source))
            self.assertEqual(original_cache.read_bytes(), b"do not touch")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["source_digest"], builder.source_digest(source))
            self.assertEqual(manifest["self_test"], SELF_TEST_REPORT)
            exe = next(item for item in manifest["files"] if item["path"] == "CodexTokenDesktop.exe")
            self.assertEqual(exe["sha256"], hashlib.sha256(b"fake executable").hexdigest())
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

            def fake_run(command, cwd, env=None):
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
                elif command[0] == "ditto":
                    Path(command[-1]).write_bytes(b"fake archive")

            with patch.object(builder, "SOURCE_DIR", source), patch.object(builder.sys, "platform", "darwin"), patch.object(builder.platform, "machine", return_value="arm64"), patch.object(builder, "run", side_effect=fake_run):
                archive, manifest_path = builder.build(root / "release", skip_tests=True)
            self.assertEqual(sum(command[0] == "sips" for command, _, _ in calls), 10)
            self.assertEqual(sum(command[0] == "iconutil" for command, _, _ in calls), 1)
            command = calls[-1][0]
            self.assertEqual(command[:5], ["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent"])
            self_test_calls = [command for command, _, _ in calls if "--self-test" in command]
            self.assertEqual(len(self_test_calls), 1)
            self.assertIn("--self-test-report", self_test_calls[0])
            package = Path(command[-2])
            self.assertFalse((package / "build-manifest.json").exists())
            self.assertTrue(archive.is_file())
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["self_test"], SELF_TEST_REPORT)
            self.assertEqual(manifest["architecture"], "arm64")
            self.assertEqual(manifest["validation"], "skipped")
            self.assertEqual(manifest["source_digest"], builder.source_digest(source))

    def test_failed_packaged_self_test_rejects_build(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            self.make_source(source)

            def fake_run(command, cwd, env=None):
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


if __name__ == "__main__":
    unittest.main()

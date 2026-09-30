import os
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WindowsLauncherContractTests(unittest.TestCase):
    def test_cmd_preserves_startup_errors(self) -> None:
        launcher = (ROOT / "start-dashboard.cmd").read_text(encoding="utf-8")
        self.assertIn("start-dashboard.ps1", launcher)
        self.assertIn("pause", launcher.lower())
        self.assertIn("exit /b %dashboard_exit%", launcher)

    def test_powershell_launcher_has_fallbacks(self) -> None:
        launcher = (ROOT / "start-dashboard.ps1").read_text(encoding="utf-8")
        self.assertIn("CODEX_DASHBOARD_PYTHON", launcher)
        self.assertIn("Python\\Launcher\\py.exe", launcher)
        self.assertIn("codex-runtimes", launcher)
        self.assertIn("WindowsApps", launcher)

    @unittest.skipUnless(os.name == "nt", "Windows-only launcher check")
    def test_check_only_runs_backend_doctor(self) -> None:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ROOT / "start-dashboard.ps1"),
                "-CheckOnly",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Using Python", result.stdout)
        self.assertIn('"status": "ok"', result.stdout)


if __name__ == "__main__":
    unittest.main()

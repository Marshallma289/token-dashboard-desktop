"""Persisted 60-second defaults and refresh wakeups remain independent."""

import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from backend import DashboardDB, DashboardService


class ScanScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.db_path = self.home / "history.sqlite3"
        self.sessions = self.home / "sessions"
        self.sessions.mkdir()
        self.db = DashboardDB(self.db_path, roots=[self.sessions], timezone_name="UTC")

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def reopen(self):
        self.db.close()
        self.db = DashboardDB(self.db_path, roots=[self.sessions], timezone_name="UTC")

    def test_default_is_enabled_at_60_seconds_and_subsequent_user_settings_survive_restart(self):
        self.assertEqual(self.db.scan_options(), {"auto_scan": True, "interval_seconds": 60})
        service = DashboardService(self.db)
        self.assertTrue(service.auto_scan)
        self.assertEqual(service.interval, 60)
        self.db.set_scan_options(False, 125)
        self.reopen()
        self.assertEqual(self.db.scan_options(), {"auto_scan": False, "interval_seconds": 125})

    def test_previous_configuration_is_migrated_only_once(self):
        self.db.close()
        with closing(sqlite3.connect(self.db_path)) as previous, previous:
            previous.execute("DELETE FROM history_meta WHERE key='scan_defaults_60s_v1'")
            previous.execute("UPDATE history_meta SET value='0' WHERE key='auto_scan'")
            previous.execute("UPDATE history_meta SET value='2' WHERE key='scan_interval_seconds'")
        self.db = DashboardDB(self.db_path, roots=[self.sessions], timezone_name="UTC")
        self.assertEqual(self.db.scan_options(), {"auto_scan": True, "interval_seconds": 60})
        self.db.set_scan_options(False, 10)
        self.reopen()
        self.assertEqual(self.db.scan_options(), {"auto_scan": False, "interval_seconds": 10})

    def test_config_wakeup_restarts_timer_and_does_not_disable_later_scans(self):
        service = DashboardService(self.db)
        service.set_scan_options(True, 60)
        with patch.object(service._wake_scan, "wait", side_effect=[True, False]) as wait, \
                patch.object(service, "refresh", side_effect=lambda: service._stop.set()) as refresh:
            service._run()
        self.assertEqual([call.args for call in wait.call_args_list], [(60,), (60,)])
        self.assertEqual(refresh.call_count, 1)

    def test_polling_recovers_after_a_refresh_failure(self):
        service = DashboardService(self.db)
        real_scan = self.db.scan
        calls = []

        def scan():
            calls.append(True)
            if len(calls) == 1:
                raise OSError("synthetic scanner failure")
            result = real_scan()
            service._stop.set()
            return result

        with patch.object(service._wake_scan, "wait", return_value=False), \
                patch.object(self.db, "scan", side_effect=scan):
            service._run()
        self.assertEqual(len(calls), 2)
        self.assertEqual(service.consecutive_failures, 0)
        self.assertIsNone(service.last_error)
        self.assertIsNotNone(service.last_success_at)

    def test_async_initial_refresh_is_immediate_even_when_automatic_scan_is_disabled(self):
        self.db.set_scan_options(False, 60)
        service = DashboardService(self.db)
        completed = threading.Event()
        original = service.refresh

        def refresh():
            result = original()
            completed.set()
            return result

        with patch.object(service, "refresh", side_effect=refresh) as refreshed:
            service.start(initial_refresh=False)
            try:
                self.assertEqual(refreshed.call_count, 0)
                service.request_refresh()
                self.assertTrue(completed.wait(2), "queued startup refresh waited for the 60-second timer")
                self.assertEqual(refreshed.call_count, 1)
            finally:
                service.stop()

    def test_manual_refresh_runs_without_waiting_for_the_automatic_timer(self):
        self.db.set_scan_options(False, 86400)
        service = DashboardService(self.db)
        result = service.refresh()
        self.assertEqual(result.files_seen, 0)
        self.assertIsNotNone(service.last_success_at)


if __name__ == "__main__":
    unittest.main()

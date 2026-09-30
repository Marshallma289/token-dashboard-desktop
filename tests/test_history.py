import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import DashboardDB, DashboardService
from tests.test_review_regressions import session, turn, usage, write_lines


class DurableHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.root = self.home / 'codex' / 'sessions'
        self.root.mkdir(parents=True)
        self.path = self.root / 'one.jsonl'
        self.db_path = self.home / 'independent-data' / 'usage.sqlite3'
        self.db = DashboardDB(self.db_path, roots=[self.root], timezone_name='UTC')

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def save(self, total=100, response='r1'):
        write_lines(self.path, [session(), turn('gpt-6.1-sol'), usage('thread-a', response, total)])
        self.db.scan()

    def totals(self):
        return self.db.dashboard(0)['summary']

    def reopen(self, timezone='UTC'):
        self.db.close()
        self.db = DashboardDB(self.db_path, roots=[self.root], timezone_name=timezone)

    def count(self, table):
        return self.db._connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]

    def test_delete_files_and_entire_codex_directory_then_restart(self):
        self.save()
        expected = self.db.dashboard(0)
        self.path.unlink()
        self.assertEqual(self.db.scan().files_removed, 1)
        self.assertEqual(self.count('history_sources'), 0)
        self.assertEqual(self.count('source_files'), 0)
        shutil.rmtree(self.root.parent)
        self.reopen()
        self.db.scan()
        actual = self.db.dashboard(0)
        for key in ('summary', 'daily_model_usage', 'workspace_distribution', 'daily_active', 'request_token_distribution'):
            self.assertEqual(actual[key], expected[key])

    def test_deleted_root_removes_scanning_indexes_but_keeps_history(self):
        self.save()
        shutil.rmtree(self.root.parent)
        self.db.scan()
        self.assertEqual(self.count('source_files'), 0)
        self.assertEqual(self.count('history_sources'), 0)
        self.assertEqual(self.totals()['total_tokens'], 100)

    def test_restore_duplicate_archive_and_partial_copy_never_downgrade_history(self):
        self.save(200)
        raw = self.path.read_bytes()
        self.path.unlink()
        self.db.scan()
        self.reopen()
        self.save(100)
        self.assertEqual(self.totals()['total_tokens'], 200)
        self.path.write_bytes(raw)
        duplicate = self.root / 'copy.jsonl'
        duplicate.write_bytes(raw)
        self.db.scan()
        for _ in range(20):
            self.db.scan()
        self.assertEqual(self.totals()['request_count'], 1)
        self.assertEqual(self.totals()['total_tokens'], 200)
        self.assertEqual(self.count('history_snapshots'), 1)

    def test_one_coherent_snapshot_and_downward_source_correction(self):
        self.save(200)
        self.save(120)
        self.assertEqual(self.totals()['total_tokens'], 120)
        rich = usage('thread-a', 'r1', 120)
        rich['payload']['usage'] = {'input_tokens': 70, 'output_tokens': 50, 'cached_input_tokens': 30, 'total_tokens': 120}
        write_lines(self.path, [session(), turn('gpt-6.1-sol'), rich, usage('thread-a', 'r1', 100)])
        self.db.scan()
        self.assertEqual((self.totals()['input_tokens'], self.totals()['output_tokens'], self.totals()['cached_input_tokens']), (70, 50, 30))
        self.assertEqual(self.count('history_snapshots'), 1)

    def test_deleted_rich_duplicate_keeps_history_when_partial_source_remains(self):
        self.save(200)
        other = self.root / 'partial.jsonl'
        write_lines(other, [session(), turn('gpt-6.1-sol'), usage('thread-a', 'r1', 100)])
        self.db.scan()
        self.path.unlink()
        self.db.scan()
        write_lines(other, [session(), turn('gpt-6.1-sol'), usage('thread-a', 'r1', 110)])
        self.db.scan()
        self.assertEqual(self.totals()['total_tokens'], 200)
        self.assertEqual(self.count('history_snapshots'), 2)
        other.unlink()
        self.db.scan()
        self.assertEqual(self.count('history_snapshots'), 1)

    def test_truncation_preserves_genuine_past_requests(self):
        self.save()
        self.path.write_text('')
        self.db.scan()
        self.assertEqual(self.totals()['total_tokens'], 100)
        self.save(200, response='r2')
        self.assertEqual(self.totals()['total_tokens'], 300)

    def test_malformed_rescan_does_not_replace_good_history(self):
        self.save(200)
        write_lines(self.path, [session(), turn('gpt-6.1-sol'), usage('thread-a', 'r1', 100), '{broken'])
        result = self.db.scan()
        self.assertEqual(result.parse_errors, 1)
        self.assertEqual(self.totals()['total_tokens'], 200)
        self.assertEqual(self.db.scan().files_scanned, 1)

    def test_legacy_superseded_by_modern_has_no_ghost_history(self):
        event = {'type': 'event_msg', 'timestamp': '2026-09-18T16:30:00Z', 'payload': {'type': 'token_count', 'info': {'last_token_usage': {'input_tokens': 90, 'output_tokens': 10, 'total_tokens': 100}}}}
        write_lines(self.path, [session(), turn('gpt-6.1-sol'), event])
        self.db.scan()
        self.save(100)
        self.assertEqual(self.totals()['request_count'], 1)
        self.assertEqual(self.totals()['total_tokens'], 100)
        self.assertEqual(self.count('history_snapshots'), 1)

    def test_timezone_changes_after_logs_deleted(self):
        self.save()
        self.path.unlink()
        self.db.scan()
        self.reopen('Asia/Shanghai')
        self.assertEqual(self.db.dashboard(0)['daily_active'][0]['date'], '2026-09-19')
        self.assertEqual(self.db.dashboard(0)['daily_active'][0]['hour'], 0)
        self.reopen('UTC')
        self.assertEqual(self.db.dashboard(0)['daily_active'][0]['date'], '2026-09-18')

    def test_request_without_id_move_is_idempotent(self):
        row = usage('thread-a', 'r1', 100)
        del row['payload']['response_id']
        write_lines(self.path, [session(), turn('gpt-6.1-sol'), row])
        self.db.scan()
        self.path.replace(self.root / 'moved.jsonl')
        self.db.scan()
        self.assertEqual(self.totals()['total_tokens'], 100)
        self.assertEqual(self.totals()['request_count'], 1)

    def test_transaction_failure_retries_without_duplicates(self):
        self.save()
        self.save(200, response='r2')
        write_lines(self.path, [session(), turn('gpt-6.1-sol'), usage('thread-a', 'r3', 300)])
        with patch.object(self.db, '_rebuild_logical_records', side_effect=RuntimeError('interrupted')):
            with self.assertRaises(RuntimeError):
                self.db.scan()
        self.reopen()
        self.db.scan()
        self.assertEqual(self.totals()['total_tokens'], 600)
        self.assertEqual(self.totals()['request_count'], 3)
        self.assertEqual(self.count('history_snapshots'), 3)

    def test_space_is_bounded_by_requests_not_snapshots_or_poll_count(self):
        rows = [session(), turn('gpt-6.1-sol')]
        for i in range(1000):
            # Two identical source entries still require only one stored value.
            event = usage('thread-a', 'r' + str(i), 100 + i)
            rows.extend([event, event])
        write_lines(self.path, rows)
        self.db.scan()
        (self.root / 'duplicate.jsonl').write_bytes(self.path.read_bytes())
        self.db.scan()
        self.assertEqual(self.count('history_requests'), 1000)
        self.assertEqual(self.count('history_snapshots'), 1000)
        self.assertEqual(self.count('history_contexts'), 1)
        self.db._maintain_storage(force=True)
        baseline = self.db.storage_status()['database_bytes']
        for _ in range(50):
            self.db.scan()
        self.db._maintain_storage(force=True)
        self.assertEqual(self.db.storage_status()['database_bytes'], baseline)
        # More than 30 days worth of repeated corrections leave one snapshot/request.
        for i in range(40):
            self.save(200 + i, response='correction')
        self.assertEqual(self.count('history_requests'), 1001)
        self.assertEqual(self.count('history_snapshots'), 1001)
        shutil.rmtree(self.root)
        self.root.mkdir()
        self.db.scan()
        self.reopen()
        self.assertEqual(self.count('history_sources'), 0)
        self.assertEqual(self.count('source_files'), 0)
        self.assertEqual(self.count('history_snapshots'), 1001)
        self.assertLess(self.db.storage_status()['database_bytes'], 1024 * 1024)
        self.assertFalse(self.db._connection.execute('SELECT 1 FROM history_snapshots WHERE id NOT IN (SELECT snapshot_id FROM history_requests)').fetchone())

    def test_health_reports_size_and_independent_history_location(self):
        self.save()
        health = DashboardService(self.db).health()
        self.assertEqual(health['history']['mode'], 'durable')
        self.assertEqual(health['history']['database_path'], str(self.db_path.resolve()))
        self.assertGreater(health['history']['database_bytes'], 0)


class HistoryMigrationTests(unittest.TestCase):
    def old_database(self, path):
        connection = sqlite3.connect(str(path))
        columns = """source_path TEXT NOT NULL, source_line INTEGER NOT NULL,
            response_id TEXT, thread_id TEXT, timestamp TEXT NOT NULL,
            day TEXT NOT NULL, hour INTEGER NOT NULL, provider TEXT NOT NULL,
            provider_label TEXT NOT NULL, model TEXT NOT NULL, workspace TEXT NOT NULL,
            input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
            cached_input_tokens INTEGER NOT NULL, cache_write_input_tokens INTEGER NOT NULL,
            reasoning_output_tokens INTEGER NOT NULL, total_tokens INTEGER NOT NULL"""
        connection.execute("CREATE TABLE usage_records (record_key TEXT PRIMARY KEY, " + columns + ")")
        connection.execute("CREATE TABLE usage_sources (source_key TEXT PRIMARY KEY, logical_key TEXT NOT NULL, " + columns + ")")
        connection.execute("""CREATE TABLE source_files (path TEXT PRIMARY KEY, mtime_ns INTEGER NOT NULL,
            size INTEGER NOT NULL, scanned_at TEXT NOT NULL, record_count INTEGER NOT NULL,
            timezone TEXT NOT NULL, parser_version INTEGER NOT NULL)""")
        for source, total in [('one.jsonl', 200), ('two.jsonl', 100)]:
            row = (str(path.parent / 'sessions' / source), 3, 'r1', 'thread-a',
                   '2026-09-18T16:30:00+00:00', '2026-09-18', 16, 'openai', 'OpenAI',
                   'gpt-6.1-sol', '/workspace/a', total - 10, 10, 0, 0, 0, total)
            connection.execute('INSERT INTO usage_sources VALUES (' + ','.join('?' * 19) + ')', (source, 'old-key', *row))
            connection.execute('INSERT INTO source_files VALUES (?,?,?,?,?,?,?)', (row[0], 0, 0, row[4], 1, 'UTC', 3))
            if total == 200:
                connection.execute('INSERT INTO usage_records VALUES (' + ','.join('?' * 18) + ')', ('old-key', *row))
        connection.commit()
        connection.close()

    def test_migration_retains_offline_data_is_idempotent_and_bounds_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'usage.sqlite3'
            self.old_database(path)
            for _ in range(3):
                db = DashboardDB(path, roots=[path.parent / 'sessions'], timezone_name='UTC')
                try:
                    self.assertEqual(db.dashboard(0)['summary']['total_tokens'], 200)
                    db.scan()
                    self.assertEqual(db.dashboard(0)['summary']['request_count'], 1)
                    self.assertEqual(db._connection.execute('SELECT COUNT(*) FROM history_snapshots').fetchone()[0], 1)
                    self.assertEqual(db._connection.execute('SELECT COUNT(*) FROM source_files').fetchone()[0], 0)
                    self.assertEqual(db._connection.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                finally:
                    db.close()
            backups = list(path.parent.glob('*.bak'))
            self.assertEqual(len(backups), 1)
            original = sqlite3.connect(str(backups[0]))
            try:
                self.assertEqual(original.execute('SELECT total_tokens FROM usage_records').fetchone()[0], 200)
            finally:
                original.close()

    def test_failed_migration_rolls_back_and_original_remains_readable(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'usage.sqlite3'
            self.old_database(path)
            with patch.object(DashboardDB, '_store_snapshot', side_effect=RuntimeError('interrupted')):
                with self.assertRaises(RuntimeError):
                    DashboardDB(path, roots=[path.parent / 'sessions'])
            original = sqlite3.connect(str(path))
            try:
                self.assertEqual(original.execute('SELECT total_tokens FROM usage_records').fetchone()[0], 200)
                self.assertEqual(original.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            finally:
                original.close()
            db = DashboardDB(path, roots=[path.parent / 'sessions'])
            try:
                self.assertEqual(db.dashboard(0)['summary']['total_tokens'], 200)
            finally:
                db.close()


if __name__ == '__main__':
    unittest.main()

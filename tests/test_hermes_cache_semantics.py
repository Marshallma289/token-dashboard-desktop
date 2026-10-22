"""Hermes 的 input 语义与 Codex 不同导致 cache_hit_rate 失效的回归测试。

Codex:  input_tokens 已包含 cache_read（README 口径）
Hermes: input_tokens 与 cache_read 是并列独立量

修复前：分母只累加 input_tokens，Hermes 场景下 cached > 分母，
      保护条件把结果打成 None，界面上永远显示「—」。
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from backend import DashboardDB


HERMES_SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, source TEXT, title TEXT, started_at REAL,
    model TEXT, billing_provider TEXT, cwd TEXT, profile_name TEXT);
CREATE TABLE session_model_usage (
    session_id TEXT, model TEXT, billing_provider TEXT, billing_base_url TEXT,
    billing_mode TEXT, task TEXT, api_call_count INTEGER,
    input_tokens INTEGER, output_tokens INTEGER,
    cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER,
    estimated_cost_usd REAL, actual_cost_usd REAL, cost_status TEXT, cost_source TEXT,
    first_seen REAL, last_seen REAL);
"""


def build_hermes_db(path: Path, rows):
    con = sqlite3.connect(path)
    con.executescript(HERMES_SCHEMA)
    for row in rows:
        con.execute(
            "INSERT INTO sessions (id, source, title, started_at, model, billing_provider, cwd, profile_name)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (row['session_id'], 'local', 'T', row['first_seen'], row['model'],
             row['billing_provider'], '/work', 'default'))
        con.execute(
            "INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row['session_id'], row['model'], row['billing_provider'], 'https://x/v1', '',
             '', row['api_call_count'], row['input_tokens'], row['output_tokens'],
             row['cache_read_tokens'], row['cache_write_tokens'], row['reasoning_tokens'],
             0.0, 0.0, 'unknown', 'none', row['first_seen'], row['last_seen']))
    con.commit()
    con.close()


class HermesCacheSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.mkdtemp(prefix='hermes-cache-semantics-')
        self.root = Path(self.temp)
        self.hermes_db = self.root / 'state.db'
        build_hermes_db(self.hermes_db, [{
            'session_id': 's-1', 'model': 'deepseek-v4.1-flash', 'billing_provider': 'custom',
            'api_call_count': 1, 'input_tokens': 1000, 'output_tokens': 100,
            'cache_read_tokens': 20000, 'cache_write_tokens': 0, 'reasoning_tokens': 0,
            'first_seen': 1792640591.0, 'last_seen': 1792640600.0,
        }])
        self.db = DashboardDB(
            self.root / 'cache.sqlite3', roots=[self.root / 'no-codex'],
            providers_path=None, hermes_db=self.hermes_db)
        self.db.scan()

    def tearDown(self):
        self.db.close()

    def test_cache_hit_rate_uses_combined_denominator_for_hermes(self):
        """Hermes 的 input 不含 cache_read，分母必须是 input + cache_read。"""
        summary = self.db.dashboard(days=0, source_kind='hermes')['summary']
        self.assertEqual(summary['cached_input_tokens'], 20000)
        self.assertEqual(summary['input_tokens'], 1000)
        # 20000 / (1000 + 20000) ≈ 0.9524
        self.assertIsNotNone(
            summary['cache_hit_rate'],
            'Hermes 缓存命中率不应为 None —— 分母口径必须计入 cache_read')
        self.assertAlmostEqual(summary['cache_hit_rate'], 20000 / 21000, places=4)

    def test_cache_hit_rate_never_exceeds_one(self):
        """保护条件仍然有效：缓存不得大于分母。"""
        summary = self.db.dashboard(days=0, source_kind='hermes')['summary']
        self.assertLessEqual(summary['cache_hit_rate'], 1.0)

    def test_fresh_input_excludes_cache_for_hermes(self):
        """未缓存输入 = input_tokens 本身（Hermes 的 input 就是未缓存的）。"""
        summary = self.db.dashboard(days=0, source_kind='hermes')['summary']
        self.assertEqual(summary['fresh_input_tokens'], 1000)


if __name__ == '__main__':
    unittest.main()

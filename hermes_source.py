"""Read Hermes cumulative usage without treating aggregate rows as requests."""
from __future__ import annotations

import datetime as dt
from contextlib import closing, contextmanager
import os
import json
from pathlib import Path
import sqlite3
import shutil
import tempfile


EXCLUSION = 'hermes_aggregate_no_request_boundary'


def resolve_hermes_paths(path=None):
    if path is None:
        home = os.environ.get('HERMES_HOME')
        if home:
            path = Path(home) / 'state.db'
        elif os.name == 'nt':
            # Windows 上 Hermes 默认装在 <盘符>:\Hermes；这里只作为最后兜底，
            # 用 $HERMES_HOME 或 --hermes-db 显式指定更可靠。
            path = Path.home() / 'Hermes' / 'state.db'
        else:
            path = Path.home() / '.hermes' / 'state.db'
    path = Path(path).expanduser()
    if not path.is_absolute():
        path = path.resolve()
    return [path, *sorted((path.parent / 'profiles').glob('*/state.db'))]


class HermesSource:
    def __init__(self, path, timezone=None):
        self.path = Path(path)
        self.timezone = timezone or dt.datetime.now().astimezone().tzinfo

    @contextmanager
    def _readonly_snapshot(self):
        # Even mode=ro may create/update WAL shared-memory beside the source.
        # Copy DB + committed WAL with ordinary read handles, then let SQLite
        # create its private WAL index only inside this disposable directory.
        paths = [self.path, Path(str(self.path) + '-wal')]
        def signature():
            return [(p.stat().st_size, p.stat().st_mtime_ns) if p.exists() else None for p in paths]
        with tempfile.TemporaryDirectory(prefix='hermes-read-') as folder:
            target = Path(folder) / 'state.db'
            for _ in range(3):
                before = signature()
                shutil.copyfile(self.path, target)
                wal = Path(str(target) + '-wal')
                wal.unlink(missing_ok=True)
                try:
                    if before[1] is not None:
                        shutil.copyfile(paths[1], wal)
                except FileNotFoundError:
                    continue
                if signature() == before:
                    self.read_signature = json.dumps(before)
                    break
            else:
                raise sqlite3.OperationalError('Hermes source changed during read; retry next scan')
            with closing(sqlite3.connect(target.as_uri() + '?mode=ro', uri=True)) as connection:
                yield connection

    def read(self, previous=None, previous_signature=None):
        if not self.path.is_file():
            return [], (None, 0)
        with self._readonly_snapshot() as connection:
            connection.row_factory = sqlite3.Row
            watermark = tuple(connection.execute(
                'SELECT MAX(last_seen), COUNT(*) FROM session_model_usage').fetchone())
            if (previous is not None and tuple(previous) == watermark
                    and previous_signature == self.read_signature):
                return None, watermark
            rows = connection.execute('''SELECT u.*, s.cwd, s.source, s.profile_name
                FROM session_model_usage u LEFT JOIN sessions s ON s.id=u.session_id
                ORDER BY u.session_id,u.model,u.billing_provider,u.task''').fetchall()
        return [self._record(dict(row), i) for i, row in enumerate(rows, 1)], watermark

    def _record(self, raw, line):
        at = dt.datetime.fromtimestamp(float(raw['last_seen']), dt.timezone.utc)
        local = at.astimezone(self.timezone)
        record = dict(raw)
        record.update(source_kind='hermes', source_path=str(self.path), source_line=line,
            timestamp=at.isoformat(), day=local.date().isoformat(), hour=local.hour,
            response_id=None, thread_id=None, thread_key=raw['session_id'],
            parent_thread_key=None, task_id=raw['task'] or '',
            agent_role='subagent' if raw['task'] else 'main',
            provider=raw['billing_provider'] or 'unknown',
            workspace=raw['cwd'] or raw['source'] or 'unknown',
            workspace_source='sessions.cwd' if raw['cwd'] else 'sessions.source',
            cached_input_tokens=int(raw['cache_read_tokens'] or 0),
            cache_write_input_tokens=int(raw['cache_write_tokens'] or 0),
            reasoning_output_tokens=int(raw['reasoning_tokens'] or 0),
            total_tokens=int(raw['input_tokens'] or 0) + int(raw['output_tokens'] or 0),
            cache_read_available=1, cache_write_available=1,
            speed_group_key=None, speed_output_tokens=0, speed_duration_ms=None,
            speed_exclusion_reason=EXCLUSION, ttft_ms=None,
            request_duration_estimate_ms=None, request_output_tokens_per_second_estimate=None,
            request_timing_source=None, request_speed_exclusion_reason=EXCLUSION,
            context_window_tokens=None, context_input_tokens=None, context_observed_at=None,
            hermes_estimated_cost_usd=raw['estimated_cost_usd'],
            hermes_actual_cost_usd=raw['actual_cost_usd'])
        return record

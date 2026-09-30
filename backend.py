#!/usr/bin/env python3
"""Local Codex token dashboard backend.

The backend deliberately stores only usage metadata.  It never persists or
returns rollout prompts, response text, environment variables, or secrets.
It is a single-file, Python 3.9+ standard-library application so it can be
copied next to the dashboard web assets and run without a package manager.
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pricing import DEFAULT_PRICING, canonical_model


APP_DIR = Path(__file__).resolve().parent
DEFAULT_DB = APP_DIR / "codex-token-dashboard.sqlite3"
DEFAULT_PROVIDER_CONFIG = APP_DIR / "providers.json"
PARSER_SCHEMA_VERSION = 3
APP_VERSION = "1.2.0"

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass


def safe_csv_row(row):
    """Keep text cells from being interpreted as spreadsheet formulas."""
    return {key: ("'" + value if isinstance(value, str)
            and value.lstrip().startswith(("=", "+", "-", "@")) else value)
            for key, value in row.items()}


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return None


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return max(0, int(value))
    if isinstance(value, str):
        try:
            return max(0, int(float(value.strip())))
        except (TypeError, ValueError):
            return None
    return None


def _resolve_timezone(name: Optional[str]) -> _dt.tzinfo:
    if name:
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError as exc:
            # The python.org Windows build does not bundle the IANA tzdata
            # database. Keep the zero-dependency launcher useful for the two
            # zones used by the app and its Windows-first audience. Shanghai
            # has had a fixed UTC+08:00 offset since 1991, which covers all
            # Codex rollout timestamps.
            fixed_timezones = {
                "UTC": _dt.timezone.utc,
                "Etc/UTC": _dt.timezone.utc,
                "Asia/Shanghai": _dt.timezone(_dt.timedelta(hours=8), "Asia/Shanghai"),
            }
            if name in fixed_timezones:
                return fixed_timezones[name]
            raise ValueError(f"unknown timezone: {name}") from exc
    return _dt.datetime.now().astimezone().tzinfo or _dt.timezone.utc


def _parse_timestamp(
    value: Any,
    fallback: Optional[float] = None,
    timezone: Optional[_dt.tzinfo] = None,
) -> Tuple[str, str, int]:
    """Return a UTC ISO timestamp plus day/hour in the selected timezone."""

    display_timezone = timezone or _resolve_timezone(None)
    dt: Optional[_dt.datetime] = None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            dt = _dt.datetime.fromtimestamp(float(value), tz=_dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            dt = None
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            dt = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                dt = _dt.datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                dt = None
    if dt is None:
        dt = _dt.datetime.fromtimestamp(fallback or time.time(), tz=display_timezone)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=display_timezone)
    local_dt = dt.astimezone(display_timezone)
    return dt.astimezone(_dt.timezone.utc).isoformat(), local_dt.date().isoformat(), local_dt.hour


def _safe_workspace(value: Any) -> str:
    if value is None:
        return "Unknown"
    text = str(value).strip()
    return text or "Unknown"


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cache_write_input_tokens: int
    reasoning_output_tokens: int
    total_tokens: int


def parse_usage(value: Any) -> Optional[Usage]:
    """Parse a single request's usage object.

    ``reasoning_output_tokens`` is an output subset in Codex telemetry.  It is
    kept as a separate column but is intentionally never added to output or
    total a second time.
    """

    usage = _as_dict(value)
    if not usage:
        return None
    input_tokens = _as_int(_first(usage, "input_tokens", "prompt_tokens", "prompt_token_count"))
    output_tokens = _as_int(_first(usage, "output_tokens", "completion_tokens", "completion_token_count"))
    cached = _as_int(
        _first(
            usage,
            "cached_input_tokens",
            "cache_read_input_tokens",
            "cache_read_tokens",
            "cached_tokens",
        )
    )
    cache_write = _as_int(
        _first(
            usage,
            "cache_write_input_tokens",
            "cache_creation_input_tokens",
            "cache_write_tokens",
        )
    )
    reasoning = _as_int(_first(usage, "reasoning_output_tokens", "reasoning_tokens"))
    total = _as_int(_first(usage, "total_tokens", "total_token_count", "tokens"))

    fields = (input_tokens, output_tokens, cached, cache_write, reasoning, total)
    if not any(value is not None for value in fields):
        return None
    input_tokens = input_tokens or 0
    output_tokens = output_tokens or 0
    cached = cached or 0
    cache_write = cache_write or 0
    reasoning = reasoning or 0
    # The service reports the provider's total when available.  Legacy
    # records occasionally omit it; input + output is the least surprising
    # fallback because cache/reasoning fields are subsets.
    total = total if total is not None else input_tokens + output_tokens
    return Usage(input_tokens, output_tokens, cached, cache_write, reasoning, total)


class ProviderCatalog:
    """Resolve provider ids to safe display labels and filter aliases."""

    def __init__(self, config_path: Optional[Path] = None) -> None:
        self.labels: Dict[str, str] = {}
        self.alias_to_provider: Dict[str, str] = {}
        self.config_path = Path(config_path) if config_path else DEFAULT_PROVIDER_CONFIG
        self.reload()

    def reload(self) -> None:
        # Rebuild aliases from disk on every dashboard query.  This makes a
        # providers.json rename visible immediately and prevents labels that
        # were removed from the file from lingering in a long-running app.
        self.labels = {"openai": "OpenAI 官方"}
        self.alias_to_provider = {"openai": "openai", "OpenAI 官方".casefold(): "openai"}
        path = self.config_path
        if not path or not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(data, dict):
            return
        aliases = data.get("aliases") if isinstance(data.get("aliases"), dict) else {}
        providers = data.get("providers") if isinstance(data.get("providers"), dict) else {}
        # Both {"provider-id": "Display name"} and a nested providers map
        # are accepted, making providers.json easy to edit by hand.
        merged: Dict[str, Any] = {}
        merged.update(providers)
        merged.update(aliases)
        for provider, value in merged.items():
            provider_id = str(provider).strip()
            if not provider_id:
                continue
            label: Optional[str] = None
            if isinstance(value, str):
                label = value.strip()
            elif isinstance(value, dict):
                label_value = _first(value, "display_name", "displayName", "label", "name")
                if label_value is not None:
                    label = str(label_value).strip()
            if label:
                self.labels[provider_id] = label
                self.alias_to_provider[provider_id.casefold()] = provider_id
                self.alias_to_provider[label.casefold()] = provider_id

    def label(self, provider: Any) -> str:
        provider_id = str(provider or "unknown").strip() or "unknown"
        if provider_id.casefold() == "openai":
            return "OpenAI 官方"
        return self.labels.get(provider_id, provider_id)

    def resolve(self, value: Optional[str]) -> Optional[List[str]]:
        if value is None or not str(value).strip():
            return None
        text = str(value).strip()
        resolved = self.alias_to_provider.get(text.casefold())
        return [resolved or text]


@dataclass(frozen=True)
class ParsedRecord:
    source_path: str
    source_line: int
    timestamp: str
    day: str
    hour: int
    response_id: Optional[str]
    thread_id: Optional[str]
    provider: str
    provider_label: str
    model: str
    workspace: str
    usage: Usage


@dataclass
class ScanResult:
    files_seen: int = 0
    files_scanned: int = 0
    files_unchanged: int = 0
    files_removed: int = 0
    records_added: int = 0
    records_skipped: int = 0
    parse_errors: int = 0
    incomplete_files: int = 0
    unavailable_roots: int = 0

    def as_dict(self) -> Dict[str, int]:
        return {
            "files_seen": self.files_seen,
            "files_scanned": self.files_scanned,
            "files_unchanged": self.files_unchanged,
            "files_removed": self.files_removed,
            "records_added": self.records_added,
            "records_skipped": self.records_skipped,
            "parse_errors": self.parse_errors,
            "incomplete_files": self.incomplete_files,
            "unavailable_roots": self.unavailable_roots,
        }


class RolloutParser:
    """Parse only metadata and usage from a rollout JSONL file."""

    def __init__(self, catalog: ProviderCatalog, timezone: Optional[_dt.tzinfo] = None) -> None:
        self.catalog = catalog
        self.timezone = timezone or _resolve_timezone(None)
        self._parse_errors = 0

    def _iter_json(self, path: Path) -> Iterator[Tuple[int, Dict[str, Any]]]:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line_number, line in enumerate(handle, 1):
                    if not line.strip():
                        continue
                    try:
                        value = json.loads(line)
                    except (ValueError, TypeError):
                        # A non-newline-terminated tail is commonly a record
                        # still being written; retry it on the next scan.
                        if line.endswith(("\n", "\r")):
                            self._parse_errors += 1
                        continue
                    if isinstance(value, dict):
                        yield line_number, value
        except (OSError, UnicodeError):
            self._read_failed = True
            self._parse_errors += 1
            return

    @staticmethod
    def _event_type(event: Mapping[str, Any]) -> str:
        return str(event.get("type") or "").strip()

    @staticmethod
    def _payload(event: Mapping[str, Any]) -> Dict[str, Any]:
        payload = event.get("payload")
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _thread_id(event: Mapping[str, Any], payload: Mapping[str, Any]) -> Optional[str]:
        value = _first(payload, "thread_id", "threadId") or _first(event, "thread_id", "threadId")
        return str(value).strip() if value is not None and str(value).strip() else None

    def parse(self, path: Path) -> Tuple[List[ParsedRecord], int]:
        # Detect a modern record before parsing old token_count events.  A
        # rollout can contain both during migration; modern usage is the
        # single-request source of truth and old totals would double count it.
        self._parse_errors = 0
        self._read_failed = False
        has_modern = False
        try:
            for _, event in self._iter_json(path):
                if self._event_type(event) == "token_usage_record":
                    has_modern = True
                    break
        except Exception:
            pass

        # Reset errors from the lightweight schema-detection pass; report
        # malformed lines exactly once from the real parse below.
        self._parse_errors = 0
        session_provider = "unknown"
        session_workspace = "Unknown"
        session_thread_id: Optional[str] = None
        current_model = "unknown"
        current_workspace = session_workspace
        current_provider = session_provider
        current_turn_id: Optional[str] = None
        turn_sequence = 0
        # A thread can switch model/provider several times.  Settings are
        # keyed by thread id, while turn_context is a stream-level event.
        thread_states: Dict[str, Dict[str, str]] = {}
        # Very old token_count telemetry can contain only a session-wide
        # cumulative counter.  Keep its state per inherited thread/session;
        # a model switch changes attribution, not the counter's baseline.
        cumulative_usage: Dict[str, Usage] = {}
        cumulative_snapshot_index: Dict[str, int] = {}
        legacy_request_index: Dict[str, int] = {}
        records: List[ParsedRecord] = []
        parse_errors = 0
        try:
            fallback_mtime = path.stat().st_mtime
        except OSError:
            fallback_mtime = time.time()

        for line_number, event in self._iter_json(path):
            event_type = self._event_type(event)
            payload = self._payload(event)
            payload_type = str(payload.get("type") or "").strip()

            if event_type == "session_meta":
                session_provider = str(
                    _first(payload, "model_provider", "model_provider_id", "provider") or session_provider
                ).strip() or session_provider
                session_workspace = _safe_workspace(_first(payload, "cwd", "workspace"))
                current_provider = session_provider
                current_workspace = session_workspace
                thread_value = _first(payload, "id", "thread_id", "threadId", "session_id")
                if thread_value is not None and str(thread_value).strip():
                    session_thread_id = str(thread_value).strip()
                    state = thread_states.setdefault(session_thread_id, {})
                    state["provider"] = session_provider
                    state["workspace"] = session_workspace
                continue

            if event_type == "turn_context":
                turn_sequence += 1
                turn_value = _first(payload, "turn_id", "turnId", "id") or _first(
                    event, "turn_id", "turnId", "id"
                )
                current_turn_id = (
                    str(turn_value).strip()
                    if turn_value is not None and str(turn_value).strip()
                    else "stream-%d" % turn_sequence
                )
                model_value = _first(payload, "model", "model_id")
                if model_value is not None and str(model_value).strip():
                    current_model = str(model_value).strip()
                cwd = _first(payload, "cwd", "workspace")
                if cwd is not None:
                    current_workspace = _safe_workspace(cwd)
                thread_id = self._thread_id(event, payload) or session_thread_id
                if thread_id:
                    state = thread_states.setdefault(thread_id, {})
                    if model_value is not None and str(model_value).strip():
                        state["model"] = current_model
                    if cwd is not None:
                        state["workspace"] = current_workspace
                    state["turn_id"] = current_turn_id
                continue

            # Current Codex uses event_msg/thread_settings_applied.  Accept a
            # top-level settings object too for older snapshots and fixtures.
            if event_type == "event_msg" and payload_type == "thread_settings_applied":
                settings = payload.get("thread_settings")
                settings = settings if isinstance(settings, dict) else payload
                thread_id = self._thread_id(event, payload) or session_thread_id
                state = thread_states.setdefault(thread_id, {}) if thread_id else None
                model_value = _first(settings, "model", "model_id")
                provider_value = _first(settings, "model_provider_id", "model_provider", "provider")
                cwd = _first(settings, "cwd", "workspace")
                if model_value is not None and str(model_value).strip():
                    current_model = str(model_value).strip()
                    if state is not None:
                        state["model"] = current_model
                if provider_value is not None and str(provider_value).strip():
                    current_provider = str(provider_value).strip()
                    if state is not None:
                        state["provider"] = current_provider
                if cwd is not None:
                    current_workspace = _safe_workspace(cwd)
                    if state is not None:
                        state["workspace"] = current_workspace
                continue

            if event_type == "token_usage_record":
                usage_container = payload.get("usage")
                if not isinstance(usage_container, dict):
                    usage_container = event.get("usage")
                usage = parse_usage(usage_container)
                if usage is None:
                    continue
                # Most old and some current records omit thread_id.  The
                # session metadata is their stable ownership context.
                thread_id = self._thread_id(event, payload) or session_thread_id
                state = thread_states.get(thread_id or "", {})
                provider = str(
                    _first(payload, "model_provider_id", "model_provider", "provider")
                    or state.get("provider")
                    or current_provider
                    or session_provider
                    or "unknown"
                ).strip() or "unknown"
                model = str(
                    _first(payload, "model", "model_id")
                    or state.get("model")
                    or current_model
                    or "unknown"
                ).strip() or "unknown"
                workspace = _safe_workspace(
                    _first(payload, "cwd", "workspace") or state.get("workspace") or current_workspace
                )
                response_value = _first(payload, "response_id", "responseId") or _first(event, "response_id")
                response_id = str(response_value).strip() if response_value is not None else None
                response_id = response_id or None
                timestamp, day, hour = _parse_timestamp(event.get("timestamp"), fallback_mtime, self.timezone)
                records.append(
                    ParsedRecord(
                        str(path),
                        line_number,
                        timestamp,
                        day,
                        hour,
                        response_id,
                        thread_id,
                        provider,
                        self.catalog.label(provider),
                        model,
                        workspace,
                        usage,
                    )
                )
                continue

            if (
                not has_modern
                and event_type == "event_msg"
                and payload_type == "token_count"
            ):
                info = _as_dict(payload.get("info"))
                # last_token_usage is per request.  total_token_usage is only
                # a compatibility fallback for very old files without it.
                usage = parse_usage(info.get("last_token_usage"))
                if usage is None:
                    usage = parse_usage(info.get("usage"))
                is_cumulative = usage is None
                total_snapshot = parse_usage(info.get("total_token_usage"))
                if usage is None:
                    usage = total_snapshot
                if usage is None:
                    continue
                thread_id = self._thread_id(event, payload) or session_thread_id
                state = thread_states.get(thread_id or "", {})
                provider = str(state.get("provider") or current_provider or session_provider or "unknown").strip()
                model = str(state.get("model") or current_model or "unknown").strip()
                workspace = _safe_workspace(state.get("workspace") or current_workspace)
                context_id = thread_id or session_thread_id or "session"
                if is_cumulative:
                    previous = cumulative_usage.get(context_id)
                    cumulative_usage[context_id] = usage
                    if previous is not None and usage.total_tokens == previous.total_tokens:
                        # Repeated snapshots report no new request usage.
                        continue
                    if previous is not None and usage.total_tokens > previous.total_tokens:
                        usage = Usage(
                            max(0, usage.input_tokens - previous.input_tokens),
                            max(0, usage.output_tokens - previous.output_tokens),
                            max(0, usage.cached_input_tokens - previous.cached_input_tokens),
                            max(0, usage.cache_write_input_tokens - previous.cache_write_input_tokens),
                            max(0, usage.reasoning_output_tokens - previous.reasoning_output_tokens),
                            usage.total_tokens - previous.total_tokens,
                        )
                    # A lower cumulative value means Codex reset the counter.
                    # Count the new baseline once instead of emitting a
                    # negative delta.  Model changes intentionally do not
                    # reset this per-session counter.
                    cumulative_snapshot_index[context_id] = cumulative_snapshot_index.get(context_id, 0) + 1
                    response_id = "legacy:%s:cumulative:%d" % (
                        context_id,
                        cumulative_snapshot_index[context_id],
                    )
                else:
                    response_value = _first(payload, "response_id", "responseId") or _first(info, "response_id")
                    response_id = str(response_value).strip() if response_value is not None else None
                    if not response_id:
                        turn_value = (
                            _first(payload, "turn_id", "turnId")
                            or _first(event, "turn_id", "turnId")
                            or state.get("turn_id")
                            or current_turn_id
                        )
                        # A total snapshot alongside last_token_usage gives us
                        # the missing request boundary for old logs. Identical
                        # snapshots keep the same key; a changed cumulative
                        # total becomes a second request even in one turn.
                        request_index: Optional[int] = None
                        if total_snapshot is not None:
                            previous_total = cumulative_usage.get(context_id)
                            cumulative_usage[context_id] = total_snapshot
                            if (
                                previous_total is None
                                or total_snapshot.total_tokens != previous_total.total_tokens
                            ):
                                legacy_request_index[context_id] = legacy_request_index.get(context_id, 0) + 1
                            request_index = legacy_request_index.get(context_id, 1)
                        if request_index is not None:
                            response_id = "legacy:%s:request:%d" % (context_id, request_index)
                        else:
                            # Without a request id or cumulative counter there
                            # is no reliable boundary. Preserve the historical
                            # conservative dedupe rule, but include the
                            # inherited session and turn so separate sessions
                            # cannot collide. Different usage in the same turn
                            # remains distinct.
                            fingerprint = "|".join(
                                str(value or "")
                                for value in (
                                    context_id, turn_value, provider, model,
                                    usage.input_tokens, usage.output_tokens,
                                    usage.cached_input_tokens, usage.cache_write_input_tokens,
                                    usage.reasoning_output_tokens, usage.total_tokens,
                                )
                            )
                            response_id = "legacy:" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
                timestamp, day, hour = _parse_timestamp(event.get("timestamp"), fallback_mtime, self.timezone)
                records.append(
                    ParsedRecord(
                        str(path),
                        line_number,
                        timestamp,
                        day,
                        hour,
                        response_id or None,
                        thread_id,
                        provider or "unknown",
                        self.catalog.label(provider or "unknown"),
                        model or "unknown",
                        workspace,
                        usage,
                    )
                )
        return records, self._parse_errors


class DashboardDB:
    """Incremental SQLite cache and all dashboard aggregations."""

    def __init__(
        self,
        db_path: Path | str = DEFAULT_DB,
        roots: Optional[Sequence[Path | str]] = None,
        providers_path: Optional[Path | str] = None,
        timezone_name: Optional[str] = None,
        clock: Optional[Callable[[], _dt.datetime]] = None,
    ) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.roots = [Path(root).expanduser() for root in roots] if roots else default_roots()
        self.timezone_name = timezone_name or str(_resolve_timezone(None))
        self.timezone = _resolve_timezone(timezone_name)
        self._clock = clock or (lambda: _dt.datetime.now(self.timezone))
        self.catalog = ProviderCatalog(Path(providers_path).expanduser() if providers_path else None)
        self.parser = RolloutParser(self.catalog, self.timezone)
        self.pricing = DEFAULT_PRICING
        self.database_recovery: Optional[Dict[str, Any]] = None
        self.database_migration: Optional[Dict[str, Any]] = None
        self._query_lock = threading.RLock()
        self._dashboard_cache = {}
        self._data_version = 0
        self._lock = threading.RLock()
        self._connection = self._open_connection()
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._create_schema()

    def _open_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path), check_same_thread=False)
        try:
            check = connection.execute("PRAGMA quick_check(1)").fetchone()
            healthy = bool(check and str(check[0]).casefold() == "ok")
        except sqlite3.DatabaseError as error:
            connection.close()
            if "malformed" not in str(error).casefold():
                raise
            healthy = False
        if healthy:
            return connection

        connection.close()
        backups: List[str] = []
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(self.db_path) + suffix)
            if not candidate.exists():
                continue
            backup = Path(f"{candidate}.corrupt-{stamp}.bak")
            candidate.replace(backup)
            backups.append(str(backup))
        self.database_recovery = {
            "status": "rebuilt",
            "reason": "database failed SQLite quick_check",
            "backups": backups,
        }
        return sqlite3.connect(str(self.db_path), check_same_thread=False)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS source_files (
                path TEXT PRIMARY KEY,
                mtime_ns INTEGER NOT NULL,
                size INTEGER NOT NULL,
                scanned_at TEXT NOT NULL,
                record_count INTEGER NOT NULL DEFAULT 0,
                timezone TEXT NOT NULL DEFAULT '',
                parser_version INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS usage_records (
                record_key TEXT PRIMARY KEY,
                source_path TEXT NOT NULL,
                source_line INTEGER NOT NULL,
                response_id TEXT,
                thread_id TEXT,
                timestamp TEXT NOT NULL,
                day TEXT NOT NULL,
                hour INTEGER NOT NULL,
                provider TEXT NOT NULL,
                provider_label TEXT NOT NULL,
                model TEXT NOT NULL,
                workspace TEXT NOT NULL,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                cached_input_tokens INTEGER NOT NULL,
                cache_write_input_tokens INTEGER NOT NULL,
                reasoning_output_tokens INTEGER NOT NULL,
                total_tokens INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS usage_sources (
                source_key TEXT PRIMARY KEY,
                logical_key TEXT NOT NULL,
                source_path TEXT NOT NULL,
                source_line INTEGER NOT NULL,
                response_id TEXT,
                thread_id TEXT,
                timestamp TEXT NOT NULL,
                day TEXT NOT NULL,
                hour INTEGER NOT NULL,
                provider TEXT NOT NULL,
                provider_label TEXT NOT NULL,
                model TEXT NOT NULL,
                workspace TEXT NOT NULL,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                cached_input_tokens INTEGER NOT NULL,
                cache_write_input_tokens INTEGER NOT NULL,
                reasoning_output_tokens INTEGER NOT NULL,
                total_tokens INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_usage_day ON usage_records(day);
            CREATE INDEX IF NOT EXISTS idx_usage_provider ON usage_records(provider);
            CREATE INDEX IF NOT EXISTS idx_usage_workspace ON usage_records(workspace);
            CREATE INDEX IF NOT EXISTS idx_usage_source_logical ON usage_sources(logical_key);
            CREATE INDEX IF NOT EXISTS idx_usage_source_path ON usage_sources(source_path);
            DROP INDEX IF EXISTS idx_usage_response_id;
            DROP INDEX IF EXISTS idx_usage_thread_response_id;
            """
        )
        source_columns = {
            str(row["name"])
            for row in self._connection.execute("PRAGMA table_info(source_files)").fetchall()
        }
        if "timezone" not in source_columns:
            self._connection.execute("ALTER TABLE source_files ADD COLUMN timezone TEXT NOT NULL DEFAULT ''")
        if "parser_version" not in source_columns:
            self._connection.execute("ALTER TABLE source_files ADD COLUMN parser_version INTEGER NOT NULL DEFAULT 0")

        # Version 1 stored one deduplicated row directly in usage_records.
        # Seed the new per-source ledger before rebuilding logical rows so a
        # deployment never discards an existing cache merely because a root
        # drive is temporarily unavailable.  Reachable files are reparsed on
        # their next scan because their parser_version is still zero.
        source_count = self._connection.execute("SELECT COUNT(*) FROM usage_sources").fetchone()[0]
        if not source_count:
            old_rows = self._connection.execute("SELECT * FROM usage_records").fetchall()
            if old_rows:
                try:
                    backup = self._backup_before_source_ledger_migration()
                    self._connection.execute("BEGIN")
                    # v1 keys are physical path:line values while v2 keys are
                    # logical request hashes. Clear only inside this
                    # transaction, after the on-disk backup succeeds, so
                    # rebuilding cannot leave both generations counted.
                    self._connection.execute("DELETE FROM usage_records")
                    keys = set()
                    for row in old_rows:
                        logical_key = self._logical_key(
                            row["thread_id"], row["response_id"], row["source_path"], int(row["source_line"])
                        )
                        keys.add(logical_key)
                        self._connection.execute(
                            """
                            INSERT OR IGNORE INTO usage_sources (
                                source_key, logical_key, source_path, source_line, response_id, thread_id,
                                timestamp, day, hour, provider, provider_label, model, workspace,
                                input_tokens, output_tokens, cached_input_tokens,
                                cache_write_input_tokens, reasoning_output_tokens, total_tokens
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                "legacy-cache:" + str(row["record_key"]), logical_key,
                                row["source_path"], row["source_line"], row["response_id"], row["thread_id"],
                                row["timestamp"], row["day"], row["hour"], row["provider"], row["provider_label"],
                                row["model"], row["workspace"], row["input_tokens"], row["output_tokens"],
                                row["cached_input_tokens"], row["cache_write_input_tokens"],
                                row["reasoning_output_tokens"], row["total_tokens"],
                            ),
                        )
                    self._rebuild_logical_records(keys)
                    self._connection.commit()
                    self.database_migration = {
                        "status": "rebuilt_source_ledger",
                        "backup": backup,
                        "records": len(old_rows),
                    }
                except Exception:
                    self._connection.rollback()
                    raise
        self._connection.commit()

    def _backup_before_source_ledger_migration(self) -> str:
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        backup_path = Path(str(self.db_path) + ".pre-source-ledger-" + stamp + ".bak")
        backup = sqlite3.connect(str(backup_path))
        try:
            self._connection.backup(backup)
        finally:
            backup.close()
        return str(backup_path)

    @staticmethod
    def _logical_key(
        thread_id: Optional[str], response_id: Optional[str], source_path: str, source_line: int
    ) -> str:
        if response_id:
            identity = "response\0%s\0%s" % (thread_id or "", response_id)
        else:
            identity = "source\0%s\0%d" % (source_path, source_line)
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    @staticmethod
    def _source_key(path: str, line: int) -> str:
        return hashlib.sha256((path + "\0" + str(line)).encode("utf-8")).hexdigest()

    def _rebuild_logical_records(self, logical_keys: Iterable[str]) -> None:
        """Rebuild changed request keys from all their independently stored sources."""

        for logical_key in set(logical_keys):
            source_rows = self._connection.execute(
                """
                SELECT * FROM usage_sources
                WHERE logical_key = ?
                ORDER BY timestamp, source_path, source_line
                """,
                (logical_key,),
            ).fetchall()
            self._connection.execute("DELETE FROM usage_records WHERE record_key = ?", (logical_key,))
            if not source_rows:
                continue
            # A correction is one coherent response snapshot, never column maxima.
            # Prefer the fullest snapshot (total and optional breakdowns),
            # then complete attribution and deterministic recency.
            representative = max(source_rows, key=lambda row: (
                row["total_tokens"],
                sum(bool(row[key]) for key in ("cached_input_tokens",
                    "cache_write_input_tokens", "reasoning_output_tokens")),
                sum(row[key] not in (None, "", "unknown", "Unknown")
                    for key in ("model", "provider", "workspace")),
                row["timestamp"], row["source_line"], row["source_path"],
            ))
            maxima = representative
            self._connection.execute(
                """
                INSERT INTO usage_records (
                    record_key, source_path, source_line, response_id, thread_id,
                    timestamp, day, hour, provider, provider_label, model, workspace,
                    input_tokens, output_tokens, cached_input_tokens,
                    cache_write_input_tokens, reasoning_output_tokens, total_tokens
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    logical_key, representative["source_path"], representative["source_line"],
                    representative["response_id"], representative["thread_id"], representative["timestamp"],
                    representative["day"], representative["hour"], representative["provider"],
                    representative["provider_label"], representative["model"], representative["workspace"],
                    maxima["input_tokens"], maxima["output_tokens"], maxima["cached_input_tokens"],
                    maxima["cache_write_input_tokens"], maxima["reasoning_output_tokens"], maxima["total_tokens"],
                ),
            )

    def _files(self) -> List[Path]:
        found: Dict[str, Path] = {}
        self._complete_roots = []
        self._unavailable_roots = 0
        for root in self.roots:
            try:
                if not root.is_dir():
                    self._unavailable_roots += 1
                    continue
                errors = []
                # os.walk reports enumeration failures; rglob can suppress them.
                for directory, _, filenames in os.walk(root, onerror=errors.append):
                    for name in filenames:
                        if name.lower().endswith(".jsonl"):
                            path = (Path(directory) / name).resolve()
                            found[str(path)] = path
                if errors:
                    self._unavailable_roots += 1
                else:
                    self._complete_roots.append(root)
            except OSError:
                self._unavailable_roots += 1
        return sorted(found.values(), key=lambda path: str(path).casefold())

    def _local_now(self) -> _dt.datetime:
        value = self._clock()
        if value.tzinfo is None:
            return value.replace(tzinfo=self.timezone)
        return value.astimezone(self.timezone)

    def scan(self) -> ScanResult:
        result = ScanResult()
        self._complete_roots = []
        self._unavailable_roots = 0
        files = self._files()
        result.unavailable_roots = self._unavailable_roots
        result.files_seen = len(files)
        seen = {str(path) for path in files}
        with self._lock:
            existing_rows = self._connection.execute(
                "SELECT path, mtime_ns, size, timezone, parser_version FROM source_files"
            ).fetchall()
            existing = {
                str(row["path"]): (
                    int(row["mtime_ns"]), int(row["size"]), str(row["timezone"]), int(row["parser_version"])
                )
                for row in existing_rows
            }
            active_roots = [root for root in self.roots if root.exists()]
            removed_paths = [
                path for path in existing
                if path not in seen and any(_is_within(Path(path), root) for root in active_roots)
            ]
            try:
                self._connection.execute("BEGIN")
                for path in files:
                    key = str(path)
                    try:
                        stat = path.stat()
                    except OSError:
                        result.incomplete_files += 1
                        continue
                    signature = (
                        int(stat.st_mtime_ns), int(stat.st_size), self.timezone_name, PARSER_SCHEMA_VERSION
                    )
                    if existing.get(key) == signature:
                        result.files_unchanged += 1
                        continue
                    result.files_scanned += 1
                    records, parse_errors = self.parser.parse(path)
                    result.parse_errors += parse_errors
                    try:
                        after = path.stat()
                        stable = (after.st_size, after.st_mtime_ns) == (stat.st_size, stat.st_mtime_ns)
                    except OSError:
                        stable = False
                    if self.parser._read_failed or not stable:
                        result.incomplete_files += 1
                        continue
                    old_keys = {
                        str(row["logical_key"])
                        for row in self._connection.execute(
                            "SELECT logical_key FROM usage_sources WHERE source_path = ?", (key,)
                        ).fetchall()
                    }
                    # Do not replace a usable migrated cache with an unreadable
                    # file. A malformed tail with valid records still updates.
                    if parse_errors and not records and old_keys:
                        result.records_skipped += len(old_keys)
                        continue
                    self._connection.execute("DELETE FROM usage_sources WHERE source_path = ?", (key,))
                    added = 0
                    skipped = 0
                    affected_keys = set(old_keys)
                    for record in records:
                        u = record.usage
                        logical_key = self._logical_key(
                            record.thread_id, record.response_id, key, record.source_line
                        )
                        affected_keys.add(logical_key)
                        self._connection.execute(
                            """
                            INSERT INTO usage_sources (
                                source_key, logical_key, source_path, source_line, response_id, thread_id,
                                timestamp, day, hour, provider, provider_label, model, workspace,
                                input_tokens, output_tokens, cached_input_tokens,
                                cache_write_input_tokens, reasoning_output_tokens, total_tokens
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                self._source_key(key, record.source_line), logical_key, key, record.source_line,
                                record.response_id, record.thread_id, record.timestamp, record.day, record.hour,
                                record.provider, record.provider_label, record.model, record.workspace,
                                u.input_tokens, u.output_tokens, u.cached_input_tokens,
                                u.cache_write_input_tokens, u.reasoning_output_tokens, u.total_tokens,
                            ),
                        )
                        added += 1
                    self._rebuild_logical_records(affected_keys)
                    skipped = max(0, added - len({key for key in affected_keys if key not in old_keys}))
                    self._connection.execute(
                        """
                        INSERT INTO source_files(path, mtime_ns, size, scanned_at, record_count, timezone, parser_version)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(path) DO UPDATE SET
                            mtime_ns=excluded.mtime_ns,
                            size=excluded.size,
                            scanned_at=excluded.scanned_at,
                            record_count=excluded.record_count,
                            timezone=excluded.timezone,
                            parser_version=excluded.parser_version
                        """,
                        (key, signature[0], signature[1], _now_iso(), added, self.timezone_name, PARSER_SCHEMA_VERSION),
                    )
                    result.records_added += added
                    result.records_skipped += skipped

                # Only prune disappeared files beneath roots that still exist;
                # a disconnected/renamed root must not erase its cache.
                for path, _signature in existing.items():
                    if path in seen:
                        continue
                    if any(_is_within(Path(path), root) for root in self._complete_roots):
                        old_keys = {
                            str(row["logical_key"])
                            for row in self._connection.execute(
                                "SELECT logical_key FROM usage_sources WHERE source_path = ?", (path,)
                            ).fetchall()
                        }
                        self._connection.execute("DELETE FROM usage_sources WHERE source_path = ?", (path,))
                        self._rebuild_logical_records(old_keys)
                        self._connection.execute("DELETE FROM source_files WHERE path = ?", (path,))
                        result.files_removed += 1
                self._connection.commit()
                if result.files_scanned or result.files_removed:
                    self._data_version += 1
            except Exception:
                self._connection.rollback()
                raise
        return result

    def _fetch_rows(
        self,
        days: Optional[int],
        provider: Optional[str],
        workspace: Optional[str],
        model: Optional[str] = None,
    ) -> List[sqlite3.Row]:
        where: List[str] = []
        params: List[Any] = []
        if days is not None and days > 0:
            today = self._local_now().date()
            cutoff = today - _dt.timedelta(days=days - 1)
            # day is an ISO date and is indexed, so this bounds the SQLite
            # read before Python builds dashboard aggregates.
            where.append("day >= ? AND day <= ?")
            params.extend((cutoff.isoformat(), today.isoformat()))
        providers = self.catalog.resolve(provider)
        if providers:
            where.append("provider IN (%s)" % ",".join("?" for _ in providers))
            params.extend(providers)
        if workspace:
            where.append("workspace = ?")
            params.append(workspace)
        predicate = (" WHERE " + " AND ".join(where)) if where else ""
        # WAL permits a separate read connection while the background scanner
        # is rebuilding a changed rollout.  Keeping dashboard reads off the
        # writer lock prevents the desktop window from appearing frozen when
        # a large active session file changes.
        reader = sqlite3.connect(str(self.db_path), timeout=5.0)
        try:
            reader.row_factory = sqlite3.Row
            rows = reader.execute(
                "SELECT * FROM usage_records" + predicate + " ORDER BY day, hour, source_path, source_line",
                params,
            ).fetchall()
        finally:
            reader.close()
        if model:
            # Model filters are matched on the canonical name so that a relay
            # prefix such as "deepseek/deepseek-flash" still matches a
            # "deepseek-flash" selection.
            wanted = canonical_model(model)
            rows = [row for row in rows if canonical_model(row["model"]) == wanted]
        return rows

    @staticmethod
    def _empty_metrics() -> Dict[str, Any]:
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "reasoning_output_tokens": 0,
            "total_tokens": 0,
            "request_count": 0,
            "estimated_cost_usd": 0.0,
            "priced_tokens": 0,
            "unpriced_tokens": 0,
            "priced_request_count": 0,
            "unpriced_request_count": 0,
        }

    @staticmethod
    def _add_metrics(target: Dict[str, Any], row: Mapping[str, Any], quote: Any) -> None:
        for key in (
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "cache_write_input_tokens",
            "reasoning_output_tokens",
            "total_tokens",
        ):
            target[key] += int(row[key] or 0)
        target["request_count"] += 1
        if quote.is_priced:
            target["estimated_cost_usd"] += float(quote.estimated_cost_usd)
            target["priced_tokens"] += int(row["total_tokens"] or 0)
            target["priced_request_count"] += 1
        else:
            target["unpriced_tokens"] += int(row["total_tokens"] or 0)
            target["unpriced_request_count"] += 1

    @staticmethod
    def _metrics_aliases(metrics: Mapping[str, Any]) -> Dict[str, Any]:
        # Short names make the JSON convenient for small charts while the
        # *_tokens names remain canonical and unambiguous.
        aliases = {
            **{key: int(value) for key, value in metrics.items() if key != "estimated_cost_usd"},
            "estimated_cost_usd": round(float(metrics.get("estimated_cost_usd", 0.0)), 8),
            "input": int(metrics["input_tokens"]),
            "output": int(metrics["output_tokens"]),
            "cached": int(metrics["cached_input_tokens"]),
            "cache_write": int(metrics["cache_write_input_tokens"]),
            "reasoning": int(metrics["reasoning_output_tokens"]),
            "total": int(metrics["total_tokens"]),
        }
        aliases["estimated_cost"] = aliases["estimated_cost_usd"]
        coverage_tokens = aliases["priced_tokens"] + aliases["unpriced_tokens"]
        aliases["pricing_coverage"] = (
            aliases["priced_tokens"] / coverage_tokens if coverage_tokens else 0.0
        )
        return aliases

    @staticmethod
    def _percentile(values: Sequence[int], percentile: float) -> int:
        if not values:
            return 0
        ordered = sorted(int(value) for value in values)
        if len(ordered) == 1:
            return ordered[0]
        rank = (len(ordered) - 1) * percentile
        lower = int(rank)
        upper = min(lower + 1, len(ordered) - 1)
        fraction = rank - lower
        return int(round(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction))

    def dashboard(self, days=30, provider=None, workspace=None, model=None):
        # Serialize cache population; repeated SSE/filter requests reuse aggregates.
        with self._query_lock:
            self.catalog.reload()
            key = (self._data_version, self._local_now().date().isoformat(),
                   days, provider, workspace, model, repr(self.catalog.__dict__))
            if key not in self._dashboard_cache:
                if len(self._dashboard_cache) >= 16:
                    self._dashboard_cache.clear()
                self._dashboard_cache[key] = self._build_dashboard(days, provider, workspace, model)
            return self._dashboard_cache[key]

    def _build_dashboard(
        self,
        days: Optional[int] = 30,
        provider: Optional[str] = None,
        workspace: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Dict[str, Any]:
        # Display labels and filter aliases are configuration, not immutable
        # cached data. Reloading here makes provider renames immediately
        # visible for historical rows without rescanning rollout files.
        self.catalog.reload()
        available = self._fetch_rows(days, None, None)
        selected_model = model
        providers = self.catalog.resolve(provider)
        # Relay and provider routers report the same model under different
        # names (for example "deepseek/deepseek-flash" next to
        # "deepseek-flash"). Aggregating on the canonical name keeps them in
        # one row instead of splitting the same model across several.
        model_filter = canonical_model(model) if model else None
        rows = [row for row in available
                if (not providers or row["provider"] in providers)
                and (not workspace or row["workspace"] == workspace)
                and (not model_filter or canonical_model(row["model"]) == model_filter)]
        available_providers = {row['provider']: self.catalog.label(row['provider']) for row in available}
        available_workspaces = sorted({row['workspace'] for row in available})
        summary = self._empty_metrics()
        workspace_groups: Dict[str, Dict[str, Any]] = {}
        active_groups: Dict[Tuple[str, int], Dict[str, Any]] = {}
        daily_groups: Dict[str, Dict[str, Any]] = {}
        model_groups: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
        model_pricing_groups: Dict[Tuple[str, str, str, str], Dict[str, set[str]]] = {}
        model_source_names: Dict[Tuple[str, str, str, str], set[str]] = {}
        unpriced_model_groups: Dict[Tuple[str, str], int] = {}
        request_sizes: List[int] = []
        provider_groups: Dict[str, Dict[str, str]] = {}
        for row in rows:
            quote = self.pricing.quote(
                row["model"],
                timestamp=row["timestamp"],
                input_tokens=int(row["input_tokens"] or 0),
                cached_input_tokens=int(row["cached_input_tokens"] or 0),
                cache_write_input_tokens=int(row["cache_write_input_tokens"] or 0),
                output_tokens=int(row["output_tokens"] or 0),
            )
            self._add_metrics(summary, row, quote)
            request_sizes.append(int(row["total_tokens"] or 0))
            workspace_key = str(row["workspace"])
            workspace_groups.setdefault(workspace_key, self._empty_metrics())
            self._add_metrics(workspace_groups[workspace_key], row, quote)
            active_key = (str(row["day"]), int(row["hour"]))
            active_groups.setdefault(active_key, self._empty_metrics())
            self._add_metrics(active_groups[active_key], row, quote)
            day_key = str(row["day"])
            daily_groups.setdefault(day_key, self._empty_metrics())
            self._add_metrics(daily_groups[day_key], row, quote)
            canonical = canonical_model(row["model"])
            model_key = (
                str(row["day"]),
                str(row["provider"]),
                self.catalog.label(row["provider"]),
                canonical,
            )
            model_groups.setdefault(model_key, self._empty_metrics())
            self._add_metrics(model_groups[model_key], row, quote)
            model_source_names.setdefault(model_key, set()).add(str(row["model"]))
            pricing_group = model_pricing_groups.setdefault(
                model_key,
                {"models": set(), "bands": set(), "sources": set(), "notes": set()},
            )
            if quote.pricing_model:
                pricing_group["models"].add(str(quote.pricing_model))
            if quote.rate_band:
                pricing_group["bands"].add(str(quote.rate_band))
            if quote.source_url:
                pricing_group["sources"].add(str(quote.source_url))
            if quote.note:
                pricing_group["notes"].add(str(quote.note))
            if not quote.is_priced:
                unpriced_key = (str(row["provider"]), canonical)
                unpriced_model_groups[unpriced_key] = (
                    unpriced_model_groups.get(unpriced_key, 0)
                    + int(row["total_tokens"] or 0)
                )
            provider_groups[str(row["provider"])] = {
                "provider": str(row["provider"]),
                "provider_label": self.catalog.label(row["provider"]),
            }

        workspace_distribution = []
        for name, metrics in sorted(workspace_groups.items(), key=lambda item: (-item[1]["total_tokens"], item[0])):
            workspace_distribution.append({"workspace": name, **self._metrics_aliases(metrics)})

        daily_active = []
        for (day, hour), metrics in sorted(active_groups.items()):
            daily_active.append({"date": day, "hour": hour, **self._metrics_aliases(metrics)})
        daily_activity = [
            {"date": day, **self._metrics_aliases(metrics)}
            for day, metrics in sorted(daily_groups.items())
        ]

        daily_model_distribution = []
        daily_model_usage = []
        for (day, provider_id, provider_label, model), metrics in sorted(model_groups.items()):
            priced_requests = int(metrics["priced_request_count"])
            unpriced_requests = int(metrics["unpriced_request_count"])
            if priced_requests and unpriced_requests:
                pricing_status = "partial"
            elif priced_requests:
                pricing_status = "priced"
            else:
                pricing_status = "unpriced"
            pricing_group = model_pricing_groups.get(
                (day, provider_id, provider_label, model),
                {"models": set(), "bands": set(), "sources": set(), "notes": set()},
            )
            item = {
                "date": day,
                "provider": provider_id,
                "provider_label": provider_label,
                "model": model,
                "source_models": sorted(
                    model_source_names.get((day, provider_id, provider_label, model), set())
                ),
                **self._metrics_aliases(metrics),
                "pricing_status": pricing_status,
                "pricing_model": ", ".join(sorted(pricing_group["models"])) or None,
                "pricing_rate_band": ", ".join(sorted(pricing_group["bands"])) or None,
                "pricing_source": next(iter(sorted(pricing_group["sources"])), None),
                "pricing_note": " ".join(sorted(pricing_group["notes"])) or None,
            }
            daily_model_distribution.append(
                {
                    "date": day,
                    "provider": provider_id,
                    "provider_label": provider_label,
                    "model": model,
                    "total_tokens": metrics["total_tokens"],
                    "request_count": metrics["request_count"],
                    "estimated_cost_usd": item["estimated_cost_usd"],
                    "pricing_status": pricing_status,
                }
            )
            daily_model_usage.append(item)

        bucket_specs = (
            (0, 1000, "0-999"),
            (1000, 5000, "1K-4.9K"),
            (5000, 10000, "5K-9.9K"),
            (10000, 20000, "10K-19.9K"),
            (20000, 50000, "20K-49.9K"),
            (50000, 100000, "50K-99.9K"),
            (100000, None, "100K+"),
        )
        histogram = []
        for lower, upper, label in bucket_specs:
            count = sum(1 for value in request_sizes if value >= lower and (upper is None or value < upper))
            histogram.append(
                {
                    "bucket": label,
                    "min_tokens": lower,
                    "max_tokens": upper,
                    "request_count": count,
                    "count": count,
                }
            )
        request_token_distribution = {
            "histogram": histogram,
            "p50": self._percentile(request_sizes, 0.50),
            "p90": self._percentile(request_sizes, 0.90),
            "p99": self._percentile(request_sizes, 0.99),
            "request_count": len(request_sizes),
        }

        summary_aliases = self._metrics_aliases(summary)
        pricing_metadata = self.pricing.metadata()
        pricing_metadata["coverage"] = summary_aliases["pricing_coverage"]
        pricing_metadata["unpriced_model_usage"] = [
            {
                "provider": provider_id,
                "model": model,
                "total_tokens": total_tokens,
            }
            for (provider_id, model), total_tokens in sorted(
                unpriced_model_groups.items(),
                key=lambda item: (-item[1], item[0]),
            )
        ]

        return {
            "generated_at": _now_iso(),
            "timezone": self.timezone_name,
            "local_date": self._local_now().date().isoformat(),
            "days": days,
            "filters": {
                "provider": provider,
                "workspace": workspace,
                "providers": sorted(available_providers),
                "workspaces": available_workspaces,
                "models": sorted({canonical_model(row['model']) for row in available}),
                "model": selected_model,
            },
            "summary": summary_aliases,
            "pricing": pricing_metadata,
            "providers": [{"provider": key, "provider_label": label} for key, label in sorted(available_providers.items())],
            "workspaces": available_workspaces,
            "workspace_distribution": workspace_distribution,
            "workspace_active_distribution": workspace_distribution,
            "daily_active": daily_active,
            "daily_active_distribution": daily_active,
            "daily_activity": daily_activity,
            "hourly_activity": daily_active,
            "request_token_distribution": request_token_distribution,
            "request_distribution": request_token_distribution,
            "request_token_histogram": histogram,
            "daily_model_distribution": daily_model_distribution,
            "model_daily_distribution": daily_model_distribution,
            "daily_model_usage": daily_model_usage,
            "daily_model_summary": daily_model_usage,
        }

    def counts(self) -> Dict[str, int]:
        reader = sqlite3.connect(str(self.db_path), timeout=5.0)
        try:
            files = reader.execute("SELECT COUNT(*) FROM source_files").fetchone()[0]
            records = reader.execute("SELECT COUNT(*) FROM usage_records").fetchone()[0]
        finally:
            reader.close()
        return {"source_files": int(files), "usage_records": int(records)}


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.expanduser().resolve())
        return True
    except (ValueError, OSError):
        return False


def default_roots() -> List[Path]:
    configured_home = os.environ.get("CODEX_HOME")
    codex_home = Path(configured_home).expanduser() if configured_home else Path.home() / ".codex"
    return [codex_home / "sessions", codex_home / "archived_sessions"]


class DashboardService:
    """Scanner plus a small condition-based change stream for SSE clients."""

    def __init__(self, database: DashboardDB, interval: float = 2.0) -> None:
        self.database = database
        self.interval = max(0.25, float(interval))
        self._stop = threading.Event()
        self._changed = threading.Condition()
        self._version = 0
        self._thread: Optional[threading.Thread] = None
        self.last_scan: Dict[str, int] = {}
        self.last_success_at: Optional[str] = None
        self.last_duration_ms: Optional[int] = None
        self.consecutive_failures = 0
        self.last_error: Optional[Dict[str, str]] = None

    @property
    def version(self) -> int:
        with self._changed:
            return self._version

    def refresh(self) -> ScanResult:
        started = time.perf_counter()
        try:
            result = self.database.scan()
        except Exception as error:
            self._record_failure(error)
            raise
        duration_ms = max(0, int(round((time.perf_counter() - started) * 1000)))
        with self._changed:
            self.last_scan = result.as_dict()
            self.last_success_at = _now_iso()
            self.last_duration_ms = duration_ms
            self.consecutive_failures = 0
            self.last_error = None
        changed = bool(result.files_scanned or result.files_removed or result.records_added)
        if changed:
            with self._changed:
                self._version += 1
                self._changed.notify_all()
        return result

    def _record_failure(self, error: BaseException) -> None:
        # Error text could contain a rollout path or other local metadata;
        # expose only the exception category to the local health endpoint.
        with self._changed:
            self.consecutive_failures += 1
            self.last_error = {"category": type(error).__name__, "at": _now_iso()}

    def start(self, initial_refresh: bool = True) -> None:
        if self._thread and self._thread.is_alive():
            return
        if initial_refresh:
            try:
                self.refresh()
            except Exception:
                # Keep the API available so health can describe an initial
                # scanner failure and the polling loop can recover later.
                pass
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="codex-token-scan", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.refresh()
            except Exception:
                # A partially-written JSONL is expected while Codex is active;
                # keep the poller alive and let the next pass retry it.
                continue

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=max(1.0, self.interval + 1.0))

    def wait_for_change(self, version: int, timeout: float = 15.0) -> int:
        with self._changed:
            if self._version <= version:
                self._changed.wait(timeout=max(0.0, timeout))
            return self._version

    def health(self) -> Dict[str, Any]:
        with self._changed:
            if self.consecutive_failures:
                scan_state = "error"
                status = "degraded"
            elif any(self.last_scan.get(key, 0) for key in
                     ("parse_errors", "incomplete_files", "unavailable_roots")):
                scan_state = "partial"
                status = "degraded"
            elif self.last_success_at is None:
                scan_state = "starting"
                status = "ok"
            else:
                scan_state = "ok"
                status = "ok"
            scan = {
                "state": scan_state,
                "last_success_at": self.last_success_at,
                "last_duration_ms": self.last_duration_ms,
                "consecutive_failures": self.consecutive_failures,
                "last_error": dict(self.last_error) if self.last_error else None,
            }
            last_scan = dict(self.last_scan)
        return {
            "status": status,
            "service": "codex-token-dashboard",
            "app_version": APP_VERSION,
            "version": self.version,
            "last_scan": last_scan,
            "scan": scan,
            "database_recovery": self.database.database_recovery,
            **self.database.counts(),
        }


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "CodexTokenDashboard/1.0"

    @property
    def app(self) -> Tuple[DashboardService, str]:
        http_server = self.server  # type: ignore[assignment]
        return http_server.dashboard_service, http_server.dashboard_host  # type: ignore[attr-defined]

    def log_message(self, _format: str, *_args: Any) -> None:
        # The API must never echo request parameters or rollout data into logs.
        return

    def _write_json(self, value: Mapping[str, Any], status: int = HTTPStatus.OK) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _query(self) -> Dict[str, str]:
        parsed = parse_qs(urlparse(self.path).query, keep_blank_values=False)
        return {key: values[-1] for key, values in parsed.items() if values}

    def _write_static(self, filename: str) -> None:
        web_root = (APP_DIR / "web").resolve()
        path = (web_root / filename).resolve()
        if path.parent != web_root or not path.is_file():
            self._write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            body = path.read_bytes()
        except OSError:
            self._write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix == ".js":
            content_type = "text/javascript"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (stdlib handler API)
        service, _host = self.app
        route = urlparse(self.path).path
        static_routes = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/styles.css": "styles.css"}
        if route in static_routes:
            self._write_static(static_routes[route])
            return
        if route == "/api/health":
            self._write_json(service.health())
            return
        if route == "/api/dashboard":
            query = self._query()
            try:
                days = int(query.get("days", "30"))
            except ValueError:
                days = 30
            try:
                value = service.database.dashboard(
                    days=days,
                    provider=query.get("provider"),
                    workspace=query.get("workspace"),
                    model=query.get("model"),
                )
                self._write_json(value)
            except Exception:
                self._write_json({"error": "dashboard_unavailable"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if route == "/api/events":
            self._events(service)
            return
        self._write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

    def _events(self, service: DashboardService) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        version = service.version
        try:
            # Send a complete initial snapshot, then only changed snapshots.
            self._write_sse("dashboard", {"version": version})
            deadline = time.monotonic() + 60.0
            while time.monotonic() < deadline:
                new_version = service.wait_for_change(version, timeout=10.0)
                if new_version != version:
                    version = new_version
                    self._write_sse("dashboard", {"version": version})
                else:
                    self._write_sse("ping", {"version": version})
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _write_sse(self, event: str, data: Mapping[str, Any]) -> None:
        encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        payload = f"event: {event}\ndata: {encoded}\n\n".encode("utf-8")
        self.wfile.write(payload)
        self.wfile.flush()


class DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, _request: Any, _client_address: Any) -> None:
        # Browsers routinely close SSE/keep-alive sockets during navigation.
        # This is normal and should not print connection tracebacks.
        return

    def __init__(self, address: Tuple[str, int], service: DashboardService) -> None:
        super().__init__(address, DashboardHandler)
        self.dashboard_service = service
        self.dashboard_host = address[0]


def create_server(service: DashboardService, host: str = "127.0.0.1", port: int = 8765) -> DashboardHTTPServer:
    return DashboardHTTPServer((host, int(port)), service)


def _common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite cache file")
    parser.add_argument("--root", type=Path, action="append", help="Codex session root; may be repeated")
    parser.add_argument("--providers", type=Path, default=None, help="provider aliases JSON")
    parser.add_argument("--timezone", default=None, help="IANA timezone, default: system local timezone")


def _database_from_args(args: argparse.Namespace) -> DashboardDB:
    roots = args.root if args.root else None
    return DashboardDB(args.db, roots=roots, providers_path=args.providers, timezone_name=args.timezone)


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Local Codex token dashboard backend")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="scan Codex rollout files into SQLite")
    _common_options(scan_parser)

    summary_parser = subparsers.add_parser("summary", help="scan and print aggregate dashboard JSON")
    _common_options(summary_parser)
    summary_parser.add_argument("--days", type=int, default=30)
    summary_parser.add_argument("--provider")
    summary_parser.add_argument("--workspace")

    serve_parser = subparsers.add_parser("serve", help="run HTTP API with background polling")
    _common_options(serve_parser)
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument("--interval", type=float, default=2.0)
    serve_parser.add_argument("--no-open", action="store_true", help="do not open the dashboard in a browser")

    doctor_parser = subparsers.add_parser("doctor", help="check roots, SQLite, and parser availability")
    _common_options(doctor_parser)

    export_parser = subparsers.add_parser("export", help="scan and export aggregate JSON or CSV")
    _common_options(export_parser)
    export_parser.add_argument("--days", type=int, default=30)
    export_parser.add_argument("--provider")
    export_parser.add_argument("--workspace")
    export_parser.add_argument("--format", choices=("json", "csv"), default="json")
    export_parser.add_argument("--output", type=Path)

    args = parser.parse_args(argv)
    db = _database_from_args(args)
    try:
        if args.command == "scan":
            _print_json(db.scan().as_dict())
            return 0
        if args.command == "summary":
            db.scan()
            _print_json(db.dashboard(args.days, args.provider, args.workspace))
            return 0
        if args.command == "doctor":
            roots = []
            for root in db.roots:
                roots.append(
                    {
                        "path": str(root),
                        "exists": root.exists(),
                        "readable": os.access(str(root), os.R_OK) if root.exists() else False,
                    }
                )
            usable = any(item["exists"] and item["readable"] for item in roots)
            _print_json({"status": "ok" if usable else "error", "database": str(db.db_path), "roots": roots, **db.counts()})
            return 0 if usable else 1
        if args.command == "export":
            db.scan()
            dashboard = db.dashboard(args.days, args.provider, args.workspace)
            if args.format == "json":
                output = json.dumps(dashboard, ensure_ascii=False, indent=2, sort_keys=True)
            else:
                rows = dashboard["daily_model_usage"]
                columns = [
                    "date",
                    "provider",
                    "provider_label",
                    "model",
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
                import io

                buffer = io.StringIO()
                writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(safe_csv_row(row) for row in rows)
                output = buffer.getvalue().rstrip("\r\n")
            if args.output:
                args.output.write_text(output + "\n", encoding="utf-8")
            else:
                print(output)
            return 0
        if args.command == "serve":
            service = DashboardService(db, interval=args.interval)
            server: Optional[DashboardHTTPServer] = None
            try:
                service.start()
                server = create_server(service, args.host, args.port)
                browser_host = "127.0.0.1" if args.host in {"0.0.0.0", "::"} else args.host
                url = f"http://{browser_host}:{server.server_port}"
                print(json.dumps({"status": "ok", "url": url}, ensure_ascii=False))
                if not args.no_open:
                    webbrowser.open(url)
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                if server is not None:
                    server.shutdown()
                    server.server_close()
                service.stop()
                db.close()
            return 0
    finally:
        if args.command != "serve":
            db.close()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

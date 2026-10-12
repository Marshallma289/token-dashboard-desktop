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
import ipaddress
import json
import math
import mimetypes
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass, replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pricing import DEFAULT_PRICING, PricingCatalog, canonical_model, validate_pricing_rules


APP_DIR = Path(__file__).resolve().parent
def local_data_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "CodexTokenDashboard"
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "CodexTokenDashboard"


DEFAULT_DB = local_data_dir() / "usage.sqlite3"
DEFAULT_PROVIDER_CONFIG = APP_DIR / "providers.json"
PARSER_SCHEMA_VERSION = 6
APP_VERSION = (Path(__file__).resolve().parent / "VERSION").read_text(encoding="utf-8").strip()

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
    cache_read_available: bool = False
    cache_write_available: bool = False


def _as_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _fresh_input_tokens(input_tokens: Any, cached_input_tokens: Any, cache_write_input_tokens: Any) -> int:
    """Estimate uncached input without letting counters subtract below zero."""
    total = max(0, int(input_tokens or 0))
    cached = min(total, max(0, int(cached_input_tokens or 0)))
    cache_write = min(total - cached, max(0, int(cache_write_input_tokens or 0)))
    return total - cached - cache_write


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
    cache_read_names = (
        "cached_input_tokens", "cache_read_input_tokens", "cache_read_tokens", "cached_tokens",
    )
    cache_write_names = (
        "cache_write_input_tokens", "cache_creation_input_tokens", "cache_write_tokens",
    )
    cached_value = _first(usage, *cache_read_names)
    cache_write_value = _first(usage, *cache_write_names)
    cached = _as_int(cached_value)
    cache_write = _as_int(cache_write_value)
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
    return Usage(input_tokens, output_tokens, cached, cache_write, reasoning, total,
                 cached_value is not None, cache_write_value is not None)


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
    turn_id: Optional[str] = None
    speed_group_key: Optional[str] = None
    speed_output_tokens: int = 0
    speed_duration_ms: float = 0.0
    ttft_ms: Optional[float] = None
    parent_thread_id: Optional[str] = None
    speed_exclusion_reason: Optional[str] = None


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
        self._has_modern = has_modern
        self._parse_errors = 0
        session_provider = "unknown"
        session_workspace = "Unknown"
        session_thread_id: Optional[str] = None
        timing_thread_id: Optional[str] = None
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
        turn_starts: Dict[Tuple[str, str], Mapping[str, Any]] = {}
        turn_timings: Dict[Tuple[str, str], float] = {}
        turn_output_totals: Dict[Tuple[str, str], int] = {}
        turn_ttft: Dict[Tuple[str, str], float] = {}
        parent_by_thread: Dict[str, str] = {}
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
                    timing_thread_id = session_thread_id
                    state = thread_states.setdefault(session_thread_id, {})
                    state["provider"] = session_provider
                    state["workspace"] = session_workspace
                    source = _as_dict(payload.get("source"))
                    subagent = _as_dict(source.get("subagent"))
                    thread_spawn = _as_dict(subagent.get("thread_spawn"))
                    parent_value = (
                        _first(thread_spawn, "parent_thread_id", "parentThreadId")
                        or _first(payload, "forked_from_id", "forkedFromId")
                    )
                    if parent_value is not None and str(parent_value).strip():
                        parent_by_thread[session_thread_id] = str(parent_value).strip()
                continue

            if event_type == "event_msg" and payload_type in ("task_started", "task_complete"):
                timing_thread = self._thread_id(event, payload) or timing_thread_id or session_thread_id
                timing_turn = payload.get("turn_id")
                if timing_thread and timing_turn:
                    timing_key = (timing_thread, str(timing_turn))
                    reported_ttft = _as_float(_first(
                        payload, "time_to_first_token_ms", "ttft_ms", "first_token_latency_ms"))
                    if reported_ttft is not None and reported_ttft > 0:
                        turn_ttft[timing_key] = reported_ttft
                    if payload_type == "task_started":
                        turn_starts[timing_key] = event
                        current_turn_id = str(timing_turn)
                    else:
                        duration = self._turn_duration(turn_starts.get(timing_key), event)
                        if duration is not None:
                            turn_timings[timing_key] = duration
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
                # Forked logs replay the parent's session_meta, then apply
                # the child's settings. Lifecycle events omit thread_id;
                # their owner is the active stream thread, not that parent.
                if thread_id:
                    timing_thread_id = thread_id
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
                record_turn = str(payload.get("turn_id") or state.get("turn_id") or current_turn_id or "") or None
                parent_value = (
                    parent_by_thread.get(thread_id or "")
                    or _first(payload, "parent_thread_id", "parentThreadId", "forked_from_id", "forkedFromId")
                )
                session_value = payload.get("session_id")
                if parent_value is None and session_value is not None and str(session_value).strip() != str(thread_id or ""):
                    parent_value = str(session_value).strip()
                usage_ttft = _as_float(_first(
                    payload, "time_to_first_token_ms", "ttft_ms", "first_token_latency_ms"))
                if usage_ttft is None:
                    usage_ttft = _as_float(_first(
                        _as_dict(usage_container),
                        "time_to_first_token_ms", "ttft_ms", "first_token_latency_ms"))
                if usage_ttft is None and thread_id and record_turn:
                    usage_ttft = turn_ttft.get((thread_id, record_turn))
                turn_usage = _as_dict(payload.get("turn_token_usage"))
                turn_output = _as_int(turn_usage.get("output_tokens"))
                if thread_id and record_turn and turn_output is not None:
                    turn_output_totals[(thread_id, record_turn)] = turn_output
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
                        record_turn,
                        ttft_ms=usage_ttft if usage_ttft and usage_ttft > 0 else None,
                        parent_thread_id=str(parent_value) if parent_value is not None else None,
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
                parent_value = (
                    parent_by_thread.get(thread_id or "")
                    or _first(payload, "parent_thread_id", "parentThreadId", "forked_from_id", "forkedFromId")
                )
                session_value = _first(payload, "session_id", "sessionId")
                if parent_value is None and session_value is not None and str(session_value).strip() != str(thread_id or ""):
                    parent_value = str(session_value).strip()
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
                            usage.cache_read_available,
                            usage.cache_write_available,
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
                        str(payload.get("turn_id") or current_turn_id or "") or None,
                        parent_thread_id=str(parent_value) if parent_value is not None else None,
                    )
                )
        # A whole turn includes waits and tools. Never invent API-request or
        # streaming timings from adjacent usage timestamps. Carry the same
        # sample on its requests; aggregation deduplicates the hashed turn id.
        # Mixed-attribution turns cannot be split into reliable model speeds.
        groups: Dict[Tuple[str, str], List[int]] = {}
        for index, record in enumerate(records):
            if not record.thread_id:
                records[index] = replace(record, speed_exclusion_reason="missing_thread_id")
            elif not record.turn_id:
                records[index] = replace(record, speed_exclusion_reason="missing_turn_id")
        for index, record in enumerate(records):
            if record.thread_id and record.turn_id:
                groups.setdefault((record.thread_id, record.turn_id), []).append(index)
        for key, indices in groups.items():
            duration = turn_timings.get(key)
            group_key = hashlib.sha256(json.dumps(key).encode("utf-8")).hexdigest()
            reason = None
            if len({(records[i].provider, records[i].model, records[i].workspace)
                    for i in indices}) != 1:
                reason = "mixed_attribution"
            elif any(not records[i].response_id for i in indices):
                reason = "missing_request_id"
            elif duration is None:
                reason = "missing_or_invalid_duration"
            unique: Dict[str, ParsedRecord] = {}
            # Require stable request ids to avoid overcounting duplicate usage.
            for index in indices:
                candidate = records[index]
                if not candidate.response_id:
                    continue
                prior = unique.get(candidate.response_id)
                if prior is None or (candidate.usage.total_tokens, candidate.timestamp) > (
                        prior.usage.total_tokens, prior.timestamp):
                    unique[candidate.response_id] = candidate
            output = sum(record.usage.output_tokens for record in unique.values())
            # When a cumulative turn counter is present, prove we saw all
            # output rather than silently computing from an incomplete copy.
            if reason is None and key in turn_output_totals and turn_output_totals[key] != output:
                reason = "inconsistent_turn_output"
            if reason is not None:
                for index in indices:
                    records[index] = replace(records[index], speed_group_key=group_key,
                                             speed_exclusion_reason=reason)
                continue
            sample_day = max(unique.values(), key=lambda record: record.timestamp).day
            for index in indices:
                if records[index].day == sample_day:
                    records[index] = replace(records[index], speed_group_key=group_key,
                                             speed_output_tokens=output, speed_duration_ms=duration,
                                             speed_exclusion_reason=None)
                else:
                    records[index] = replace(records[index], speed_group_key=group_key,
                                             speed_exclusion_reason="outside_sample_day")
        # A TTFT attached to a turn completion is one turn-level sample. Carry
        # it on one request only so aggregate counts remain stable.
        for key, indices in groups.items():
            reported = turn_ttft.get(key)
            if reported is None:
                candidates = [records[index].ttft_ms for index in indices if records[index].ttft_ms]
                reported = candidates[0] if candidates else None
            if reported is not None and reported > 0:
                first_index = min(indices, key=lambda i: records[i].timestamp)
                for index in indices:
                    records[index] = replace(records[index], ttft_ms=reported if index == first_index else None)
        return records, self._parse_errors

    @staticmethod
    def _turn_duration(start: Optional[Mapping[str, Any]], end: Mapping[str, Any]) -> Optional[float]:
        # Require the start marker so a truncated tail cannot masquerade as
        # all output for a completed turn. Never use filesystem timestamps.
        if start is None:
            return None
        payload = _as_dict(end.get("payload"))
        if "duration_ms" in payload:
            value = payload["duration_ms"]
            if isinstance(value, bool):
                return None
            try:
                duration = float(value)
            except (TypeError, ValueError, OverflowError):
                return None
            return duration if math.isfinite(duration) and duration > 0 else None
        start_payload = _as_dict(start.get("payload")) if start else {}
        first = _first(payload, "started_at", "started_at_ms")
        first_ms = payload.get("started_at") is None and payload.get("started_at_ms") is not None
        if first is None:
            first = _first(start_payload, "started_at", "started_at_ms")
            first_ms = start_payload.get("started_at") is None and start_payload.get("started_at_ms") is not None
        if first is None and start:
            first = start.get("timestamp")
        last = _first(payload, "completed_at", "completed_at_ms")
        last_ms = payload.get("completed_at") is None and payload.get("completed_at_ms") is not None
        if last is None:
            last = end.get("timestamp")
        def instant(value, milliseconds=False):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value) / (1000 if milliseconds else 1) if math.isfinite(value) else None
            if isinstance(value, str):
                try:
                    dt = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
                    return dt.timestamp() if dt.tzinfo is not None else None
                except (ValueError, OverflowError, OSError):
                    pass
            return None
        first, last = instant(first, first_ms), instant(last, last_ms)
        if first is None or last is None:
            return None
        duration = (last - first) * 1000
        return duration if math.isfinite(duration) and duration > 0 else None


class DashboardDB:
    """Durable local usage history and dashboard aggregations."""

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
        self.pricing = PricingCatalog()
        self.database_recovery: Optional[Dict[str, Any]] = None
        self.database_migration: Optional[Dict[str, Any]] = None
        self._query_lock = threading.RLock()
        self._dashboard_cache = {}
        self._data_version = 0
        self._lock = threading.RLock()
        self._connection = self._open_connection()
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            try:
                self._connection.execute("PRAGMA foreign_keys=ON")
                self._connection.execute("PRAGMA journal_mode=WAL")
                self._connection.execute("PRAGMA synchronous=FULL")
                self._create_schema()
            except Exception:
                self._connection.close()
                raise
        self._load_pricing_catalog()
        self._backfill_pricing_snapshots()

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
            self._maintain_storage(force=True)
            self._connection.close()

    @staticmethod
    def _json_object(value: Any) -> Dict[str, Any]:
        try:
            result = json.loads(value) if isinstance(value, str) else value
        except (ValueError, TypeError):
            result = None
        return result if isinstance(result, dict) else {}

    def _pricing_snapshot_id(self, snapshot: Mapping[str, Any]) -> int:
        encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode("utf-8")).digest()
        self._connection.execute(
            "INSERT OR IGNORE INTO pricing_snapshots(snapshot_hash,snapshot_json) VALUES (?,?)",
            (digest, encoded))
        return int(self._connection.execute(
            "SELECT id FROM pricing_snapshots WHERE snapshot_hash=?", (digest,)).fetchone()[0])

    def _stored_pricing_rules(self) -> List[Dict[str, Any]]:
        with self._lock:
            try:
                row = self._connection.execute("SELECT rules_json FROM pricing_config WHERE id=1").fetchone()
            except sqlite3.DatabaseError:
                row = None
        if not row:
            return []
        try:
            value = json.loads(row[0])
        except (ValueError, TypeError):
            return []
        return value if isinstance(value, list) else []

    def _load_pricing_catalog(self) -> None:
        self.pricing = PricingCatalog(rules=self._stored_pricing_rules())

    def _quote_for_usage(
        self, model: str, provider: str, timestamp: str,
        input_tokens: int, cached_input_tokens: int,
        cache_write_input_tokens: int, output_tokens: int,
        rate_snapshot: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        usage = {
            "timestamp": timestamp,
            "input_tokens": int(input_tokens or 0),
            "cached_input_tokens": int(cached_input_tokens or 0),
            "cache_write_input_tokens": int(cache_write_input_tokens or 0),
            "output_tokens": int(output_tokens or 0),
            "provider": provider,
        }
        quote = None
        if rate_snapshot:
            frozen = getattr(self.pricing, "quote_from_snapshot", None)
            if callable(frozen):
                quote = frozen(dict(rate_snapshot), model=model, **usage)
        if quote is None:
            try:
                quote = self.pricing.quote(model, **usage)
            except TypeError:
                usage.pop("provider", None)
                quote = self.pricing.quote(model, **usage)
        status = str(getattr(quote, "status", "unpriced"))
        rate = getattr(quote, "rate_snapshot", None)
        rate = dict(rate) if isinstance(rate, Mapping) else {}
        snapshot_status = str(rate.get("status") or status)
        is_priced = bool(getattr(quote, "is_priced", status in ("priced", "free")))
        if status == "unpriced" or snapshot_status == "unpriced":
            is_priced = False
        elif snapshot_status == "free":
            is_priced = True
        if not rate:
            rate = {
                "status": status,
                "pricing_model": getattr(quote, "pricing_model", None),
                "rate_band": getattr(quote, "rate_band", None),
                "source": getattr(quote, "source_url", None),
                "note": getattr(quote, "note", None),
            }
        multiplier = _as_float(getattr(quote, "cost_multiplier", 1.0))
        if multiplier is None:
            multiplier = 1.0
        before = getattr(quote, "cost_before_discount_usd", None)
        after = getattr(quote, "cost_after_discount_usd", None)
        if before is None and is_priced:
            before = getattr(quote, "estimated_cost_usd", None)
        if after is None and is_priced:
            after = getattr(quote, "estimated_cost_usd", None)
        before = _as_float(before) if is_priced else None
        after = _as_float(after) if is_priced else None
        breakdown = getattr(quote, "cost_breakdown_before_discount", None)
        if not isinstance(breakdown, Mapping):
            breakdown = {}
        saving_before = getattr(quote, "cache_savings_before_discount_usd", None)
        saving_after = getattr(quote, "cache_savings_after_discount_usd", None)
        return {
            "pricing_status": snapshot_status if is_priced else "unpriced",
            "rate_snapshot": dict(rate),
            "cost_multiplier": multiplier,
            "cost_before_discount_usd": before,
            "cost_after_discount_usd": after,
            "cost_breakdown_before_discount": dict(breakdown),
            "cache_savings_before_discount_usd": _as_float(saving_before),
            "cache_savings_after_discount_usd": _as_float(saving_after),
        }

    def _backfill_pricing_snapshots(self) -> None:
        with self._lock:
            rows = self._connection.execute("""SELECT s.*, c.provider, c.model
                FROM history_requests r JOIN history_snapshots s ON s.id=r.snapshot_id
                JOIN history_contexts c ON c.id=s.context_id
                WHERE s.rate_snapshot_id IS NULL AND (s.rate_snapshot='{}' OR s.rate_snapshot='')""").fetchall()
            if not rows:
                return
            self._connection.execute("BEGIN")
            try:
                for row in rows:
                    quote = self._quote_for_usage(
                        row["model"], row["provider"], row["timestamp"], row["input_tokens"],
                        row["cached_input_tokens"], row["cache_write_input_tokens"], row["output_tokens"])
                    self._save_quote_on_snapshot(row["id"], quote)
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def _save_quote_on_snapshot(self, snapshot_id: int, quote: Mapping[str, Any]) -> None:
        rate_snapshot_id = self._pricing_snapshot_id(quote["rate_snapshot"])
        self._connection.execute("""UPDATE history_snapshots SET pricing_status=?, rate_snapshot=?,
            rate_snapshot_id=?, cost_multiplier=?, cost_before_discount_usd=?, cost_after_discount_usd=?,
            cost_breakdown_before_discount=?, cache_savings_before_discount_usd=?,
            cache_savings_after_discount_usd=? WHERE id=?""", (
            quote["pricing_status"], "{}", rate_snapshot_id,
            quote["cost_multiplier"], quote["cost_before_discount_usd"],
            quote["cost_after_discount_usd"], json.dumps(quote["cost_breakdown_before_discount"],
                ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            quote["cache_savings_before_discount_usd"], quote["cache_savings_after_discount_usd"],
            snapshot_id))

    @staticmethod
    def _validate_cleanup_days(days: Any) -> int:
        if type(days) is not int or not 1 <= days <= 36500:
            raise ValueError("清理天数必须是 1 到 36500 之间的整数")
        return days

    def _cleanup_meta_locked(self) -> Tuple[int, Optional[_dt.date]]:
        values = {row["key"]: row["value"] for row in self._connection.execute(
            "SELECT key, value FROM history_meta WHERE key IN ('cleanup_days','cleanup_before')")}
        cleanup_days = 30
        try:
            stored_days = int(values.get("cleanup_days", "30"))
            if 1 <= stored_days <= 36500:
                cleanup_days = stored_days
        except (TypeError, ValueError):
            pass
        raw_before = values.get("cleanup_before")
        if not raw_before:
            return cleanup_days, None
        try:
            cleanup_before = _dt.date.fromisoformat(raw_before)
        except (TypeError, ValueError) as error:
            raise sqlite3.DatabaseError("数据库中的清理边界日期无效") from error
        if cleanup_before.isoformat() != raw_before:
            raise sqlite3.DatabaseError("数据库中的清理边界日期无效")
        return cleanup_days, cleanup_before

    def _effective_cleanup_cutoff_locked(self, days: int) -> _dt.date:
        _stored_days, previous = self._cleanup_meta_locked()
        requested = self._local_now().date() - _dt.timedelta(days=days)
        return max(requested, previous) if previous else requested

    @staticmethod
    def _database_size(db_path: Path) -> int:
        size = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                size += Path(str(db_path) + suffix).stat().st_size
            except FileNotFoundError:
                pass
        return size

    def _data_settings_locked(self) -> Dict[str, Any]:
        cleanup_days, cleanup_before = self._cleanup_meta_locked()
        row = self._connection.execute("""SELECT COUNT(*) AS request_count,
                MIN(s.day) AS earliest_day, MAX(s.day) AS latest_day
            FROM history_requests r JOIN history_snapshots s ON s.id=r.snapshot_id""").fetchone()
        return {
            "database_path": str(self.db_path),
            "directory": str(self.db_path.parent),
            "database_bytes": self._database_size(self.db_path),
            "request_count": int(row["request_count"]),
            "earliest_day": row["earliest_day"],
            "latest_day": row["latest_day"],
            "cleanup_days": cleanup_days,
            "cleanup_before": cleanup_before.isoformat() if cleanup_before else None,
        }

    def data_settings(self) -> Dict[str, Any]:
        with self._lock:
            return self._data_settings_locked()

    def _cleanup_preview_locked(self, days: int, cutoff: _dt.date) -> Dict[str, Any]:
        cutoff_day = cutoff.isoformat()
        snapshots = self._connection.execute(
            "SELECT COUNT(*) FROM history_snapshots WHERE day < ?", (cutoff_day,)
        ).fetchone()[0]
        requests = self._connection.execute("""WITH candidates(logical_key, snapshot_id) AS (
                SELECT logical_key, snapshot_id FROM history_requests
                UNION SELECT logical_key, retained_snapshot_id FROM history_requests
                    WHERE retained_snapshot_id IS NOT NULL
                UNION SELECT logical_key, snapshot_id FROM history_sources
            )
            SELECT COUNT(*) FROM history_requests r
            WHERE EXISTS (
                SELECT 1 FROM candidates c JOIN history_snapshots s ON s.id=c.snapshot_id
                WHERE c.logical_key=r.logical_key AND s.day < ?
            ) AND NOT EXISTS (
                SELECT 1 FROM candidates c JOIN history_snapshots s ON s.id=c.snapshot_id
                WHERE c.logical_key=r.logical_key AND s.day >= ?
            )""", (cutoff_day, cutoff_day)).fetchone()[0]
        return {
            "days": days,
            "cutoff_day": cutoff_day,
            "request_count": int(requests),
            "snapshot_count": int(snapshots),
        }

    def preview_cleanup(self, days: int) -> Dict[str, Any]:
        days = self._validate_cleanup_days(days)
        with self._lock:
            cutoff = self._effective_cleanup_cutoff_locked(days)
            return self._cleanup_preview_locked(days, cutoff)

    def _cleanup_backup_locked(self) -> Path:
        backup_directory = self.db_path.parent / "backups"
        backup_directory.mkdir(parents=True, exist_ok=True)
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup_path = backup_directory / ("usage-cleanup-%s-%s.sqlite3" % (stamp, uuid.uuid4().hex))
        target = None
        try:
            target = sqlite3.connect(str(backup_path), timeout=5.0)
            self._connection.backup(target)
            check = target.execute("PRAGMA quick_check(1)").fetchone()
            if not check or str(check[0]).casefold() != "ok":
                raise sqlite3.DatabaseError("SQLite 备份未通过完整性检查")
            target.close()
            target = None
            return backup_path
        except Exception:
            if target is not None:
                try:
                    target.close()
                except sqlite3.Error:
                    pass
            for suffix in ("", "-wal", "-shm"):
                try:
                    Path(str(backup_path) + suffix).unlink()
                except OSError:
                    pass
            raise

    def cleanup_history(self, days: int, expected_cutoff: Optional[str] = None) -> Dict[str, Any]:
        days = self._validate_cleanup_days(days)
        if expected_cutoff is not None:
            if not isinstance(expected_cutoff, str):
                raise ValueError("预览截止日期必须是 ISO 日期")
            try:
                parsed_expected = _dt.date.fromisoformat(expected_cutoff)
            except ValueError as error:
                raise ValueError("预览截止日期必须是 ISO 日期") from error
            if parsed_expected.isoformat() != expected_cutoff:
                raise ValueError("预览截止日期必须是 ISO 日期")

        # Dashboard reads take _query_lock before they open a reader; scanner
        # writes take only _lock.  This order blocks cache fills during a
        # destructive change without introducing a lock inversion.
        with self._query_lock:
            with self._lock:
                cutoff = self._effective_cleanup_cutoff_locked(days)
                cutoff_day = cutoff.isoformat()
                if expected_cutoff is not None and expected_cutoff != cutoff_day:
                    raise ValueError("清理截止日期已变化，请重新预览")

                # A verified online copy exists before any deletion or marker
                # change.  If copying/checking fails, the live DB is untouched.
                backup_path = self._cleanup_backup_locked()
                before_requests = int(self._connection.execute(
                    "SELECT COUNT(*) FROM history_requests").fetchone()[0])
                before_snapshots = int(self._connection.execute(
                    "SELECT COUNT(*) FROM history_snapshots WHERE day < ?", (cutoff_day,)
                ).fetchone()[0])
                try:
                    self._connection.execute("BEGIN")
                    affected = {row[0] for row in self._connection.execute("""SELECT logical_key
                        FROM history_sources src JOIN history_snapshots s ON s.id=src.snapshot_id
                        WHERE s.day < ?
                        UNION SELECT logical_key FROM history_requests r
                            JOIN history_snapshots s ON s.id=r.snapshot_id WHERE s.day < ?
                        UNION SELECT logical_key FROM history_requests r
                            JOIN history_snapshots s ON s.id=r.retained_snapshot_id WHERE s.day < ?""",
                        (cutoff_day, cutoff_day, cutoff_day)).fetchall()}
                    self._connection.execute("""DELETE FROM history_sources WHERE snapshot_id IN (
                        SELECT id FROM history_snapshots WHERE day < ?)""", (cutoff_day,))
                    self._connection.execute("""UPDATE history_requests SET retained_snapshot_id=NULL
                        WHERE retained_snapshot_id IN (
                            SELECT id FROM history_snapshots WHERE day < ?)""", (cutoff_day,))
                    # Preserve a surviving current winner if a damaged or old
                    # ledger has no source/retained reference for that row.
                    self._connection.execute("""UPDATE history_requests SET retained_snapshot_id=snapshot_id
                        WHERE retained_snapshot_id IS NULL
                        AND logical_key IN (SELECT logical_key FROM history_requests
                            WHERE snapshot_id IN (SELECT id FROM history_snapshots WHERE day < ?))
                        AND snapshot_id IN (SELECT id FROM history_snapshots WHERE day >= ?)
                        AND NOT EXISTS (SELECT 1 FROM history_sources src
                            WHERE src.logical_key=history_requests.logical_key
                            AND src.snapshot_id=history_requests.snapshot_id)""", (cutoff_day, cutoff_day))
                    self._rebuild_logical_records(affected)
                    self._connection.execute("DELETE FROM history_snapshots WHERE day < ?", (cutoff_day,))
                    self._collect_unused_snapshots()
                    self._connection.execute("""UPDATE source_files SET record_count=(
                        SELECT COUNT(*) FROM history_sources WHERE history_sources.file_id=source_files.id)""")
                    self._connection.execute("INSERT OR REPLACE INTO history_meta(key,value) VALUES (?,?)",
                                             ("cleanup_before", cutoff_day))
                    self._connection.execute("INSERT OR REPLACE INTO history_meta(key,value) VALUES (?,?)",
                                             ("cleanup_days", str(days)))
                    self._connection.commit()
                except Exception:
                    self._connection.rollback()
                    raise

                # Cleanup is committed and recoverable from backup_path even
                # if physical compaction fails; report that limitation without
                # pretending the logical cleanup failed.
                space_reclaimed = True
                try:
                    self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    self._connection.execute("VACUUM")
                    checkpoint = self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                    if checkpoint and checkpoint[0]:
                        space_reclaimed = False
                    self._last_maintenance = time.monotonic()
                except sqlite3.DatabaseError:
                    space_reclaimed = False
                after_requests = int(self._connection.execute(
                    "SELECT COUNT(*) FROM history_requests").fetchone()[0])
                after_old_snapshots = int(self._connection.execute(
                    "SELECT COUNT(*) FROM history_snapshots WHERE day < ?", (cutoff_day,)
                ).fetchone()[0])
                self._data_version += 1
                self._dashboard_cache.clear()
                settings = self._data_settings_locked()
                result = {
                    "ok": True,
                    "cutoff_day": cutoff_day,
                    "deleted_requests": max(0, before_requests - after_requests),
                    "deleted_snapshots": max(0, before_snapshots - after_old_snapshots),
                    "backup_path": str(backup_path),
                    "settings": settings,
                }
                if not space_reclaimed:
                    result["space_reclaimed"] = False
                return result

    def relocate_database(
        self,
        directory: Path | str,
        persist: Optional[Callable[[str], Any]] = None,
    ) -> Dict[str, Any]:
        if not isinstance(directory, (Path, str)):
            raise ValueError("保存目录必须是绝对路径")
        requested_directory = Path(directory).expanduser()
        if not requested_directory.is_absolute():
            raise ValueError("保存目录必须是绝对路径")
        try:
            destination_directory = requested_directory.resolve(strict=True)
        except OSError as error:
            raise ValueError("保存目录不存在或无法访问") from error
        if not destination_directory.is_dir():
            raise ValueError("保存路径必须是已存在的文件夹")
        destination = destination_directory / "usage.sqlite3"

        # Check the selected folder is writable before locking the live store.
        probe = destination_directory / (".codex-token-dashboard-write-test-%s.tmp" % uuid.uuid4().hex)
        try:
            descriptor = os.open(str(probe), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
            probe.unlink()
        except OSError as error:
            try:
                probe.unlink()
            except OSError:
                pass
            raise ValueError("保存目录不可写") from error

        with self._query_lock:
            with self._lock:
                previous_path = self.db_path
                current_settings = self._data_settings_locked()
                current_path = previous_path.resolve()
                try:
                    requested_target = destination.resolve(strict=False)
                except OSError as error:
                    raise ValueError("数据库目标路径无效") from error
                if requested_target == current_path:
                    return {"ok": True, "settings": current_settings,
                            "previous_path": str(previous_path)}
                if destination.exists() or destination.is_symlink():
                    raise FileExistsError("目标文件夹中已存在 usage.sqlite3，拒绝覆盖")

                token = uuid.uuid4().hex
                temporary = destination_directory / (".usage.sqlite3-relocate-%s.tmp" % token)
                new_connection: Optional[sqlite3.Connection] = None
                final_created = False
                try:
                    target = sqlite3.connect(str(temporary), timeout=5.0)
                    try:
                        self._connection.backup(target)
                        check = target.execute("PRAGMA quick_check(1)").fetchone()
                        if not check or str(check[0]).casefold() != "ok":
                            raise sqlite3.DatabaseError("迁移后的数据库未通过完整性检查")
                    finally:
                        target.close()

                    # Reserve the destination without overwriting a database
                    # that appeared after the initial check, then atomically
                    # replace the empty reservation with the verified copy.
                    descriptor = os.open(str(destination), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    final_created = True
                    os.close(descriptor)
                    os.replace(str(temporary), str(destination))
                    new_connection = sqlite3.connect(str(destination), timeout=5.0, check_same_thread=False)
                    new_connection.row_factory = sqlite3.Row
                    new_connection.execute("PRAGMA foreign_keys=ON")
                    check = new_connection.execute("PRAGMA quick_check(1)").fetchone()
                    if not check or str(check[0]).casefold() != "ok":
                        raise sqlite3.DatabaseError("迁移后的数据库未通过完整性检查")
                    new_connection.execute("PRAGMA journal_mode=WAL")
                    new_connection.execute("PRAGMA synchronous=FULL")
                    new_settings = dict(current_settings)
                    new_settings["database_path"] = str(destination)
                    new_settings["directory"] = str(destination_directory)
                    new_settings["database_bytes"] = self._database_size(destination)
                    if persist is not None:
                        persisted = persist(str(destination))
                        if persisted is False:
                            raise RuntimeError("未能保存数据库位置设置")

                    old_connection = self._connection
                    self.db_path = destination
                    self._connection = new_connection
                    new_connection = None
                    self._data_version += 1
                    self._dashboard_cache.clear()
                    try:
                        old_connection.close()
                    except sqlite3.Error:
                        pass
                    return {"ok": True, "settings": new_settings,
                            "previous_path": str(previous_path)}
                except Exception:
                    if new_connection is not None:
                        try:
                            new_connection.close()
                        except sqlite3.Error:
                            pass
                    cleanup_paths = [temporary]
                    if final_created:
                        cleanup_paths.append(destination)
                    for path in cleanup_paths:
                        for suffix in ("", "-wal", "-shm"):
                            try:
                                Path(str(path) + suffix).unlink()
                            except OSError:
                                pass
                    raise

    def _create_schema(self) -> None:
        # The history is authoritative, not a disposable copy of source files.
        # A request points at one coherent snapshot; source references share it.
        tables = {row[0] for row in self._connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        migrating = "usage_records" in tables
        timing_upgrade = ("history_snapshots" in tables and "speed_group_key" not in
                          {row[1] for row in self._connection.execute("PRAGMA table_info(history_snapshots)")})
        snapshot_columns = ({row[1] for row in self._connection.execute(
            "PRAGMA table_info(history_snapshots)")} if "history_snapshots" in tables else set())
        analytics_columns = {
            "cache_read_available", "cache_write_available", "ttft_ms", "thread_key",
            "parent_thread_key", "task_id", "agent_role", "speed_exclusion_reason",
            "pricing_status", "rate_snapshot", "cost_multiplier", "cost_before_discount_usd",
            "cost_after_discount_usd", "cost_breakdown_before_discount",
            "cache_savings_before_discount_usd", "cache_savings_after_discount_usd",
            "rate_snapshot_id",
        }
        analytics_upgrade = bool(tables.intersection({"history_snapshots", "usage_records", "source_files"})) \
            and not analytics_columns.issubset(snapshot_columns)
        analytics_backup = None
        if analytics_upgrade:
            analytics_backup = Path(str(self.db_path) + ".pre-analytics.bak")
            if not analytics_backup.exists():
                target = sqlite3.connect(str(analytics_backup))
                try:
                    self._connection.backup(target)
                finally:
                    target.close()
        if timing_upgrade:
            timing_backup = Path(str(self.db_path) + ".pre-speed.bak")
            if not timing_backup.exists():
                target = sqlite3.connect(str(timing_backup))
                try:
                    self._connection.backup(target)
                finally:
                    target.close()
        backup = None
        if migrating:
            # The analytics backup already captures the exact pre-migration
            # database. Avoid creating a second full copy for legacy-ledger
            # migrations; keep the historical backup name for upgrades that
            # do not need the analytics schema backup.
            backup = analytics_backup or Path(str(self.db_path) + ".pre-history.bak")
            if not backup.exists():
                target = sqlite3.connect(str(backup))
                try:
                    self._connection.backup(target)
                finally:
                    target.close()
        try:
            self._connection.execute("BEGIN")
            if migrating:
                for table in ("source_files", "usage_records", "usage_sources"):
                    if table in tables:
                        self._connection.execute(f"ALTER TABLE {table} RENAME TO legacy_{table}")
            statements = [
                """CREATE TABLE IF NOT EXISTS history_meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID""",
                """CREATE TABLE IF NOT EXISTS source_files (
                    id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE,
                    mtime_ns INTEGER NOT NULL, size INTEGER NOT NULL,
                    scanned_at TEXT NOT NULL, record_count INTEGER NOT NULL DEFAULT 0,
                    timezone TEXT NOT NULL DEFAULT '', parser_version INTEGER NOT NULL DEFAULT 0,
                    usage_format TEXT NOT NULL DEFAULT '')""",
                """CREATE TABLE IF NOT EXISTS history_contexts (
                    id INTEGER PRIMARY KEY, provider TEXT NOT NULL, model TEXT NOT NULL,
                    workspace TEXT NOT NULL, UNIQUE(provider, model, workspace))""",
                """CREATE TABLE IF NOT EXISTS pricing_snapshots (
                    id INTEGER PRIMARY KEY, snapshot_hash BLOB NOT NULL UNIQUE,
                    snapshot_json TEXT NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS history_snapshots (
                    id INTEGER PRIMARY KEY, fingerprint BLOB NOT NULL UNIQUE,
                    context_id INTEGER NOT NULL REFERENCES history_contexts(id), timestamp TEXT NOT NULL,
                    day TEXT NOT NULL, hour INTEGER NOT NULL,
                    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
                    cached_input_tokens INTEGER NOT NULL, cache_write_input_tokens INTEGER NOT NULL,
                    reasoning_output_tokens INTEGER NOT NULL, total_tokens INTEGER NOT NULL,
                    speed_group_key TEXT, speed_output_tokens INTEGER NOT NULL DEFAULT 0,
                    speed_duration_ms REAL NOT NULL DEFAULT 0,
                    cache_read_available INTEGER NOT NULL DEFAULT 0,
                    cache_write_available INTEGER NOT NULL DEFAULT 0,
                    ttft_ms REAL, thread_key TEXT, parent_thread_key TEXT, task_id TEXT,
                    agent_role TEXT NOT NULL DEFAULT 'unknown', speed_exclusion_reason TEXT,
                    pricing_status TEXT NOT NULL DEFAULT 'unpriced',
                    rate_snapshot TEXT NOT NULL DEFAULT '{}', rate_snapshot_id INTEGER,
                    cost_multiplier REAL NOT NULL DEFAULT 1,
                    cost_before_discount_usd REAL, cost_after_discount_usd REAL,
                    cost_breakdown_before_discount TEXT NOT NULL DEFAULT '{}',
                    cache_savings_before_discount_usd REAL,
                    cache_savings_after_discount_usd REAL)""",
                """CREATE TABLE IF NOT EXISTS history_requests (
                    logical_key BLOB PRIMARY KEY, snapshot_id INTEGER NOT NULL REFERENCES history_snapshots(id),
                    retained_snapshot_id INTEGER REFERENCES history_snapshots(id)) WITHOUT ROWID""",
                """CREATE TABLE IF NOT EXISTS history_sources (
                    file_id INTEGER NOT NULL REFERENCES source_files(id), logical_key BLOB NOT NULL,
                    snapshot_id INTEGER NOT NULL REFERENCES history_snapshots(id), source_line INTEGER NOT NULL,
                    PRIMARY KEY(file_id, logical_key)) WITHOUT ROWID""",
                "CREATE INDEX IF NOT EXISTS idx_history_logical ON history_sources(logical_key)",
                "CREATE INDEX IF NOT EXISTS idx_history_day ON history_snapshots(day)",
                "CREATE INDEX IF NOT EXISTS idx_history_task ON history_snapshots(task_id)",
                "CREATE INDEX IF NOT EXISTS idx_history_thread ON history_snapshots(thread_key)",
                """CREATE TABLE IF NOT EXISTS thread_relationships (
                    thread_key TEXT PRIMARY KEY, parent_thread_key TEXT,
                    agent_role TEXT NOT NULL DEFAULT 'main', updated_at TEXT NOT NULL) WITHOUT ROWID""",
                """CREATE TABLE IF NOT EXISTS pricing_config (
                    id INTEGER PRIMARY KEY CHECK (id=1), rules_json TEXT NOT NULL DEFAULT '[]',
                    rules_hash TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS reprice_previews (
                    token TEXT PRIMARY KEY, filters_json TEXT NOT NULL, rules_hash TEXT NOT NULL,
                    data_hash TEXT NOT NULL, request_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL, expires_at TEXT NOT NULL) WITHOUT ROWID""",
                """CREATE VIEW IF NOT EXISTS usage_records AS
                    SELECT r.logical_key AS record_key, lower(hex(r.logical_key)) AS request_id,
                        '' AS source_path, 0 AS source_line, NULL AS response_id,
                        s.thread_key AS thread_id, s.timestamp, s.day, s.hour,
                        c.provider, c.provider AS provider_label, c.model, c.workspace,
                        s.input_tokens, s.output_tokens, s.cached_input_tokens,
                        s.cache_write_input_tokens, s.reasoning_output_tokens, s.total_tokens,
                        s.speed_group_key, s.speed_output_tokens, s.speed_duration_ms,
                        s.cache_read_available, s.cache_write_available, s.ttft_ms,
                        s.thread_key, s.parent_thread_key, s.task_id, s.agent_role,
                        s.speed_exclusion_reason, s.pricing_status,
                        COALESCE(p.snapshot_json,s.rate_snapshot) AS rate_snapshot,
                        s.cost_multiplier, s.cost_before_discount_usd, s.cost_after_discount_usd,
                        s.cost_breakdown_before_discount, s.cache_savings_before_discount_usd,
                        s.cache_savings_after_discount_usd
                    FROM history_requests r JOIN history_snapshots s ON s.id=r.snapshot_id
                    JOIN history_contexts c ON c.id=s.context_id
                    LEFT JOIN pricing_snapshots p ON p.id=s.rate_snapshot_id""",
            ]
            # Existing ledgers are preserved. Changed parser version makes
            # available logs rescan once, filling timing without clearing use.
            if timing_upgrade:
                self._connection.execute("ALTER TABLE history_snapshots ADD COLUMN speed_group_key TEXT")
                self._connection.execute("ALTER TABLE history_snapshots ADD COLUMN speed_output_tokens INTEGER NOT NULL DEFAULT 0")
                self._connection.execute("ALTER TABLE history_snapshots ADD COLUMN speed_duration_ms REAL NOT NULL DEFAULT 0")
            if "history_snapshots" in tables:
                migrations = {
                    "cache_read_available": "INTEGER NOT NULL DEFAULT 0",
                    "cache_write_available": "INTEGER NOT NULL DEFAULT 0",
                    "ttft_ms": "REAL",
                    "thread_key": "TEXT",
                    "parent_thread_key": "TEXT",
                    "task_id": "TEXT",
                    "agent_role": "TEXT NOT NULL DEFAULT 'unknown'",
                    "speed_exclusion_reason": "TEXT",
                    "pricing_status": "TEXT NOT NULL DEFAULT 'unpriced'",
                    "rate_snapshot": "TEXT NOT NULL DEFAULT '{}'",
                    "rate_snapshot_id": "INTEGER",
                    "cost_multiplier": "REAL NOT NULL DEFAULT 1",
                    "cost_before_discount_usd": "REAL",
                    "cost_after_discount_usd": "REAL",
                    "cost_breakdown_before_discount": "TEXT NOT NULL DEFAULT '{}'",
                    "cache_savings_before_discount_usd": "REAL",
                    "cache_savings_after_discount_usd": "REAL",
                }
                present = {row[1] for row in self._connection.execute(
                    "PRAGMA table_info(history_snapshots)")}
                for column, definition in migrations.items():
                    if column not in present:
                        self._connection.execute(
                            "ALTER TABLE history_snapshots ADD COLUMN %s %s" % (column, definition))
            self._connection.execute("DROP VIEW IF EXISTS usage_records")
            for statement in statements:
                self._connection.execute(statement)
            # Deduplicate immutable rate objects while keeping the full JSON
            # available through usage_records and the request APIs.
            if "history_snapshots" in tables:
                for snapshot_row in self._connection.execute("""SELECT id,rate_snapshot
                    FROM history_snapshots WHERE rate_snapshot_id IS NULL
                    AND rate_snapshot NOT IN ('', '{}')""").fetchall():
                    snapshot_data = self._json_object(snapshot_row["rate_snapshot"])
                    if not snapshot_data:
                        continue
                    rate_id = self._pricing_snapshot_id(snapshot_data)
                    self._connection.execute("""UPDATE history_snapshots
                        SET rate_snapshot='{}',rate_snapshot_id=? WHERE id=?""",
                        (rate_id, snapshot_row["id"]))
            if migrating:
                if "source_files" in tables:
                    for row in self._connection.execute("SELECT * FROM legacy_source_files").fetchall():
                        self._connection.execute("""INSERT INTO source_files
                            (path, mtime_ns, size, scanned_at, record_count, timezone, parser_version)
                            VALUES (?, ?, ?, ?, ?, ?, 0)""", (
                            row["path"], row["mtime_ns"], row["size"], row["scanned_at"],
                            row["record_count"], row["timezone"] if "timezone" in row.keys() else ""))
                keys = set()
                # Seed canonical rows even if an old per-source ledger is partial.
                for row in self._connection.execute("SELECT * FROM legacy_usage_records").fetchall():
                    key = self._history_key(row)
                    snapshot = self._store_snapshot(key, row)
                    self._retain_snapshot(key, snapshot)
                    file_row = self._connection.execute(
                        "SELECT id FROM source_files WHERE path=?", (row["source_path"],)).fetchone()
                    if file_row:
                        self._store_source(file_row[0], key, snapshot, row["source_line"])
                    keys.add(key)
                if "usage_sources" in tables:
                    for row in self._connection.execute("SELECT * FROM legacy_usage_sources").fetchall():
                        key = self._history_key(row)
                        snapshot = self._store_snapshot(key, row)
                        file_row = self._connection.execute(
                            "SELECT id FROM source_files WHERE path=?", (row["source_path"],)).fetchone()
                        if file_row:
                            self._store_source(file_row[0], key, snapshot, row["source_line"])
                        else:
                            self._retain_snapshot(key, snapshot)
                        keys.add(key)
                # Legacy synthetic response ids identify cumulative-format files.
                for old_table in ("legacy_usage_records", "legacy_usage_sources"):
                    if old_table.removeprefix("legacy_") not in tables:
                        continue
                    for row in self._connection.execute(
                            f"SELECT source_path, response_id FROM {old_table}"):
                        self._connection.execute("UPDATE source_files SET usage_format=? WHERE path=?",
                            ("legacy" if str(row["response_id"] or "").startswith("legacy:") else "modern",
                             row["source_path"]))
                self._connection.execute("""UPDATE history_requests SET retained_snapshot_id=NULL
                    WHERE EXISTS (SELECT 1 FROM history_sources src
                        WHERE src.logical_key=history_requests.logical_key
                        AND src.snapshot_id=history_requests.retained_snapshot_id)""")
                self._rebuild_logical_records(keys)
                for table in ("usage_records", "usage_sources", "source_files"):
                    if table in tables:
                        self._connection.execute(f"DROP TABLE legacy_{table}")
                self.database_migration = {"status": "durable_history", "backup": str(backup),
                                           "records": len(keys)}
            stored_timezone = self._connection.execute(
                "SELECT value FROM history_meta WHERE key='timezone'").fetchone()
            if not stored_timezone or stored_timezone[0] != self.timezone_name:
                # Reproject retained timestamps too, even after all logs are gone.
                for row in self._connection.execute("SELECT id, timestamp FROM history_snapshots").fetchall():
                    _, day, hour = _parse_timestamp(row["timestamp"], timezone=self.timezone)
                    self._connection.execute("UPDATE history_snapshots SET day=?, hour=? WHERE id=?",
                                             (day, hour, row["id"]))
                self._connection.execute("INSERT OR REPLACE INTO history_meta VALUES ('timezone', ?)",
                                         (self.timezone_name,))
            self._collect_unused_snapshots()
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        # One-time physical compaction also enables automatic page reclamation.
        if self._connection.execute("PRAGMA auto_vacuum").fetchone()[0] != 2:
            self._connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
            self._connection.execute("VACUUM")
        self._connection.execute("PRAGMA journal_size_limit=1048576")
        self._connection.execute("PRAGMA wal_autocheckpoint=256")
        self._last_maintenance = time.monotonic()
        self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    @staticmethod
    def _logical_key(thread_id, response_id, source_path, source_line) -> str:
        if response_id:
            identity = "response\0%s\0%s" % (thread_id or "", response_id)
        else:
            identity = "source\0%s\0%d" % (source_path, source_line)
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def _history_key(self, row) -> bytes:
        if row["response_id"]:
            return bytes.fromhex(self._logical_key(row["thread_id"], row["response_id"],
                                                   row["source_path"], row["source_line"]))
        # Old telemetry without request ids has no perfect identity. Use the
        # session (or original filename), timestamp and usage rather than the
        # absolute location so moving an identical log cannot count it again.
        values = [row["thread_id"] or Path(row["source_path"]).name, row["timestamp"],
                  row["provider"], row["model"], row["workspace"]]
        values.extend(row[key] for key in self._token_columns())
        return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode("utf-8")).digest()

    @staticmethod
    def _token_columns():
        return ("input_tokens", "output_tokens", "cached_input_tokens",
                "cache_write_input_tokens", "reasoning_output_tokens", "total_tokens")

    @staticmethod
    def _snapshot_rank(row):
        return (row["total_tokens"], sum(bool(row[key]) for key in
                ("cached_input_tokens", "cache_write_input_tokens", "reasoning_output_tokens")),
                sum(row[key] not in (None, "", "unknown", "Unknown") for key in
                    ("model", "provider", "workspace")),
                bool(row["speed_group_key"]) if "speed_group_key" in row.keys() else False,
                row["timestamp"],
                row["source_line"] if "source_line" in row.keys() else 0,
                row["source_path"] if "source_path" in row.keys() else "")

    def _store_snapshot(self, key, row, force_reprice=False):
        context = (row["provider"], row["model"], row["workspace"])
        self._connection.execute("INSERT OR IGNORE INTO history_contexts(provider,model,workspace) VALUES (?,?,?)",
                                 context)
        context_id = self._connection.execute(
            "SELECT id FROM history_contexts WHERE provider=? AND model=? AND workspace=?", context).fetchone()[0]
        tokens = tuple(int(row[name]) for name in self._token_columns())
        columns = set(row.keys())
        speed = (row["speed_group_key"], row["speed_output_tokens"], row["speed_duration_ms"]) \
            if "speed_group_key" in columns else (None, 0, 0.0)
        raw_thread = row["thread_id"] if "thread_id" in columns else None
        raw_parent = row["parent_thread_id"] if "parent_thread_id" in columns else None
        thread_key = row["thread_key"] if "thread_key" in columns else None
        parent_thread_key = row["parent_thread_key"] if "parent_thread_key" in columns else None
        if thread_key is None and raw_thread:
            thread_key = hashlib.sha256(str(raw_thread).encode("utf-8")).hexdigest()
        if parent_thread_key is None and raw_parent:
            parent_thread_key = hashlib.sha256(str(raw_parent).encode("utf-8")).hexdigest()
        if thread_key:
            agent_role = "subagent" if parent_thread_key else "main"
            task_id = row["task_id"] if "task_id" in columns and row["task_id"] else (parent_thread_key or thread_key)
            self._connection.execute("""INSERT INTO thread_relationships
                (thread_key,parent_thread_key,agent_role,updated_at) VALUES (?,?,?,?)
                ON CONFLICT(thread_key) DO UPDATE SET
                    parent_thread_key=COALESCE(excluded.parent_thread_key,thread_relationships.parent_thread_key),
                    agent_role=CASE WHEN excluded.agent_role='unknown' THEN thread_relationships.agent_role
                                    ELSE excluded.agent_role END,
                    updated_at=excluded.updated_at""",
                (thread_key, parent_thread_key, agent_role, _now_iso()))
        else:
            agent_role = "unknown"
            task_id = None
        cache_read_available = int(bool(row["cache_read_available"])) if "cache_read_available" in columns else 0
        cache_write_available = int(bool(row["cache_write_available"])) if "cache_write_available" in columns else 0
        ttft_ms = row["ttft_ms"] if "ttft_ms" in columns else None
        exclusion = row["speed_exclusion_reason"] if "speed_exclusion_reason" in columns else None
        previous = self._connection.execute("""SELECT s.*,
            COALESCE(p.snapshot_json,s.rate_snapshot) AS effective_rate_snapshot
            FROM history_requests r JOIN history_snapshots s ON s.id=r.snapshot_id
            LEFT JOIN pricing_snapshots p ON p.id=s.rate_snapshot_id
            WHERE r.logical_key=?""", (key,)).fetchone()
        frozen_snapshot = self._json_object(previous["effective_rate_snapshot"]) if previous else {}
        if force_reprice:
            frozen_snapshot = {}
        quote = self._quote_for_usage(
            row["model"], row["provider"], row["timestamp"], tokens[0], tokens[2], tokens[3], tokens[1],
            rate_snapshot=frozen_snapshot or None)
        pricing_fields = (
            quote["pricing_status"],
            json.dumps(quote["rate_snapshot"], ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            quote["cost_multiplier"], quote["cost_before_discount_usd"], quote["cost_after_discount_usd"],
            json.dumps(quote["cost_breakdown_before_discount"], ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")), quote["cache_savings_before_discount_usd"],
            quote["cache_savings_after_discount_usd"],
        )
        rate_snapshot_id = self._pricing_snapshot_id(quote["rate_snapshot"])
        fingerprint = hashlib.sha256(key + json.dumps(
            [*context, row["timestamp"], *tokens, *([*speed] if speed[0] else []),
             cache_read_available, cache_write_available, ttft_ms, thread_key, parent_thread_key,
             task_id, agent_role, exclusion, *pricing_fields], ensure_ascii=False, sort_keys=True,
             separators=(",", ":")).encode("utf-8")).digest()
        _, day, hour = _parse_timestamp(row["timestamp"], timezone=self.timezone)
        self._connection.execute("""INSERT OR IGNORE INTO history_snapshots
            (fingerprint,context_id,timestamp,day,hour,input_tokens,output_tokens,cached_input_tokens,
             cache_write_input_tokens,reasoning_output_tokens,total_tokens,
             speed_group_key,speed_output_tokens,speed_duration_ms,cache_read_available,
             cache_write_available,ttft_ms,thread_key,parent_thread_key,task_id,agent_role,
             speed_exclusion_reason,pricing_status,rate_snapshot,rate_snapshot_id,cost_multiplier,
             cost_before_discount_usd,cost_after_discount_usd,cost_breakdown_before_discount,
             cache_savings_before_discount_usd,cache_savings_after_discount_usd)
             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (fingerprint, context_id, row["timestamp"], day, hour, *tokens, *speed,
             cache_read_available, cache_write_available, ttft_ms, thread_key, parent_thread_key,
             task_id, agent_role, exclusion, pricing_fields[0], "{}", rate_snapshot_id,
             *pricing_fields[2:]))
        return self._connection.execute(
            "SELECT id FROM history_snapshots WHERE fingerprint=?", (fingerprint,)).fetchone()[0]

    def _snapshot(self, snapshot_id):
        return self._connection.execute("""SELECT s.*, c.provider,c.model,c.workspace
            FROM history_snapshots s JOIN history_contexts c ON c.id=s.context_id WHERE s.id=?""",
            (snapshot_id,)).fetchone()

    def _store_source(self, file_id, key, snapshot_id, line):
        previous = self._connection.execute("""SELECT snapshot_id, source_line FROM history_sources
            WHERE file_id=? AND logical_key=?""", (file_id, key)).fetchone()
        candidate = dict(self._snapshot(snapshot_id), source_line=line)
        if previous:
            prior = dict(self._snapshot(previous[0]), source_line=previous[1])
            if self._snapshot_rank(prior) > self._snapshot_rank(candidate):
                return
        self._connection.execute("INSERT OR REPLACE INTO history_sources VALUES (?,?,?,?)",
                                 (file_id, key, snapshot_id, line))

    def _retain_snapshot(self, key, snapshot_id):
        previous = self._connection.execute("SELECT retained_snapshot_id FROM history_requests WHERE logical_key=?",
                                            (key,)).fetchone()
        if previous and previous[0]:
            if self._snapshot_rank(self._snapshot(previous[0])) > self._snapshot_rank(self._snapshot(snapshot_id)):
                snapshot_id = previous[0]
        self._connection.execute("""INSERT INTO history_requests VALUES (?,?,?)
            ON CONFLICT(logical_key) DO UPDATE SET retained_snapshot_id=excluded.retained_snapshot_id""",
            (key, snapshot_id, snapshot_id))

    def _detach_source(self, file_id, keep_keys=(), retire_missing=False):
        # A missing request means source cleanup, not negative usage. Keep one
        # representative, not every deleted file, path, line or old snapshot.
        keys = set()
        for row in self._connection.execute("SELECT * FROM history_sources WHERE file_id=?", (file_id,)).fetchall():
            key = row["logical_key"]
            keys.add(key)
            if key not in keep_keys and not retire_missing:
                self._retain_snapshot(key, row["snapshot_id"])
        self._connection.execute("DELETE FROM history_sources WHERE file_id=?", (file_id,))
        return keys

    def _rebuild_logical_records(self, logical_keys) -> None:
        for key in set(logical_keys):
            candidates = self._connection.execute("""SELECT s.*, c.provider,c.model,c.workspace,
                refs.source_line, f.path AS source_path FROM history_sources refs
                JOIN history_snapshots s ON s.id=refs.snapshot_id
                JOIN history_contexts c ON c.id=s.context_id JOIN source_files f ON f.id=refs.file_id
                WHERE refs.logical_key=?""", (key,)).fetchall()
            retained = self._connection.execute(
                "SELECT retained_snapshot_id FROM history_requests WHERE logical_key=?", (key,)).fetchone()
            retained_id = retained[0] if retained else None
            if retained_id:
                candidates.append(self._snapshot(retained_id))
            if not candidates:
                self._connection.execute("DELETE FROM history_requests WHERE logical_key=?", (key,))
                continue
            winner = max(candidates, key=self._snapshot_rank)
            self._connection.execute("""INSERT INTO history_requests VALUES (?,?,?)
                ON CONFLICT(logical_key) DO UPDATE SET snapshot_id=excluded.snapshot_id""",
                (key, winner["id"], retained_id))

    def _collect_unused_snapshots(self):
        self._connection.execute("""DELETE FROM history_snapshots WHERE id NOT IN (
            SELECT snapshot_id FROM history_requests UNION SELECT retained_snapshot_id FROM history_requests
            WHERE retained_snapshot_id IS NOT NULL UNION SELECT snapshot_id FROM history_sources)""")
        self._connection.execute("DELETE FROM history_contexts WHERE id NOT IN (SELECT context_id FROM history_snapshots)")
        self._connection.execute("""DELETE FROM pricing_snapshots WHERE id NOT IN (
            SELECT rate_snapshot_id FROM history_snapshots WHERE rate_snapshot_id IS NOT NULL)""")

    def _maintain_storage(self, force=False):
        # Limit WAL and reclaim free pages without rewriting history each poll.
        if not force and time.monotonic() - self._last_maintenance < 60:
            return
        self._connection.execute("PRAGMA incremental_vacuum(256)").fetchall()
        self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self._last_maintenance = time.monotonic()

    def _files(self) -> List[Path]:
        found: Dict[str, Path] = {}
        self._complete_roots = []
        self._unavailable_roots = 0
        for root in self.roots:
            try:
                if not root.is_dir():
                    self._unavailable_roots += 1
                    # History no longer depends on file references, so a
                    # definitely deleted root can shed its scanning indexes.
                    try:
                        root.stat()
                    except FileNotFoundError:
                        self._complete_roots.append(root)
                    except OSError:
                        pass
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
        files = self._files()
        result.unavailable_roots = self._unavailable_roots
        result.files_seen = len(files)
        seen = {str(path) for path in files}
        with self._lock:
            existing = {row["path"]: row for row in self._connection.execute("SELECT * FROM source_files")}
            try:
                self._connection.execute("BEGIN")
                _cleanup_days, cleanup_before = self._cleanup_meta_locked()
                affected = set()
                for path in files:
                    key = str(path)
                    try:
                        stat = path.stat()
                    except OSError:
                        result.incomplete_files += 1
                        continue
                    signature = (stat.st_mtime_ns, stat.st_size, self.timezone_name, PARSER_SCHEMA_VERSION)
                    previous = existing.get(key)
                    if previous and tuple(previous[name] for name in
                            ("mtime_ns", "size", "timezone", "parser_version")) == signature:
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
                    # Never replace a previously good ledger from a damaged
                    # read. New files may contribute valid rows; retry errors.
                    if parse_errors and previous:
                        result.records_skipped += previous["record_count"]
                        continue
                    parsed = []
                    purged_records = 0
                    for record in records:
                        if cleanup_before and _dt.date.fromisoformat(record.day) < cleanup_before:
                            purged_records += 1
                            continue
                        row = dict(vars(record))
                        row.update(vars(record.usage))
                        parsed.append((self._history_key(row), row))
                    result.records_skipped += purged_records
                    new_keys = {logical for logical, _ in parsed}
                    usage_format = "modern" if self.parser._has_modern else "legacy"
                    if previous:
                        # Old cumulative telemetry superseded by modern usage
                        # must not survive as a second copy of the same calls.
                        retire = previous["usage_format"] == "legacy" and usage_format == "modern"
                        affected.update(self._detach_source(previous["id"], new_keys, retire))
                    self._connection.execute("""INSERT INTO source_files
                        (path,mtime_ns,size,scanned_at,record_count,timezone,parser_version,usage_format)
                        VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET
                        mtime_ns=excluded.mtime_ns,size=excluded.size,scanned_at=excluded.scanned_at,
                        record_count=excluded.record_count,timezone=excluded.timezone,
                        parser_version=excluded.parser_version,usage_format=excluded.usage_format""",
                        (key, signature[0], signature[1], _now_iso(), len(new_keys),
                         self.timezone_name, PARSER_SCHEMA_VERSION if not parse_errors else 0, usage_format))
                    file_id = self._connection.execute("SELECT id FROM source_files WHERE path=?", (key,)).fetchone()[0]
                    for logical, row in parsed:
                        self._store_source(file_id, logical, self._store_snapshot(logical, row), row["source_line"])
                    affected.update(new_keys)
                    result.records_added += len(new_keys)
                    result.records_skipped += len(parsed) - len(new_keys)
                for path, previous in existing.items():
                    if path not in seen and any(_is_within(Path(path), root) for root in self._complete_roots):
                        affected.update(self._detach_source(previous["id"]))
                        self._connection.execute("DELETE FROM source_files WHERE id=?", (previous["id"],))
                        result.files_removed += 1
                self._rebuild_logical_records(affected)
                if affected or result.files_removed:
                    self._collect_unused_snapshots()
                self._connection.commit()
                if result.files_scanned or result.files_removed:
                    self._data_version += 1
                self._maintain_storage()
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
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[sqlite3.Row]:
        where: List[str] = []
        params: List[Any] = []
        if start_date is not None or end_date is not None:
            if not start_date or not end_date:
                raise ValueError("start_date and end_date must be supplied together")
            start = self._parse_local_date(start_date)
            end = self._parse_local_date(end_date)
            if start > end:
                raise ValueError("start_date must not be after end_date")
            where.append("day >= ? AND day <= ?")
            params.extend((start.isoformat(), end.isoformat()))
        elif days is not None and days > 0:
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
            reader.create_function("canonical_model", 1, canonical_model)
            if model:
                where.append("canonical_model(model) = ?")
                params.append(canonical_model(model))
            predicate = (" WHERE " + " AND ".join(where)) if where else ""
            rows = reader.execute(
                "SELECT * FROM usage_records" + predicate + " ORDER BY day, hour, timestamp, request_id",
                params,
            ).fetchall()
        finally:
            reader.close()
        return rows

    @staticmethod
    def _parse_local_date(value: str) -> _dt.date:
        if not isinstance(value, str):
            raise ValueError("date must use YYYY-MM-DD format")
        try:
            parsed = _dt.date.fromisoformat(value)
        except ValueError:
            raise ValueError("date must use YYYY-MM-DD format") from None
        if parsed.isoformat() != value:
            raise ValueError("date must use YYYY-MM-DD format")
        return parsed

    def _thread_relationship_map(self) -> Dict[str, Optional[str]]:
        with self._lock:
            return {row["thread_key"]: row["parent_thread_key"] for row in
                    self._connection.execute("SELECT thread_key,parent_thread_key FROM thread_relationships")}

    @staticmethod
    def _root_thread_key(thread_key: Optional[str], relations: Mapping[str, Optional[str]]) -> Optional[str]:
        if not thread_key:
            return None
        current = str(thread_key)
        seen = set()
        while current in relations and relations[current] and current not in seen:
            seen.add(current)
            current = str(relations[current])
        return current

    def _filter_sql(
        self, *, days=0, provider=None, workspace=None, model=None,
        start_date=None, end_date=None, task_id=None,
    ) -> Tuple[str, List[Any]]:
        where: List[str] = []
        params: List[Any] = []
        if start_date is not None or end_date is not None:
            if not start_date or not end_date:
                raise ValueError("start_date and end_date must be supplied together")
            start, end = self._parse_local_date(start_date), self._parse_local_date(end_date)
            if start > end:
                raise ValueError("start_date must not be after end_date")
            where.append("day >= ? AND day <= ?")
            params.extend((start.isoformat(), end.isoformat()))
        elif days is not None and int(days) > 0:
            today = self._local_now().date()
            cutoff = today - _dt.timedelta(days=int(days) - 1)
            where.append("day >= ? AND day <= ?")
            params.extend((cutoff.isoformat(), today.isoformat()))
        providers = self.catalog.resolve(provider)
        if providers:
            where.append("provider IN (%s)" % ",".join("?" for _ in providers))
            params.extend(providers)
        if workspace:
            where.append("workspace = ?")
            params.append(workspace)
        if model:
            where.append("canonical_model(model) = ?")
            params.append(canonical_model(model))
        if task_id:
            relations = self._thread_relationship_map()
            task_root = str(task_id)
            members = {key for key in relations
                       if self._root_thread_key(key, relations) == task_root}
            members.add(task_root)
            if members:
                where.append("thread_key IN (%s)" % ",".join("?" for _ in members))
                params.extend(sorted(members))
            else:
                where.append("0=1")
        predicate = (" WHERE " + " AND ".join(where)) if where else ""
        return predicate, params

    def _query_filtered_rows(
        self, *, days=0, provider=None, workspace=None, model=None,
        start_date=None, end_date=None, task_id=None,
    ) -> List[sqlite3.Row]:
        predicate, params = self._filter_sql(days=days, provider=provider, workspace=workspace,
            model=model, start_date=start_date, end_date=end_date, task_id=task_id)
        reader = sqlite3.connect(str(self.db_path), timeout=5.0)
        try:
            reader.row_factory = sqlite3.Row
            reader.create_function("canonical_model", 1, canonical_model)
            return reader.execute(
                "SELECT * FROM usage_records" + predicate + " ORDER BY day,hour,timestamp,request_id",
                params).fetchall()
        finally:
            reader.close()

    def _request_dict(self, row: Mapping[str, Any], relations=None) -> Dict[str, Any]:
        relations = relations or self._thread_relationship_map()
        thread_key = row["thread_key"]
        parent_key = row["parent_thread_key"]
        task_id = self._root_thread_key(thread_key, relations) or row["task_id"]
        rate_value = (row["effective_rate_snapshot"] if "effective_rate_snapshot" in row.keys()
                      else row["rate_snapshot"])
        rate_snapshot = self._json_object(rate_value)
        breakdown = self._json_object(row["cost_breakdown_before_discount"])
        return {
            "request_id": str(row["request_id"]),
            "timestamp": str(row["timestamp"]),
            "provider": str(row["provider"]),
            "provider_label": self.catalog.label(row["provider"]),
            "model": str(row["model"]),
            "workspace": str(row["workspace"]),
            "input_tokens": int(row["input_tokens"] or 0),
            "output_tokens": int(row["output_tokens"] or 0),
            "cached_input_tokens": int(row["cached_input_tokens"] or 0),
            "cache_write_input_tokens": int(row["cache_write_input_tokens"] or 0),
            "fresh_input_tokens": _fresh_input_tokens(
                row["input_tokens"], row["cached_input_tokens"], row["cache_write_input_tokens"]),
            "fresh_input_tokens_estimated": not (
                bool(row["cache_read_available"]) and bool(row["cache_write_available"])),
            "reasoning_output_tokens": int(row["reasoning_output_tokens"] or 0),
            "total_tokens": int(row["total_tokens"] or 0),
            "cache_read_available": bool(row["cache_read_available"]),
            "cache_write_available": bool(row["cache_write_available"]),
            "task_id": task_id,
            "thread_key": thread_key,
            "parent_thread_key": parent_key,
            "agent_role": row["agent_role"] or "unknown",
            "cost_before_discount_usd": _as_float(row["cost_before_discount_usd"]),
            "cost_after_discount_usd": _as_float(row["cost_after_discount_usd"]),
            "pricing_status": str(row["pricing_status"] or "unpriced"),
            "rate_snapshot": rate_snapshot,
            "cost_multiplier": _as_float(row["cost_multiplier"]),
            "cost_breakdown_before_discount": breakdown,
            "cache_savings_before_discount_usd": _as_float(row["cache_savings_before_discount_usd"]),
            "cache_savings_after_discount_usd": _as_float(row["cache_savings_after_discount_usd"]),
            "ttft_ms": _as_float(row["ttft_ms"]),
            "speed_duration_ms": float(row["speed_duration_ms"] or 0),
            "speed_group_key": row["speed_group_key"],
        }

    def requests(
        self, *, days=30, provider=None, workspace=None, model=None, start_date=None,
        end_date=None, page=1, page_size=50, sort="time_desc", task_id=None, cost_mode="after",
    ) -> Dict[str, Any]:
        if type(page) is not int or page < 1:
            raise ValueError("page must be a positive integer")
        if type(page_size) is not int or not 1 <= page_size <= 500:
            raise ValueError("page_size must be between 1 and 500")
        if sort not in ("time_desc", "token_desc", "cost_desc"):
            raise ValueError("sort must be time_desc, token_desc, or cost_desc")
        if cost_mode not in ("before", "after"):
            raise ValueError("cost_mode must be before or after")
        offset = (page - 1) * page_size
        predicate, params = self._filter_sql(
            days=days, provider=provider, workspace=workspace, model=model,
            start_date=start_date, end_date=end_date, task_id=task_id)
        order = {
            "time_desc": "timestamp DESC, request_id DESC",
            "token_desc": "total_tokens DESC, timestamp DESC, request_id DESC",
            "cost_desc": "(cost_%s_discount_usd IS NULL) ASC, cost_%s_discount_usd DESC, timestamp DESC, request_id DESC" % (cost_mode, cost_mode),
        }[sort]
        reader = sqlite3.connect(str(self.db_path), timeout=5.0)
        try:
            reader.row_factory = sqlite3.Row
            reader.create_function("canonical_model", 1, canonical_model)
            total = int(reader.execute("SELECT COUNT(*) FROM usage_records" + predicate, params).fetchone()[0])
            selected = reader.execute("SELECT * FROM usage_records" + predicate +
                " ORDER BY " + order + " LIMIT ? OFFSET ?", [*params, page_size, offset]).fetchall()
        finally:
            reader.close()
        self.catalog.reload()
        relations = self._thread_relationship_map()
        return {
            "data": [self._request_dict(row, relations) for row in selected],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def request_detail(self, request_id: str) -> Optional[Dict[str, Any]]:
        if not isinstance(request_id, str) or len(request_id) != 64:
            return None
        try:
            key = bytes.fromhex(request_id)
        except ValueError:
            return None
        with self._lock:
            row = self._connection.execute("""SELECT lower(hex(r.logical_key)) AS request_id,
                s.*, COALESCE(p.snapshot_json,s.rate_snapshot) AS effective_rate_snapshot,
                c.provider,c.model,c.workspace FROM history_requests r
                JOIN history_snapshots s ON s.id=r.snapshot_id
                JOIN history_contexts c ON c.id=s.context_id
                LEFT JOIN pricing_snapshots p ON p.id=s.rate_snapshot_id
                WHERE r.logical_key=?""", (key,)).fetchone()
        if not row:
            return None
        self.catalog.reload()
        return self._request_dict(row)

    def tasks(
        self, *, days=30, provider=None, workspace=None, model=None, start_date=None, end_date=None,
    ) -> Dict[str, Any]:
        rows = self._query_filtered_rows(days=days, provider=provider, workspace=workspace,
                                         model=model, start_date=start_date, end_date=end_date)
        self.catalog.reload()
        relations = self._thread_relationship_map()
        grouped: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            thread_key = row["thread_key"]
            task_id = self._root_thread_key(thread_key, relations) or row["task_id"]
            if not task_id:
                continue
            task = grouped.setdefault(str(task_id), {
                "task_id": str(task_id), "workspace": str(row["workspace"]),
                "summary": self._empty_metrics(), "agents": {},
            })
            quote = self._stored_quote(row)
            self._add_metrics(task["summary"], row, quote)
            role = str(row["agent_role"] or ("subagent" if row["parent_thread_key"] else "main"))
            agent_key = (str(thread_key or "unknown"), str(row["model"]))
            agent = task["agents"].setdefault(agent_key, {
                "thread_key": thread_key, "parent_thread_key": row["parent_thread_key"],
                "agent_role": role, "model": str(row["model"]),
                "summary": self._empty_metrics(),
            })
            self._add_metrics(agent["summary"], row, quote)
            if role == "main":
                task["workspace"] = str(row["workspace"])
        output = []
        for task in grouped.values():
            agents = sorted(task["agents"].values(), key=lambda item: (
                item["agent_role"] != "main", str(item["thread_key"]), item["model"]))
            for agent in agents:
                agent["summary"] = self._metrics_aliases(agent["summary"])
            task["summary"] = self._metrics_aliases(task["summary"])
            task["agents"] = agents
            output.append(task)
        output.sort(key=lambda item: (-item["summary"]["total_tokens"], item["task_id"]))
        return {"data": output}

    def scan_options(self) -> Dict[str, Any]:
        with self._lock:
            values = {row["key"]: row["value"] for row in self._connection.execute(
                "SELECT key,value FROM history_meta WHERE key IN ('auto_scan','scan_interval_seconds')")}
        try:
            interval = float(values.get("scan_interval_seconds", "2"))
        except (TypeError, ValueError):
            interval = 2.0
        interval = min(86400.0, max(0.25, interval))
        auto_scan = values.get("auto_scan", "1").strip().casefold() not in ("0", "false", "no")
        return {"auto_scan": auto_scan, "interval_seconds": interval}

    def set_scan_options(self, auto_scan: Any, interval_seconds: Any) -> Dict[str, Any]:
        if type(auto_scan) is not bool:
            raise ValueError("auto_scan must be a boolean")
        if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, (int, float)):
            raise ValueError("interval_seconds must be a number")
        interval = float(interval_seconds)
        if not math.isfinite(interval) or not 0.25 <= interval <= 86400:
            raise ValueError("interval_seconds must be between 0.25 and 86400")
        with self._lock:
            self._connection.execute("INSERT OR REPLACE INTO history_meta(key,value) VALUES ('auto_scan',?)",
                                     ("1" if auto_scan else "0",))
            self._connection.execute("INSERT OR REPLACE INTO history_meta(key,value) VALUES ('scan_interval_seconds',?)",
                                     (str(interval),))
            self._connection.commit()
        return {"auto_scan": auto_scan, "interval_seconds": interval}

    def pricing_settings(self) -> Dict[str, Any]:
        with self._lock:
            return {"rules": self._stored_pricing_rules(), "catalog": self.pricing.metadata()}

    def set_pricing_rules(self, rules: Any) -> Dict[str, Any]:
        normalized = validate_pricing_rules(rules)
        encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        rules_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        with self._lock:
            self._connection.execute("""INSERT OR REPLACE INTO pricing_config
                (id,rules_json,rules_hash,updated_at) VALUES (1,?,?,?)""",
                (encoded, rules_hash, _now_iso()))
            self._connection.commit()
            self.pricing = PricingCatalog(rules=normalized)
        with self._query_lock:
            self._dashboard_cache.clear()
        return self.pricing_settings()

    def _pricing_rules_hash(self) -> str:
        rules = validate_pricing_rules(getattr(self.pricing, "rules", []))
        encoded = json.dumps(rules, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _reprice_data_hash(rows: Sequence[Mapping[str, Any]]) -> str:
        data = []
        for row in sorted(rows, key=lambda item: str(item["request_id"])):
            data.append([
                row["request_id"], row["timestamp"], row["provider"], row["model"], row["workspace"],
                *(int(row[name] or 0) for name in DashboardDB._token_columns()),
                row["rate_snapshot"], row["cost_before_discount_usd"],
                row["cost_after_discount_usd"],
            ])
        return hashlib.sha256(json.dumps(data, ensure_ascii=False, separators=(",", ":"))
                              .encode("utf-8")).hexdigest()

    @staticmethod
    def _quote_totals(rows: Sequence[Mapping[str, Any]], quotes: Sequence[Mapping[str, Any]]) -> Tuple[float, float, int]:
        before = 0.0
        after = 0.0
        unpriced = 0
        for quote in quotes:
            if quote["pricing_status"] == "unpriced":
                unpriced += 1
                continue
            before += float(quote["cost_before_discount_usd"] or 0)
            after += float(quote["cost_after_discount_usd"] or 0)
        return round(before, 8), round(after, 8), unpriced

    def reprice_preview(
        self, *, days=0, provider=None, workspace=None, model=None,
        start_date=None, end_date=None,
    ) -> Dict[str, Any]:
        filters = {"days": int(days or 0), "provider": provider, "workspace": workspace,
                   "model": model, "start_date": start_date, "end_date": end_date}
        # Match scan/dashboard lock order and hold a stable view through quote,
        # data hashing, and preview persistence. A concurrent scan or price edit
        # must invalidate this preview rather than mix two generations.
        with self._query_lock, self._lock:
            rows = self._query_filtered_rows(**filters)
            new_quotes = [self._quote_for_usage(
                row["model"], row["provider"], row["timestamp"], row["input_tokens"],
                row["cached_input_tokens"], row["cache_write_input_tokens"], row["output_tokens"])
                for row in rows]
            old_quotes = [self._stored_quote(row) for row in rows]
            old_before, old_after, _old_unpriced = self._quote_totals(rows, old_quotes)
            new_before, new_after, unpriced = self._quote_totals(rows, new_quotes)
            token = uuid.uuid4().hex
            now = _dt.datetime.now(_dt.timezone.utc)
            expires = now + _dt.timedelta(minutes=15)
            rules_hash = self._pricing_rules_hash()
            data_hash = self._reprice_data_hash(rows)
            self._connection.execute("DELETE FROM reprice_previews WHERE expires_at < ?", (now.isoformat(),))
            self._connection.execute("""INSERT INTO reprice_previews
                (token,filters_json,rules_hash,data_hash,request_count,created_at,expires_at)
                VALUES (?,?,?,?,?,?,?)""", (
                token, json.dumps(filters, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                rules_hash, data_hash, len(rows), now.isoformat(), expires.isoformat()))
            self._connection.commit()
        return {
            "request_count": len(rows),
            "cost_before_discount_usd": new_before,
            "cost_after_discount_usd": new_after,
            "previous_cost_before_discount_usd": old_before,
            "previous_cost_after_discount_usd": old_after,
            "unpriced_request_count": unpriced,
            "preview_token": token,
        }

    def reprice_apply(self, preview_token: str) -> Dict[str, Any]:
        if not isinstance(preview_token, str) or len(preview_token) != 32:
            raise ValueError("invalid or expired preview")
        with self._lock:
            preview = self._connection.execute(
                "SELECT * FROM reprice_previews WHERE token=?", (preview_token,)).fetchone()
            if not preview:
                raise ValueError("invalid or expired preview")
            try:
                expires = _dt.datetime.fromisoformat(preview["expires_at"])
                filters = json.loads(preview["filters_json"])
            except (ValueError, TypeError):
                raise ValueError("invalid or expired preview") from None
            if expires <= _dt.datetime.now(_dt.timezone.utc):
                self._connection.execute("DELETE FROM reprice_previews WHERE token=?", (preview_token,))
                self._connection.commit()
                raise ValueError("invalid or expired preview")
            if preview["rules_hash"] != self._pricing_rules_hash():
                self._connection.execute("DELETE FROM reprice_previews WHERE token=?", (preview_token,))
                self._connection.commit()
                raise ValueError("pricing settings changed after preview")
            rows = self._query_filtered_rows(**filters)
            if len(rows) != int(preview["request_count"]) or self._reprice_data_hash(rows) != preview["data_hash"]:
                self._connection.execute("DELETE FROM reprice_previews WHERE token=?", (preview_token,))
                self._connection.commit()
                raise ValueError("usage data changed after preview")
            stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup_path = self.db_path.parent / ("usage-reprice-%s-%s.sqlite3" % (stamp, uuid.uuid4().hex[:8]))
            backup = sqlite3.connect(str(backup_path), timeout=5.0)
            try:
                self._connection.backup(backup)
                check = backup.execute("PRAGMA integrity_check").fetchone()
                if not check or check[0] != "ok":
                    raise sqlite3.DatabaseError("reprice backup integrity check failed")
            finally:
                backup.close()
            updated = 0
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                for row in rows:
                    key = bytes.fromhex(str(row["request_id"]))
                    stored = self._connection.execute("""SELECT r.snapshot_id,r.retained_snapshot_id,s.*,
                        c.provider,c.model,c.workspace FROM history_requests r
                        JOIN history_snapshots s ON s.id=r.snapshot_id
                        JOIN history_contexts c ON c.id=s.context_id WHERE r.logical_key=?""", (key,)).fetchone()
                    if not stored:
                        raise ValueError("usage data changed after preview")
                    new_snapshot = self._store_snapshot(key, dict(stored), force_reprice=True)
                    self._connection.execute("""UPDATE history_requests SET snapshot_id=?,
                        retained_snapshot_id=CASE WHEN retained_snapshot_id IS NULL THEN NULL ELSE ? END
                        WHERE logical_key=?""", (new_snapshot, new_snapshot, key))
                    self._connection.execute("UPDATE history_sources SET snapshot_id=? WHERE logical_key=?",
                                             (new_snapshot, key))
                    updated += 1
                self._connection.execute("DELETE FROM reprice_previews WHERE token=?", (preview_token,))
                self._rebuild_logical_records([bytes.fromhex(str(row["request_id"])) for row in rows])
                self._collect_unused_snapshots()
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
            self._data_version += 1
            self._dashboard_cache.clear()
        return {"updated_count": updated, "backup_path": str(backup_path)}

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
            "cost_before_discount_usd": 0.0,
            "cost_after_discount_usd": 0.0,
            "cache_savings_before_discount_usd": 0.0,
            "cache_savings_after_discount_usd": 0.0,
            "fresh_input_tokens": 0,
            "cache_read_available_count": 0,
            "cache_write_available_count": 0,
            "cache_read_input_tokens": 0,
            "_cache_read_cached_tokens": 0,
            "priced_tokens": 0,
            "unpriced_tokens": 0,
            "priced_request_count": 0,
            "unpriced_request_count": 0,
            "speed_output_tokens": 0,
            "speed_duration_ms": 0.0,
            "speed_sample_count": 0,
            "ttft_sample_count": 0,
            "_ttft_keys": set(),
            "_ttft_values": [],
            "_speed_keys": set(),
            "_speed_candidate_keys": set(),
            "_speed_exclusion_keys": set(),
            "_speed_exclusions": {},
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
        speed_key = row["speed_group_key"]
        request_id = str(row["request_id"] if "request_id" in row.keys() else "")
        candidate_key = str(speed_key or request_id)
        if candidate_key:
            target["_speed_candidate_keys"].add(candidate_key)
        if speed_key and speed_key not in target["_speed_keys"] and float(row["speed_duration_ms"] or 0) > 0:
            target["_speed_keys"].add(speed_key)
            target["speed_sample_count"] += 1
            target["speed_output_tokens"] += int(row["speed_output_tokens"] or 0)
            target["speed_duration_ms"] += float(row["speed_duration_ms"] or 0)
        exclusion = row["speed_exclusion_reason"] if "speed_exclusion_reason" in row.keys() else None
        if exclusion and exclusion != "outside_sample_day" and candidate_key and candidate_key not in target["_speed_exclusion_keys"]:
            target["_speed_exclusion_keys"].add(candidate_key)
            target["_speed_exclusions"][str(exclusion)] = target["_speed_exclusions"].get(str(exclusion), 0) + 1
        ttft = _as_float(row["ttft_ms"] if "ttft_ms" in row.keys() else None)
        ttft_key = str(speed_key or request_id)
        if ttft and ttft > 0 and ttft_key and ttft_key not in target["_ttft_keys"]:
            target["_ttft_keys"].add(ttft_key)
            target["_ttft_values"].append(ttft)
            target["ttft_sample_count"] += 1
        target["fresh_input_tokens"] += _fresh_input_tokens(
            row["input_tokens"], row["cached_input_tokens"], row["cache_write_input_tokens"])
        target["cache_read_available_count"] += int(bool(row["cache_read_available"]))
        target["cache_write_available_count"] += int(bool(row["cache_write_available"]))
        if bool(row["cache_read_available"]):
            target["cache_read_input_tokens"] += int(row["input_tokens"] or 0)
            target["_cache_read_cached_tokens"] += int(row["cached_input_tokens"] or 0)
        if quote["is_priced"]:
            before = float(quote["cost_before_discount_usd"] or 0)
            after = float(quote["cost_after_discount_usd"] or 0)
            target["cost_before_discount_usd"] += before
            target["cost_after_discount_usd"] += after
            target["estimated_cost_usd"] += after
            target["cache_savings_before_discount_usd"] += float(quote["cache_savings_before_discount_usd"] or 0)
            target["cache_savings_after_discount_usd"] += float(quote["cache_savings_after_discount_usd"] or 0)
            target["priced_tokens"] += int(row["total_tokens"] or 0)
            target["priced_request_count"] += 1
        else:
            target["unpriced_tokens"] += int(row["total_tokens"] or 0)
            target["unpriced_request_count"] += 1

    def _stored_quote(self, row: Mapping[str, Any]) -> Dict[str, Any]:
        status = str(row["pricing_status"] or "unpriced")
        before = _as_float(row["cost_before_discount_usd"])
        after = _as_float(row["cost_after_discount_usd"])
        rate_snapshot = self._json_object(row["rate_snapshot"])
        is_priced = status in ("priced", "free")
        if is_priced and (before is None or after is None):
            result = self._quote_for_usage(
                row["model"], row["provider"], row["timestamp"], row["input_tokens"],
                row["cached_input_tokens"], row["cache_write_input_tokens"], row["output_tokens"],
                rate_snapshot=rate_snapshot or None)
            before = result["cost_before_discount_usd"]
            after = result["cost_after_discount_usd"]
            status = result["pricing_status"]
        return {
            "is_priced": is_priced,
            "pricing_status": status,
            "cost_before_discount_usd": before,
            "cost_after_discount_usd": after,
            "estimated_cost_usd": after,
            "cost_multiplier": (_as_float(row["cost_multiplier"])
                                if _as_float(row["cost_multiplier"]) is not None else 1.0),
            "rate_snapshot": rate_snapshot,
            "cost_breakdown_before_discount": self._json_object(row["cost_breakdown_before_discount"]),
            "cache_savings_before_discount_usd": _as_float(row["cache_savings_before_discount_usd"]),
            "cache_savings_after_discount_usd": _as_float(row["cache_savings_after_discount_usd"]),
            "pricing_model": rate_snapshot.get("model"),
            "rate_band": rate_snapshot.get("rate_band"),
            "source_url": rate_snapshot.get("source"),
            "note": rate_snapshot.get("note"),
        }

    @staticmethod
    def _metrics_aliases(metrics: Mapping[str, Any]) -> Dict[str, Any]:
        # Short names make the JSON convenient for small charts while the
        # *_tokens names remain canonical and unambiguous.
        aliases = {
            **{key: int(value) for key, value in metrics.items()
               if key not in ("estimated_cost_usd", "cost_before_discount_usd",
                              "cost_after_discount_usd", "cache_savings_before_discount_usd",
                              "cache_savings_after_discount_usd", "speed_duration_ms",
                              "_speed_keys", "_ttft_keys", "_ttft_values",
                              "_speed_candidate_keys", "_speed_exclusion_keys", "_speed_exclusions",
                              "_cache_read_cached_tokens")},
            "speed_duration_ms": float(metrics["speed_duration_ms"]),
            "output_tokens_per_second": (round(metrics["speed_output_tokens"] * 1000 / metrics["speed_duration_ms"], 4)
                                        if metrics["speed_duration_ms"] > 0 else None),
            "estimated_cost_usd": round(float(metrics.get("cost_after_discount_usd", 0.0)), 8),
            "cost_before_discount_usd": round(float(metrics.get("cost_before_discount_usd", 0.0)), 8),
            "cost_after_discount_usd": round(float(metrics.get("cost_after_discount_usd", 0.0)), 8),
            "cache_savings_before_discount_usd": round(float(metrics.get("cache_savings_before_discount_usd", 0.0)), 8),
            "cache_savings_after_discount_usd": round(float(metrics.get("cache_savings_after_discount_usd", 0.0)), 8),
            "ttft_median_ms": DashboardDB._percentile_float(metrics.get("_ttft_values", []), 0.50),
            "ttft_p90_ms": DashboardDB._percentile_float(metrics.get("_ttft_values", []), 0.90),
            "ttft_coverage": (int(metrics.get("ttft_sample_count", 0)) / int(metrics.get("request_count", 0))
                              if int(metrics.get("request_count", 0)) else 0.0),
            "cache_hit_rate": (int(metrics.get("_cache_read_cached_tokens", 0)) /
                               int(metrics.get("cache_read_input_tokens", 0))
                               if int(metrics.get("cache_read_input_tokens", 0)) else None),
            "speed_coverage": (int(metrics.get("speed_sample_count", 0)) /
                               (int(metrics.get("speed_sample_count", 0)) + len(metrics.get("_speed_exclusion_keys", set())))
                               if (int(metrics.get("speed_sample_count", 0)) + len(metrics.get("_speed_exclusion_keys", set()))) else 0.0),
            "speed_exclusions": dict(metrics.get("_speed_exclusions", {})),
            "input": int(metrics["input_tokens"]),
            "output": int(metrics["output_tokens"]),
            "cache": int(metrics["cached_input_tokens"]),
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

    @staticmethod
    def _percentile_float(values: Sequence[float], percentile: float) -> Optional[float]:
        if not values:
            return None
        ordered = sorted(float(value) for value in values)
        if len(ordered) == 1:
            return round(ordered[0], 3)
        rank = (len(ordered) - 1) * percentile
        lower = int(rank)
        upper = min(lower + 1, len(ordered) - 1)
        fraction = rank - lower
        return round(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction, 3)

    def dashboard(self, days=30, provider=None, workspace=None, model=None,
                  start_date=None, end_date=None, compare=False):
        # Serialize cache population; repeated SSE/filter requests reuse aggregates.
        with self._query_lock:
            self.catalog.reload()
            key = (self._data_version, self._local_now().date().isoformat(),
                   days, provider, workspace, model, start_date, end_date, bool(compare),
                   repr(self.catalog.__dict__), repr(getattr(self.pricing, "rules", [])))
            if key not in self._dashboard_cache:
                if len(self._dashboard_cache) >= 16:
                    self._dashboard_cache.clear()
                self._dashboard_cache[key] = self._build_dashboard(
                    days, provider, workspace, model, start_date, end_date, bool(compare))
            return self._dashboard_cache[key]

    def _build_dashboard(
        self,
        days: Optional[int] = 30,
        provider: Optional[str] = None,
        workspace: Optional[str] = None,
        model: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        compare: bool = False,
    ) -> Dict[str, Any]:
        # Display labels and filter aliases are configuration, not immutable
        # cached data. Reloading here makes provider renames immediately
        # visible for historical rows without rescanning rollout files.
        self.catalog.reload()
        available = self._fetch_rows(days, None, None, start_date=start_date, end_date=end_date)
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
            quote = self._stored_quote(row)
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
                {"models": set(), "bands": set(), "sources": set(), "notes": set(), "statuses": set()},
            )
            pricing_group["statuses"].add(str(quote["pricing_status"]))
            if quote["pricing_model"]:
                pricing_group["models"].add(str(quote["pricing_model"]))
            if quote["rate_band"]:
                pricing_group["bands"].add(str(quote["rate_band"]))
            if quote["source_url"]:
                pricing_group["sources"].add(str(quote["source_url"]))
            if quote["note"]:
                pricing_group["notes"].add(str(quote["note"]))
            if not quote["is_priced"]:
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
                {"models": set(), "bands": set(), "sources": set(), "notes": set(), "statuses": set()},
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

        effective_start = start_date
        effective_end = end_date
        if not effective_start and days is not None and days > 0:
            effective_end = self._local_now().date().isoformat()
            effective_start = (self._local_now().date() - _dt.timedelta(days=days - 1)).isoformat()
        comparison = None
        if compare and effective_start and effective_end:
            current_start = self._parse_local_date(effective_start)
            current_end = self._parse_local_date(effective_end)
            period_days = (current_end - current_start).days + 1
            previous_end = current_start - _dt.timedelta(days=1)
            previous_start = previous_end - _dt.timedelta(days=period_days - 1)
            previous = self._build_dashboard(
                0, provider, workspace, model, previous_start.isoformat(), previous_end.isoformat(), False)
            comparison = {
                "summary": previous["summary"],
                "start_date": previous_start.isoformat(),
                "end_date": previous_end.isoformat(),
            }

        return {
            "generated_at": _now_iso(),
            "timezone": self.timezone_name,
            "local_date": self._local_now().date().isoformat(),
            "days": days,
            "start_date": effective_start,
            "end_date": effective_end,
            "filters": {
                "provider": provider,
                "workspace": workspace,
                "providers": sorted(available_providers),
                "workspaces": available_workspaces,
                "models": sorted({canonical_model(row['model']) for row in available}),
                "model": selected_model,
            },
            "summary": summary_aliases,
            "speed_exclusions": dict(summary_aliases["speed_exclusions"]),
            "comparison": comparison,
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

    def storage_status(self) -> Dict[str, Any]:
        with self._lock:
            snapshots = self._connection.execute("SELECT COUNT(*) FROM history_snapshots").fetchone()[0]
            db_path = self.db_path
            size = self._database_size(db_path)
        return {"mode": "durable", "database_path": str(db_path),
                "database_bytes": size, "snapshots": snapshots}

    def counts(self) -> Dict[str, int]:
        # Capture one database generation before opening the read connection.
        # Old DB files are retained after relocation, so this reader stays valid
        # even if a cutover completes immediately afterwards.
        with self._lock:
            db_path = self.db_path
        reader = sqlite3.connect(str(db_path), timeout=5.0)
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

    def __init__(self, database: DashboardDB, interval: Optional[float] = None) -> None:
        self.database = database
        stored_options = database.scan_options()
        self.interval = max(0.25, float(interval if interval is not None else stored_options["interval_seconds"]))
        self.auto_scan = bool(stored_options["auto_scan"])
        self._stop = threading.Event()
        self._wake_scan = threading.Event()
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

    def notify_change(self) -> None:
        """Publish a data change made outside the scanner to SSE clients."""
        with self._changed:
            self._version += 1
            self._changed.notify_all()

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
        if initial_refresh and self.auto_scan:
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
        while not self._stop.is_set():
            woke = self._wake_scan.wait(self.interval)
            self._wake_scan.clear()
            if self._stop.is_set():
                break
            if woke:
                continue
            if not self.auto_scan:
                continue
            try:
                self.refresh()
            except Exception:
                # A partially-written JSONL is expected while Codex is active;
                # keep the poller alive and let the next pass retry it.
                continue

    def set_scan_options(self, auto_scan: Any, interval_seconds: Any) -> Dict[str, Any]:
        options = self.database.set_scan_options(auto_scan, interval_seconds)
        with self._changed:
            self.auto_scan = options["auto_scan"]
            self.interval = options["interval_seconds"]
            self._wake_scan.set()
        return options

    def stop(self) -> None:
        self._stop.set()
        self._wake_scan.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

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
            "history": self.database.storage_status(),
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

    def _query_bool(self, name: str, default: bool = False) -> bool:
        value = self._query().get(name)
        if value is None:
            return default
        if value.casefold() in ("1", "true", "yes"):
            return True
        if value.casefold() in ("0", "false", "no"):
            return False
        raise ValueError("invalid boolean query parameter")

    def _same_local_origin(self) -> bool:
        if self.headers.get("X-Dashboard-Request") != "1":
            return False
        origin = self.headers.get("Origin")
        host_header = self.headers.get("Host")
        if not origin or not host_header:
            return False
        parsed = urlparse(origin)
        if parsed.scheme != "http" or parsed.path not in ("", "/") \
                or parsed.params or parsed.query or parsed.fragment or parsed.username or parsed.password:
            return False
        if parsed.netloc.casefold() != host_header.casefold():
            return False
        try:
            hostname = parsed.hostname
            port = parsed.port
        except ValueError:
            return False
        if not hostname or port != int(self.server.server_port):
            return False
        if hostname.casefold() == "localhost":
            return True
        try:
            return ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            return False

    def _read_mutation_json(self) -> Optional[Dict[str, Any]]:
        if not self._same_local_origin():
            self._write_json({"error": "forbidden_origin"}, HTTPStatus.FORBIDDEN)
            return None
        if self.headers.get("Transfer-Encoding"):
            self._write_json({"error": "invalid_body"}, HTTPStatus.BAD_REQUEST)
            return None
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().casefold()
        if content_type != "application/json":
            self._write_json({"error": "invalid_content_type"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return None
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if not 0 <= length <= 1024 * 1024:
            self._write_json({"error": "invalid_body_size"}, HTTPStatus.BAD_REQUEST)
            return None
        try:
            raw = self.rfile.read(length)
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, OSError):
            self._write_json({"error": "invalid_json"}, HTTPStatus.BAD_REQUEST)
            return None
        if not isinstance(value, dict):
            self._write_json({"error": "invalid_body"}, HTTPStatus.BAD_REQUEST)
            return None
        return value

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
        static_routes = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js",
                         "/cache-metrics.js": "cache-metrics.js",
                         "/layout.js": "layout.js",
                         "/analytics.js": "analytics.js", "/data-settings.js": "data-settings.js",
                         "/styles.css": "styles.css"}
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
                compare = self._query_bool("compare", False)
                value = service.database.dashboard(
                    days=days,
                    provider=query.get("provider"),
                    workspace=query.get("workspace"),
                    model=query.get("model"),
                    start_date=query.get("start_date"),
                    end_date=query.get("end_date"),
                    compare=compare,
                )
                self._write_json(value)
            except ValueError:
                self._write_json({"error": "invalid_filters"}, HTTPStatus.BAD_REQUEST)
            except Exception:
                self._write_json({"error": "dashboard_unavailable"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if route == "/api/requests":
            query = self._query()
            try:
                value = service.database.requests(
                    days=int(query.get("days", "30")), provider=query.get("provider"),
                    workspace=query.get("workspace"), model=query.get("model"),
                    start_date=query.get("start_date"), end_date=query.get("end_date"),
                    page=int(query.get("page", "1")), page_size=int(query.get("page_size", "50")),
                    sort=query.get("sort", "time_desc"), task_id=query.get("task_id"),
                    cost_mode=query.get("cost_mode", "after"))
                self._write_json(value)
            except ValueError:
                self._write_json({"error": "invalid_filters"}, HTTPStatus.BAD_REQUEST)
            except Exception:
                self._write_json({"error": "requests_unavailable"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if route == "/api/request":
            request = service.database.request_detail(self._query().get("id", ""))
            if request is None:
                self._write_json({"error": "request_not_found"}, HTTPStatus.NOT_FOUND)
            else:
                self._write_json({"request": request})
            return
        if route == "/api/tasks":
            query = self._query()
            try:
                value = service.database.tasks(
                    days=int(query.get("days", "30")), provider=query.get("provider"),
                    workspace=query.get("workspace"), model=query.get("model"),
                    start_date=query.get("start_date"), end_date=query.get("end_date"))
                self._write_json(value)
            except ValueError:
                self._write_json({"error": "invalid_filters"}, HTTPStatus.BAD_REQUEST)
            except Exception:
                self._write_json({"error": "tasks_unavailable"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if route == "/api/diagnostics":
            try:
                database = service.database
                with database._query_lock, database._lock:
                    all_history = database.dashboard(days=0)
                    formats = {"modern": 0, "legacy": 0}
                    for row in database._connection.execute(
                            "SELECT usage_format,COUNT(*) AS count FROM source_files GROUP BY usage_format"):
                        if row["usage_format"] in formats:
                            formats[row["usage_format"]] = int(row["count"])
                    roots = []
                    for root in database.roots:
                        try:
                            available = root.is_dir() and os.access(str(root), os.R_OK)
                        except OSError:
                            available = False
                        roots.append({"path": str(root), "available": bool(available)})
                    scan_options = database.scan_options()
                    storage = database.storage_status()
                    scan_health = service.health()
                    diagnostics = {
                        "scan": scan_health, "roots": roots, "formats": formats,
                        "unpriced_models": all_history["pricing"].get("unpriced_model_usage", []),
                        "scan_options": scan_options, "storage": storage,
                        "speed_exclusions": all_history.get("speed_exclusions", {}),
                    }
                self._write_json(diagnostics)
            except Exception:
                self._write_json({"error": "diagnostics_unavailable"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if route == "/api/pricing":
            try:
                self._write_json(service.database.pricing_settings())
            except Exception:
                self._write_json({"error": "pricing_unavailable"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if route == "/api/events":
            self._events(service)
            return
        self._write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._write_json({"error": "cross_origin_preflight_not_allowed"}, HTTPStatus.FORBIDDEN)

    def do_POST(self) -> None:  # noqa: N802
        body = self._read_mutation_json()
        if body is None:
            return
        route = urlparse(self.path).path
        service, _host = self.app
        try:
            if route == "/api/pricing":
                if set(body) != {"rules"} or not isinstance(body.get("rules"), list):
                    self._write_json({"error": "invalid_pricing_rules"}, HTTPStatus.BAD_REQUEST)
                    return
                result = service.database.set_pricing_rules(body["rules"])
                service.notify_change()
                self._write_json(result)
                return
            if route == "/api/reprice/preview":
                allowed = {"days", "provider", "workspace", "model", "start_date", "end_date"}
                if set(body).difference(allowed):
                    self._write_json({"error": "invalid_filters"}, HTTPStatus.BAD_REQUEST)
                    return
                result = service.database.reprice_preview(**body)
                self._write_json(result)
                return
            if route == "/api/reprice":
                if set(body) != {"preview_token"}:
                    self._write_json({"error": "invalid_reprice_preview"}, HTTPStatus.BAD_REQUEST)
                    return
                result = service.database.reprice_apply(body["preview_token"])
                service.notify_change()
                self._write_json(result)
                return
            if route == "/api/scan/options":
                if set(body) != {"auto_scan", "interval_seconds"}:
                    self._write_json({"error": "invalid_scan_options"}, HTTPStatus.BAD_REQUEST)
                    return
                result = service.set_scan_options(body["auto_scan"], body["interval_seconds"])
                self._write_json(result)
                return
            if route == "/api/scan/now":
                if body:
                    self._write_json({"error": "invalid_body"}, HTTPStatus.BAD_REQUEST)
                    return
                result = service.refresh()
                self._write_json({"health": service.health(), "scan": result.as_dict()})
                return
            self._write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
        except ValueError:
            if route == "/api/reprice":
                self._write_json({"error": "preview_invalid_or_stale"}, HTTPStatus.CONFLICT)
            elif route == "/api/pricing":
                self._write_json({"error": "invalid_pricing_rules"}, HTTPStatus.BAD_REQUEST)
            elif route == "/api/scan/options":
                self._write_json({"error": "invalid_scan_options"}, HTTPStatus.BAD_REQUEST)
            else:
                self._write_json({"error": "invalid_request"}, HTTPStatus.BAD_REQUEST)
        except Exception:
            self._write_json({"error": "operation_failed"}, HTTPStatus.INTERNAL_SERVER_ERROR)

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
    serve_parser.add_argument("--interval", type=float, default=None)
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
    export_parser.add_argument("--cost-mode", choices=("before", "after"), default="after")
    for filter_parser in (summary_parser, export_parser):
        filter_parser.add_argument("--model")
        filter_parser.add_argument("--start-date")
        filter_parser.add_argument("--end-date")
        filter_parser.add_argument("--compare", action="store_true")

    args = parser.parse_args(argv)
    db = _database_from_args(args)
    try:
        if args.command == "scan":
            _print_json(db.scan().as_dict())
            return 0
        if args.command == "summary":
            db.scan()
            _print_json(db.dashboard(args.days, args.provider, args.workspace, args.model,
                                     start_date=args.start_date, end_date=args.end_date, compare=args.compare))
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
            dashboard = db.dashboard(args.days, args.provider, args.workspace, args.model,
                                     start_date=args.start_date, end_date=args.end_date, compare=args.compare)
            if args.format == "json":
                output = json.dumps(dashboard, ensure_ascii=False, indent=2, sort_keys=True)
            else:
                rows = [dict(row, cost_display_mode=args.cost_mode,
                             displayed_cost_usd=row["cost_%s_discount_usd" % args.cost_mode],
                             estimated_cost_usd=row["cost_%s_discount_usd" % args.cost_mode])
                        for row in dashboard["daily_model_usage"]]
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
                    "speed_output_tokens",
                    "speed_duration_ms",
                    "speed_sample_count",
                    "output_tokens_per_second",
                    "cost_display_mode", "displayed_cost_usd", "cost_before_discount_usd", "cost_after_discount_usd",
                    "fresh_input_tokens", "cache_read_available_count", "cache_write_available_count",
                    "cache_read_input_tokens", "cache_hit_rate",
                    "cache_savings_before_discount_usd", "cache_savings_after_discount_usd",
                    "ttft_median_ms", "ttft_p90_ms", "ttft_sample_count", "ttft_coverage",
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

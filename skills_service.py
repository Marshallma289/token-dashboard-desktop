"""Dispatch local skills requests without coupling the library to HTTP."""
from __future__ import annotations

from datetime import datetime, timezone
import argparse
from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import threading
from typing import Any, Mapping
from weakref import WeakValueDictionary


_locks = WeakValueDictionary()
_locks_guard = threading.Lock()
AGENTS = ("codex", "hermes")
MAX_BODY = 1024 * 1024
ACTIVITY_LIMIT = 500
_ACTIVITY_BYTES = 128 * 1024
_SOURCE_OPS = frozenset({"local_preview", "archive_preview", "github_preview", "market", "import_selected",
                         "check_updates", "update_preview", "apply_update", "export"})
_BACKUP_OPS = frozenset({"backup_create", "backup_list", "backup_restore", "sync_status", "sync_connect",
                         "sync_push", "sync_pull", "sync_resolve"})
_GITHUB_OPS = frozenset({"github_status", "github_connect"})
_AUTOSYNC_OPS = frozenset({"autosync_status", "autosync_set", "autosync_resume"})
_AUTOMATION_OPS = frozenset({"skill_automation", "github_source", "github_link", "github_discover",
                             "github_link_discovered", "translate_descriptions"})
_CORE_OPS = frozenset({"detail", "history", "root", "install", "disable", "delete", "tags", "rollback",
                       "update", "preset_save", "preset_apply", "preset_delete"})
_READ_OPS = frozenset({"state", "detail", "history", "backup_list", "sync_status", "activity", "activity_export",
                       "autosync_status", "github_source", "github_discover", "skill_automation"})
OPS = (_CORE_OPS | _SOURCE_OPS | _BACKUP_OPS | _GITHUB_OPS | _AUTOSYNC_OPS |
       _AUTOMATION_OPS | _READ_OPS | {"install_manager"})
_AUDITED_OPS = OPS - _READ_OPS - {"local_preview", "archive_preview", "github_preview", "market",
                                  "check_updates", "update_preview", "github_status", "translate_descriptions"}
_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
_REVISION = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_SYNC_OPS_WITH_CONFLICT = frozenset({"sync_push", "sync_pull", "sync_resolve"})
_CONFLICT_CODES = frozenset({"sync_conflict", "sync_conflict_stale"})
_CONFLICT_CHOICES = ("keep_local", "use_remote", "keep_both")
_DISPLAY_NAME = re.compile(r"\w[\w .-]{0,119}\Z")

# Only these trusted codes are used to select public text. Neither str(error)
# nor structured details from handlers are safe to serialize indiscriminately.
_PUBLIC_ERRORS = {
    "unsafe_path": "Choose a regular local path without links, network drives, or device names.",
    "unsafe_root": "Choose an independent local skill root outside protected directories.",
    "invalid_input": "Check the selected skill and enter a valid local path where requested.",
    "invalid_payload": "Check the action fields and try again.",
    "invalid_request": "Check the action fields and try again.",
    "invalid_skill": "Check that the skill contains a readable UTF-8 SKILL.md.",
    "invalid_url": "Use a supported HTTPS GitHub repository or skill URL.",
    "not_found": "Refresh the library and select an existing skill, preset, or version.",
    "duplicate_id": "Choose a unique skill ID or use the explicit update action.",
    "target_exists": "Check the install root for an existing skill. It was not overwritten.",
    "install_conflict": "A Codex skill with the same name already exists in the global skills directory. Refresh and check the conflicting paths in the skill details.",
    "deployments_active": "Disable affected managed deployments before retrying.",
    "modified": "Local skill files have changed. Preserve your edits before retrying.",
    "external_skill": "External installations are read-only. Import the skill to manage a separate copy.",
    "link_unavailable": "Directory links are unavailable here. Choose copy mode.",
    "recovery_required": "Review pending library recovery before applying another change.",
    "io_error": "Check library directory permissions and available disk space.",
    "source_changed": "The source changed during inspection. Preview it again.",
    "no_skill": "Choose a folder or archive containing a SKILL.md.",
    "limit": "The package exceeds a safety limit. Choose a smaller package.",
    "unsafe_archive": "Choose a valid ZIP archive without links, encryption, or conflicting paths.",
    "invalid_archive": "Choose a valid backup created by the Skill Library.",
    "foreign_backup": "Choose a backup from this agent's library.",
    "foreign_snapshot": "Choose a remote snapshot for this agent's library.",
    "invalid_backup_id": "Select a valid backup from this library.",
    "backup_not_found": "Refresh the backup list and select an existing backup.",
    "invalid_remote": "Use a credential-free Git URL or an absolute local Git repository path.",
    "invalid_sync_config": "Check the saved Git sync settings and reconnect the repository.",
    "confirmation_required": "Confirm this action before continuing.",
    "sync_not_connected": "Connect a Git remote before syncing.",
    "git_unavailable": "Install Git before enabling sync.",
    "git_failed": "Check Git connectivity and repository permissions before retrying.",
    "git_timeout": "Check the Git remote connectivity before retrying.",
    "backup_busy": "Wait for the current backup or sync to finish and retry.",
    "backup_io_failed": "Check available space and backup directory permissions.",
    "sync_conflict": "Local and remote changes conflict. Review both snapshots before retrying.",
    "sync_conflict_stale": "A library changed after confirmation. Review the updated conflict before retrying.",
    "remote_ahead": "Pull the remote snapshot before pushing.",
    "remote_changed": "Fetch the remote changes before pushing again.",
    "remote_missing": "Check that the configured Git branch still exists.",
    "invalid_remote_snapshot": "The remote branch does not contain a usable Skill Library snapshot.",
    "library_changed": "Refresh the current library and retry.",
    "sync_state_not_saved": "The snapshot transferred; check local permissions and verify sync status before retrying.",
    "github_no_gh": "Install the official GitHub CLI, or connect a private repository via SSH.",
    "github_auth_required": "Run gh auth login --hostname github.com, then retry.",
    "github_request_failed": "Check GitHub connectivity and repository access, then retry.",
    "github_create_failed": "Check whether the private repository was created before retrying.",
    "github_not_private": "Select a private GitHub repository.",
    "github_repo_mismatch": "Enter the current GitHub OWNER/REPO and confirm again.",
    "github_no_write": "Select a writable private repository that is not archived or disabled.",
    "github_invalid_repo": "Enter a GitHub repository as OWNER/REPO.",
    "github_account_changed": "Refresh the active GitHub CLI account and confirm the operation again.",
    "github_helper_unavailable": "Update the official GitHub CLI or choose SSH.",
    "autosync_busy": "Wait for automatic sync to finish or restart the desktop app.",
    "autosync_io": "Check the automatic sync settings directory and its permissions.",
    "github_version_mismatch": "The local files differ from this GitHub version. Check the branch and skill folder.",
}


class InvalidSkillsRequest(ValueError):
    """Invalid API envelope, or operation/payload rejected by the library."""


def _matches(pattern: re.Pattern[str], value: Any) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _conflict_details(value: Mapping[str, Any], agent: str) -> dict[str, Any]:
    details = {}
    conflict = value.get("conflict")
    if isinstance(conflict, dict):
        revision, digest, choices = (conflict.get(key) for key in ("remote_revision", "local_digest", "choices"))
        if (_matches(_REVISION, revision) and _matches(_DIGEST, digest) and isinstance(choices, list)
                and 1 <= len(choices) <= len(_CONFLICT_CHOICES) and all(choice in _CONFLICT_CHOICES for choice in choices)
                and len(set(choices)) == len(choices)):
            safe_conflict = {"remote_revision": revision, "local_digest": digest, "choices": list(choices)}
            for key in ("skill_ids", "preset_names"):
                values = conflict.get(key)
                if isinstance(values, list):
                    safe_conflict[key] = [item for item in values[:128] if _matches(_DISPLAY_NAME, item)]
            if type(conflict.get("baseline_available")) is bool:
                safe_conflict["baseline_available"] = conflict["baseline_available"]
            details["conflict"] = safe_conflict
    status = value.get("status")
    if isinstance(status, dict) and status.get("state") == "conflict":
        safe_status: dict[str, Any] = {"state": "conflict"}
        if status.get("branch") == f"skills-{agent}":
            safe_status["branch"] = status["branch"]
        for key in ("connected", "git_available", "remote_checked", "local_changed"):
            data = status.get(key)
            if type(data) is bool or (key == "local_changed" and key in status and data is None):
                safe_status[key] = data
        for key in ("revision", "remote_revision"):
            data = status.get(key)
            if key == "remote_revision" and "conflict" in details and data != details["conflict"][key]:
                continue
            if _matches(_REVISION, data) or (key in status and data is None):
                safe_status[key] = data
        data = status.get("last_synced_at")
        if (isinstance(data, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z", data)
                or "last_synced_at" in status and data is None):
            safe_status["last_synced_at"] = data
        details["status"] = safe_status
    return details


class SkillsSyncConflict(InvalidSkillsRequest):
    def __init__(self, code: str, details: dict[str, Any]) -> None:
        super().__init__(_PUBLIC_ERRORS[code])
        self.response = {"ok": False, "error": str(self), "code": code, **details}


def _cli_envelope(value: Any, http_status: int, op: str, agent: str) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("ok") is True:
        return http_status == 200 and value.keys() == {"ok", "result"}
    if value.get("ok") is not False or not isinstance(value.get("error"), str) or not 400 <= http_status < 600:
        return False
    if value.keys() == {"ok", "error"}:
        return True
    code = value.get("code")
    if http_status != 409 or op not in _SYNC_OPS_WITH_CONFLICT or not isinstance(code, str) or code not in _CONFLICT_CODES:
        return False
    return value == {"ok": False, "error": _PUBLIC_ERRORS[code], "code": code, **_conflict_details(value, agent)}


def _public_error(op: str, code: Any = None) -> str:
    if isinstance(code, str) and code in _PUBLIC_ERRORS:
        return _PUBLIC_ERRORS[code]
    if op in _SOURCE_OPS:
        return "Check the skill source and create a new preview before retrying."
    if op in _BACKUP_OPS:
        return "Check the backup or sync settings and retry from the current library state."
    if op in _GITHUB_OPS:
        return "Check the GitHub CLI login and private repository settings before retrying."
    if op in _AUTOSYNC_OPS:
        return "Check the automatic sync settings and current connection before retrying."
    return "Check the selected skill, input, and library permissions before retrying."


def local_data_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "CodexTokenDashboard"
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "CodexTokenDashboard"


def publish_endpoint(data_dir: Path, port: int) -> None:
    """Publish only an ephemeral IPv4 loopback port, never user paths in the record."""
    destination = data_dir / "skills-endpoint.json"
    with tempfile.NamedTemporaryFile(dir=data_dir, mode="w", encoding="utf-8", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            json.dump({"port": port}, handle)
            handle.close()
            temporary.replace(destination)
        finally:
            handle.close()
            temporary.unlink(missing_ok=True)


def _local_port(data_dir: Path) -> int | None:
    try:
        path = data_dir / "skills-endpoint.json"
        _plain(path, directory=False)
        with path.open("rb") as file:
            value = json.loads(file.read(128)).get("port")
        if type(value) is int and 1 <= value <= 65535:
            return value
    except (OSError, ValueError, AttributeError):
        pass
    return None


def forget_endpoint(data_dir: Path, port: int) -> None:
    if _local_port(data_dir) == port:
        (data_dir / "skills-endpoint.json").unlink(missing_ok=True)


def _plain(path: Path, *, directory: bool) -> None:
    entry = path.lstat()
    valid_type = stat.S_ISDIR(entry.st_mode) if directory else stat.S_ISREG(entry.st_mode)
    if not valid_type or getattr(entry, "st_file_attributes", 0) & 0x400:
        raise OSError("Activity storage is not a regular local entry")


def activity_file(data_dir: Path, agent: str, *, create: bool = False) -> Path:
    folder = data_dir
    if create:
        folder.mkdir(parents=True, exist_ok=True)
    _plain(folder, directory=True)
    for name in ("skill_data", "activity"):
        folder /= name
        if create:
            folder.mkdir(exist_ok=True)
        _plain(folder, directory=True)
    return folder / f"{agent}.jsonl"


def _read_activity(data_dir: Path, agent: str) -> list[dict[str, str]]:
    try:
        path = activity_file(data_dir, agent)
        _plain(path, directory=False)
        with path.open("rb") as handle:
            raw = handle.read(_ACTIVITY_BYTES + 1)
    except FileNotFoundError:
        return []
    if len(raw) > _ACTIVITY_BYTES:
        return []
    entries = []
    for line in raw.splitlines()[-ACTIVITY_LIMIT:]:
        try:
            item = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if (isinstance(item, dict) and item.keys() == {"time", "agent", "op", "result"}
                and all(isinstance(value, str) for value in item.values())
                and item["agent"] == agent and item["op"] in _AUDITED_OPS
                and item["result"] in {"ok", "error"} and _TIME.fullmatch(item["time"])):
            entries.append(item)
    return entries


def _activity_jsonl(entries: list[dict[str, str]]) -> str:
    return "".join(json.dumps(item, separators=(",", ":")) + "\n" for item in entries)


def _record_activity(data_dir: Path, agent: str, op: str, result: str) -> None:
    try:
        path = activity_file(data_dir, agent, create=True)
        entries = _read_activity(data_dir, agent)
        entries.append({"time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "agent": agent, "op": op, "result": result})
        with tempfile.NamedTemporaryFile(dir=path.parent, mode="w", encoding="utf-8", newline="\n", delete=False) as handle:
            temporary = Path(handle.name)
            try:
                handle.write(_activity_jsonl(entries[-ACTIVITY_LIMIT:]))
                handle.close()
                temporary.replace(path)
            finally:
                handle.close()
                temporary.unlink(missing_ok=True)
    except OSError:
        # A log failure must not turn an already applied mutation into a retry.
        pass


def check_dependencies() -> None:
    """Check frozen imports without accessing a live skills workspace."""
    from skills_core import SkillLibrary
    from skills_sources import handle as handle_sources
    from skills_backup import handle as handle_backup
    from skills_github import handle as handle_github, credential_helper
    from skills_autosync import AutoSyncManager, get_autosync, OPS as autosync_ops
    from skills_translation import TranslationCache
    from skills_upstream import UpstreamWorker

    if autosync_ops != _AUTOSYNC_OPS or not all(callable(value) for value in (
            SkillLibrary, handle_sources, handle_backup, handle_github, credential_helper, get_autosync,
            AutoSyncManager.action, AutoSyncManager.start, AutoSyncManager.stop, TranslationCache.translate,
            UpstreamWorker.start, UpstreamWorker.stop)):
        raise RuntimeError("Invalid skills module interfaces")


class SkillsService:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir.expanduser().resolve()
        self._autosync = None
        self._upstream = None
        self._translations = None
        key = os.path.normcase(str(self.data_dir))
        with _locks_guard:
            self._lock = _locks.get(key)
            if self._lock is None:
                self._lock = _locks[key] = threading.RLock()

    @property
    def autosync(self):
        with self._lock:
            if self._autosync is None:
                from skills_autosync import get_autosync

                self._autosync = get_autosync(self.data_dir)
            return self._autosync

    @property
    def upstream(self):
        with self._lock:
            if self._upstream is None:
                from skills_upstream import UpstreamWorker

                self._upstream = UpstreamWorker(self.data_dir)
            return self._upstream

    def dispatch(self, request: Mapping[str, Any]) -> Any:
        if set(request) != {"agent", "op", "payload"}:
            raise InvalidSkillsRequest("invalid_body")
        agent, op, payload = request["agent"], request["op"], request["payload"]
        if agent not in AGENTS:
            raise InvalidSkillsRequest("invalid_agent")
        if not isinstance(op, str) or op not in OPS:
            raise InvalidSkillsRequest("invalid_operation")
        if not isinstance(payload, dict) or (op in {"state", "activity", "activity_export"} and payload):
            raise InvalidSkillsRequest("invalid_payload")

        if op == "translate_descriptions":
            from skills_core import SkillLibrary
            from skills_translation import TranslationCache

            with self._lock:
                if self._translations is None:
                    self._translations = TranslationCache(self.data_dir)
            try:
                return self._translations.translate(SkillLibrary(self.data_dir, agent), payload)
            except (ValueError, OSError) as error:
                raise InvalidSkillsRequest(_public_error(op, getattr(error, "code", None))) from None

        # Reads can inspect several files, so they share the write lock too.
        # The lock covers construction and both agents, across server instances.
        with self._lock:
            if op in {"activity", "activity_export"}:
                try:
                    entries = list(reversed(_read_activity(self.data_dir, agent)))
                except OSError:
                    raise InvalidSkillsRequest("Check that the activity directory is a readable local directory.") from None
                if op == "activity":
                    return {"entries": entries}
                return {"filename": f"skills-activity-{agent}.jsonl", "mime_type": "application/x-ndjson",
                        "content": _activity_jsonl(entries)}
            outcome = "error"
            try:
                result = self._dispatch(agent, op, payload)
                if isinstance(result, dict) and result.get("ok") is False:
                    error = result.get("error")
                    code = error.get("code") if isinstance(error, dict) else result.get("code", error)
                    if op in _SYNC_OPS_WITH_CONFLICT and isinstance(code, str) and code in _CONFLICT_CODES:
                        raise SkillsSyncConflict(code, _conflict_details(result, agent))
                    raise InvalidSkillsRequest(_public_error(op, code))
                outcome = "ok"
                return result
            except InvalidSkillsRequest:
                raise
            except (ValueError, OSError) as error:
                raise InvalidSkillsRequest(_public_error(op, getattr(error, "code", None))) from None
            finally:
                if op in _AUDITED_OPS and (op != "root" or "path" in payload):
                    _record_activity(self.data_dir, agent, op, outcome)

    def _dispatch(self, agent: str, op: str, payload: dict[str, Any]) -> Any:
        if op in _AUTOSYNC_OPS:
            return self.autosync.action(agent, op, payload)
        if op in _GITHUB_OPS:
            from skills_github import handle
            return handle(None, op, payload)

        from skills_core import SkillLibrary

        lib = SkillLibrary(self.data_dir, agent)
        if op == "state":
            return lib.state()
        if op == "skill_automation":
            from skills_translation import auth_key

            settings = lib.automation(payload or None)
            if payload.get("github_auto") is True:
                self.upstream.request_check(agent)
            return {"settings": settings, "key_available": bool(auth_key()), "upstream": self.upstream.status(agent)}
        if op in {"github_discover", "github_link_discovered"}:
            from skills_upstream import discover_github

            return discover_github(lib, payload, confirm=op == "github_link_discovered")
        if op in {"github_source", "github_link"}:
            from skills_upstream import skill_source

            return skill_source(lib, payload, link=op == "github_link")
        if op in _SOURCE_OPS:
            from skills_sources import handle
            return handle(lib, op, payload)
        if op in _BACKUP_OPS:
            from skills_backup import handle
            return handle(lib, op, payload)
        if op == "install_manager":
            if payload != {"confirm": True} or payload.get("confirm") is not True:
                raise InvalidSkillsRequest(_PUBLIC_ERRORS["confirmation_required"])
            bundled = Path(__file__).resolve().parent / "assets" / "codex-token-skill-manager"
            lib.import_path(bundled, source={"type": "builtin"})
            return lib.action("install", {"id": bundled.name, "mode": "copy"})
        return lib.action(op, payload)


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Call a local Skill Library server; no remote hosts are accepted.")
    parser.add_argument("--port", type=int, help="local HTTP port (default: discovered desktop port, or 8765)")
    parser.add_argument("--agent", required=True, choices=AGENTS)
    parser.add_argument("--op", required=True, choices=sorted(OPS))
    parser.add_argument("--payload-stdin", action="store_true", help="read the JSON object from stdin, not shell arguments")
    parser.add_argument("--apply", action="store_true", help="explicitly allow actions outside the read-only whitelist")
    args = parser.parse_args(argv)

    def emit(value: dict[str, Any]) -> int:
        print(json.dumps(value, separators=(",", ":")))
        return 0 if value.get("ok") is True else 1

    port = args.port if args.port is not None else (_local_port(local_data_dir()) or 8765)
    if not 1 <= port <= 65535:
        return emit({"ok": False, "error": "invalid_port"})
    if args.op not in _READ_OPS and not args.apply:
        return emit({"ok": False, "error": "explicit_apply_required"})
    try:
        raw = sys.stdin.buffer.read(MAX_BODY + 1) if args.payload_stdin else b"{}"
        if len(raw) > MAX_BODY:
            raise ValueError
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError
        if args.op == "skill_automation" and payload and not args.apply:
            return emit({"ok": False, "error": "explicit_apply_required"})
        body = json.dumps({"agent": args.agent, "op": args.op, "payload": payload},
                          ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_BODY:
            raise ValueError
    except (ValueError, OSError, RecursionError):
        return emit({"ok": False, "error": "invalid_payload"})
    connection = HTTPConnection("127.0.0.1", port, timeout=180)
    try:
        connection.request("GET", "/api/health")
        response = connection.getresponse()
        health = json.loads(response.read(256 * 1024))
        if response.status != 200 or not isinstance(health, dict) or health.get("skills_api") != 1:
            raise ValueError
        origin = "http://127.0.0.1" if port == 80 else f"http://127.0.0.1:{port}"
        connection.request("POST", "/api/skills", body=body,
                           headers={"Origin": origin,
                                    "Content-Type": "application/json", "X-Dashboard-Request": "1"})
        response = connection.getresponse()
        if not response.getheader("Content-Type", "").startswith("application/json"):
            raise ValueError
        raw_response = response.read(16 * 1024 * 1024 + 1)
        if len(raw_response) > 16 * 1024 * 1024:
            raise ValueError
        result = json.loads(raw_response)
        if not _cli_envelope(result, response.status, args.op, args.agent):
            raise ValueError
    except (ValueError, OSError, HTTPException, RecursionError):
        return emit({"ok": False, "error": "local_skills_service_unavailable"})
    finally:
        connection.close()
    return emit(result)


if __name__ == "__main__":
    raise SystemExit(cli())

"""Associate verified skill copies with GitHub and refresh tracked versions."""

from configparser import RawConfigParser, Error as ConfigError
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread

from skills_core import AGENTS, SkillLibrary, SkillError, plain
import skills_sources as sources


def _read_text(path):
    info = plain(path, file=True, missing=True)
    if info is None or info.st_size > sources.MiB or info.st_nlink != 1:
        return ""
    with path.open(encoding="utf-8-sig") as file:
        return file.read(sources.MiB + 1)


def _read_index(path, field):
    try:
        value = json.loads(_read_text(path))
    except (OSError, ValueError, RecursionError):
        return {}
    entries = value.get(field) if isinstance(value, dict) else None
    return entries if isinstance(entries, dict) else {}


def _source_path(lib, row):
    if row["external"]:
        return Path(row["path"])
    origin = lib.source_origin(row["id"])
    if isinstance(origin, dict):
        local = Path(origin["path"])
        return (local.parent if local.name == "SKILL.md" else local) / origin.get("subdir", "")
    return Path(origin) if isinstance(origin, str) else None


def _github_url(remote):
    if not isinstance(remote, str):
        return ""
    for prefix in ("git@github.com:", "ssh://git@github.com/"):
        if remote.startswith(prefix):
            return "https://github.com/" + remote.removeprefix(prefix)
    return remote


def _git_origin(path):
    if not path.is_absolute() or not path.is_dir():
        return None
    for repo in (path, *path.parents):
        if not (repo / ".git").exists():
            continue
        git = repo / ".git"
        config = RawConfigParser(strict=False)
        config.read_string(_read_text(git / "config"))
        url = _github_url(config.get('remote "origin"', "url", fallback=""))
        head = _read_text(git / "HEAD").strip()
        branch = head.removeprefix("ref: refs/heads/") if head.startswith("ref: refs/heads/") else ""
        source = sources._github_spec({"url": url, "subdir": path.relative_to(repo).as_posix()
                                       if path != repo else "", "ref": branch})
        revision = head
        if branch:
            ref = "refs/heads/" + branch
            revision = _read_text(git.joinpath(*ref.split("/"))).strip()
            if not revision:
                revision = next((line.split()[0] for line in _read_text(git / "packed-refs").splitlines()
                                 if line.endswith(" " + ref)), "")
        if sources._SHA.fullmatch(revision):
            source["revision"] = revision.lower()
        return source
    return None


class Discovery:
    def __init__(self, lib, root):
        self.lib, self.root = lib, root
        xdg = os.environ.get("XDG_STATE_HOME")
        npx_lock = (Path(xdg) / "skills" if xdg else Path.home() / ".agents") / ".skill-lock.json"
        self.npx = _read_index(npx_lock, "skills")
        self.hermes = _read_index(root / ".hub" / "lock.json", "installed") if lib.agent == "hermes" else {}

    def hint(self, row):
        try:
            path = _source_path(self.lib, row)
            if path is None:
                return None
            if path.is_relative_to(self.root):
                hint = self._lock_hint(path.relative_to(self.root).as_posix(), path.name)
                if hint:
                    return hint
            return _git_origin(path)
        except (ValueError, OSError, TypeError, KeyError, ConfigError):
            return None

    def _lock_hint(self, relative, name):
        entry = self.hermes.get(name)
        if isinstance(entry, dict) and entry.get("install_path") == relative:
            identifier = entry.get("identifier", "")
            if isinstance(identifier, str):
                if entry.get("source") == "github":
                    owner, repo, tail = identifier.split("/", 2)
                    return sources._github_spec({"url": f"https://github.com/{owner}/{repo}",
                                                 "subdir": tail.removesuffix("/SKILL.md")})
                if entry.get("source") == "url":
                    return sources._github_spec({"url": identifier})
        entry = self.npx.get(name)
        if relative != name or not isinstance(entry, dict) or entry.get("sourceType") != "github":
            return None
        path, identifier = entry.get("skillPath"), entry.get("source")
        if not isinstance(path, str) or not path:
            return None  # Older lock entries without a pinned path would be ambiguous.
        folder = "" if path == "SKILL.md" else path.removesuffix("/SKILL.md")
        url = "https://github.com/" + identifier if isinstance(identifier, str) else _github_url(entry.get("sourceUrl", ""))
        source = sources._github_spec({"url": url,
                                       "ref": entry.get("ref") or "", "subdir": folder})
        revision = entry.get("commit")
        if isinstance(revision, str) and sources._SHA.fullmatch(revision):
            source["revision"] = revision.lower()
        return source


def _snapshot_source(source, repo, revision):
    remote = sources._subdir(repo, source["subdir"])
    checksum = sources._digest(sources._manifest(remote))
    return remote, dict(source, digest=checksum, revision=revision)


def _guard(before, checksum, *, automatic=False):
    def validate(path, row):
        if row["source"] != before or sources._digest(sources._manifest(path)) != checksum:
            raise SkillError("source_changed", "Skill changed during upstream verification")
        if row.get("modified") or (automatic and row.get("installed_externally")):
            raise SkillError("modified", "Deployment is modified or externally installed")
    return validate


def _link(lib, row, incoming, *, automatic=False):
    checksum = incoming["digest"]
    if sources._digest(sources._manifest(Path(row["path"]))) != checksum:
        raise SkillError("github_version_mismatch", "Local files do not match this GitHub version")
    # A managed copy of an external installation can be associated, but its
    # external deployment remains under the original installer's control.
    return lib.link_github(row["id"], incoming, _guard(row["source"], checksum), automatic=automatic)


def skill_source(lib, payload, *, link=False):
    fields = {"id", "url", "ref", "subdir"} if link else {"id"}
    if set(payload) - fields or (not payload.get("url") and set(payload) != {"id"}):
        raise SkillError("invalid_payload", "Enter a skill ID and GitHub source")
    sid = sources._string(payload, "id", limit=256)
    state = lib.state()
    row = next((row for row in state["skills"] if row["id"] == sid), None)
    if row is None:
        raise SkillError("not_found", "Unknown skill")
    stored = row["source"]
    tracked = stored if isinstance(stored, dict) and stored.get("kind") == "github" else None
    hint = tracked or Discovery(lib, Path(state["root"])).hint(row)
    if not link:
        return {"source": hint}
    if row["external"]:
        raise SkillError("external_skill", "Import a copy before managing its source")
    source = sources._github_spec(payload) if payload.get("url") else hint
    if source is None:
        raise SkillError("invalid_url", "Enter the GitHub URL for this skill")
    if hint and all(source.get(field, "") == hint.get(field, "") for field in ("url", "ref", "subdir")):
        source = dict(source, revision=hint.get("revision"))
    with TemporaryDirectory(prefix="skills-upstream-") as temp:
        repo, revision = sources._github_checkout(source, Path(temp), source.get("revision"))
        _, incoming = _snapshot_source(source, repo, revision)
        return {"skill": _link(lib, row, incoming)}


def run_updates(lib, cancelled=lambda: False):
    summary = {"linked": 0, "updated": 0, "skipped": 0, "failed": 0}
    if not lib.automation()["github_auto"] or cancelled():
        return summary
    state = lib.state()
    discovery = Discovery(lib, Path(state["root"]))
    groups = {}
    for row in state["skills"]:
        if row["external"] or row["automatic_update_paused"]:
            continue
        current = row["source"]
        tracked = isinstance(current, dict) and current.get("kind") == "github"
        if row.get("modified") or (tracked and row.get("installed_externally")):
            summary["skipped"] += int(tracked)
            continue
        try:
            if tracked:
                source = sources._stored(row) | sources._github_spec(current)
            else:
                source = discovery.hint(row)
        except (ValueError, OSError):
            summary["failed"] += 1
            continue
        if source is not None:
            pin = None if tracked else source.get("revision")
            groups.setdefault((source["url"], source["ref"], pin), []).append((row, source, tracked))
    for key, rows in groups.items():
        if cancelled():
            break
        with TemporaryDirectory(prefix="skills-upstream-") as temp:
            work = Path(temp)
            try:
                head = key[2] or sources._github_head(rows[0][1])
                if all(tracked and source.get("revision") == head for _, source, tracked in rows):
                    continue
                repo, revision = sources._github_checkout(rows[0][1], work, head)
            except (ValueError, OSError):
                summary["failed"] += len(rows)
                continue
            for index, (row, source, tracked) in enumerate(rows):
                if cancelled():
                    break
                try:
                    remote, incoming = _snapshot_source(source, repo, revision)
                    sid, checksum = row["id"], incoming["digest"]
                    if not tracked:
                        _link(lib, row, incoming, automatic=True)
                        summary["linked"] += 1
                    elif checksum != source["digest"]:
                        staged = sources._stage(work, index, sid)
                        if sources._digest(sources._manifest(remote, staged)) != checksum:
                            raise SkillError("source_changed", "Snapshot changed during update")
                        if cancelled():
                            break
                        lib.replace_skill(sid, staged, source=incoming, automatic=True,
                                          validate_current=_guard(row["source"], source["digest"], automatic=True))
                        summary["updated"] += 1
                except (ValueError, OSError, sqlite3.Error) as exc:
                    if getattr(exc, "code", None) in {"modified", "source_changed", "github_version_mismatch",
                                                       "automation_off"}:
                        summary["skipped"] += 1
                    else:
                        summary["failed"] += 1
    return summary


class UpstreamWorker:
    def __init__(self, data_dir, *, clock=None):
        self.data_dir = data_dir
        self._clock = clock if clock is not None else lambda: datetime.now().astimezone()
        self._wake = Event()
        self._stop = Event()
        self._thread = None
        self._lock = Lock()
        self._pending = set()

    def status(self, agent):
        return SkillLibrary(self.data_dir, agent).github_check_status()

    def request_check(self, agent):
        with self._lock:
            self._pending.add(agent)
            self._wake.set()

    def start(self):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._pending.update(AGENTS)
                self._wake.set()
                self._thread = Thread(target=self._run, daemon=True, name="skills-upstream")
                self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def check(self, agent):
        lib = SkillLibrary(self.data_dir, agent)
        now = self._clock()
        day = now.date().isoformat()
        if self._stop.is_set() or not lib.automation()["github_auto"] or not lib.begin_github_check(day):
            return
        try:
            summary = run_updates(lib, self._stop.is_set)
        except (ValueError, OSError, sqlite3.Error):
            summary = {"failed": 1}
        result = None if self._stop.is_set() else {
            **summary, "checked_at": now.astimezone(timezone.utc).isoformat()
        }
        lib.finish_github_check(day, result)

    def _run(self):
        while not self._stop.is_set():
            self._wake.wait()
            with self._lock:
                agents = sorted(self._pending)
                self._pending.clear()
                self._wake.clear()
            for agent in agents:
                try:
                    self.check(agent)
                except (ValueError, OSError, sqlite3.Error):
                    continue

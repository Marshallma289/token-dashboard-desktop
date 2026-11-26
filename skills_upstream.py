"""Associate verified skill copies with GitHub and refresh tracked versions."""

from concurrent.futures import ThreadPoolExecutor
from configparser import RawConfigParser, Error as ConfigError
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
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
            if row["external"] and row.get("mode") == "link":
                return None
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
    normalized = {}
    manifest = sources._manifest(remote, normalized=normalized)
    return remote, dict(source, digest=sources._digest(manifest), content_digest=sources._digest(normalized),
                        revision=revision)


def _guard(before, checksum, *, automatic=False):
    def validate(path, row):
        if row["source"] != before or sources._digest(sources._manifest(path)) != checksum:
            raise SkillError("source_changed", "Skill changed during upstream verification")
        if row.get("modified") or (automatic and row.get("installed_externally")):
            raise SkillError("modified", "Deployment is modified or externally installed")
    return validate


def _link(lib, row, incoming, *, automatic=False):
    normalized = {}
    checksum = sources._digest(sources._manifest(Path(row["path"]), normalized=normalized))
    if checksum != incoming["digest"] and sources._digest(normalized) != incoming.get("content_digest"):
        raise SkillError("github_version_mismatch", "Local files do not match this GitHub version")
    # A managed copy of an external installation can be associated, but its
    # external deployment remains under the original installer's control.
    return lib.link_github(row["id"], dict(incoming, digest=checksum), _guard(row["source"], checksum),
                           automatic=automatic)


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
        repo, revision = sources._github_checkout(source, Path(temp).resolve(), source.get("revision"))
        _, incoming = _snapshot_source(source, repo, revision)
        return {"skill": _link(lib, row, incoming)}


_DISCOVERY_REPOS = 6
_DISCOVERY_SLUG = re.compile(sources._PUBLIC_SKILL_SLUG)
_DOCUMENT_SOURCE = re.compile(
    r"^[\s>*|_-]*(?:repository|source|upstream|github(?: repository)?|仓库|来源|上游)[*\s]*[:：|]"
    r"[^\r\n]*?(https://github\.com/[^\s<>()\[\]\"'|]+)", re.I | re.M)


def _market_repos(slug):
    items = sources._market({"query": slug, "limit": 30})["items"]
    return [item["url"] for item in items if slug in (item["skill_id"].casefold(), item["name"].casefold())]


def _document_sources(path):
    result = {}
    for filename in ("SKILL.md", "README.md"):
        try:
            value = _read_text(path / filename)
        except (OSError, ValueError):
            continue
        for url in _DOCUMENT_SOURCE.findall(value):
            try:
                spec = sources._github_spec({"url": url.rstrip(".,;。")})
            except ValueError:
                continue
            result.setdefault((spec["url"], spec["ref"], spec["subdir"]), spec)
    return list(result.values())[:3]


def _discovery_names(row, path, read_files):
    names = [row["name"], path.name]
    if read_files:
        try:
            text = _read_text(path / "SKILL.md")
        except (OSError, ValueError):
            text = ""
        if text.startswith("---\n"):
            frontmatter = text.partition("\n---\n")[0]
            for alias in re.findall(r"^aliases:[ \t]*(.*)$", frontmatter, re.M | re.I):
                names.extend(value.strip(" \t[]\"'") for value in alias.split(",")[:3])
    names.append(row["id"])
    return tuple(dict.fromkeys(name.casefold() for name in names if _DISCOVERY_SLUG.fullmatch(name)))[:3]


def _repository_matches(source, manifest, normalized, slugs, *, origin=None, exact_path=False):
    with TemporaryDirectory(prefix="skills-discover-") as temp:
        root, revision = sources._github_checkout(source, Path(temp).resolve(), source.get("revision"))
        base = sources._subdir(root, source["subdir"])
        matches, suggestions = [], []
        for directory, path, _, is_skill in sources._walk(base, stop_at_skill=exact_path):
            if not is_skill:
                if exact_path:
                    break
                continue
            declared, _ = sources._metadata(directory, directory.name)
            names = {directory.name.casefold(), declared.casefold()}
            matching_name = bool(names.intersection(slugs))
            if not exact_path and not matching_name:
                if manifest is None:
                    continue
                with sources._regular_file(directory / "SKILL.md") as (file, size):
                    if size > sources.MiB:
                        continue
                    body = sources._normalize_text_bytes(file.read(size + 1))
                    if (hashlib.sha256(body).hexdigest(), len(body)) != normalized["SKILL.md"]:
                        continue
            upstream_text = {}
            remote = sources._manifest(directory, normalized=upstream_text)
            same = manifest is not None and remote == manifest
            equivalent = manifest is not None and upstream_text == normalized
            doc_equal = upstream_text["SKILL.md"] == normalized.get("SKILL.md")
            trusted = exact_path and origin in {"installed_source", "bound_source"}
            evidence = [origin] if origin else []
            if same or equivalent:
                evidence.append("same_files" if same else "line_endings")
            elif doc_equal:
                evidence.append("same_skill_doc")
            if matching_name:
                evidence.append("name")
            if manifest is not None and (remote.keys() & manifest.keys()) - {"SKILL.md", "README.md", "LICENSE"}:
                evidence.append("shared_files")
            subdir = "/".join(part for part in (source["subdir"], path) if part)
            candidate = dict(source, subdir=subdir, revision=revision, evidence=evidence, name=declared,
                             comparison="exact" if same else "line_endings" if equivalent
                             else "different" if manifest is not None else "unavailable")
            provenance = origin or not source.get("fork")
            if ((same or equivalent) and provenance) or (trusted and manifest is not None):
                candidate["digest"] = sources._digest(manifest if equivalent else remote)
                candidate["content_digest"] = sources._digest(upstream_text)
                matches.append(candidate)
            elif len(suggestions) < 8:
                suggestions.append(candidate)
            if len(matches) >= sources.MAX_CANDIDATES or exact_path:
                break
        return matches, suggestions


def _discovery_error(exc):
    code = getattr(exc, "code", None)
    if isinstance(exc, FileNotFoundError) or code == "not_found":
        return None
    return code if code else "network"


def _search_repositories(slugs, manifest, normalized, exclude):
    matches, suggestions, errors = [], [], set()
    with ThreadPoolExecutor(max_workers=3) as pool:
        providers = [pool.submit(fetch, slug) for slug in slugs
                     for fetch in (_market_repos, lambda name: sources._github_search_repos(name, _DISCOVERY_REPOS))]
        results = []
        for provider in providers:
            try:
                results.append(provider.result())
            except (ValueError, OSError) as exc:
                results.append([])
                if code := _discovery_error(exc):
                    errors.add(code)
        urls = {}
        for index in range(max(map(len, results), default=0)):
            for result in results:
                if index < len(result):
                    item = result[index]
                    spec = sources._github_spec({"url": item["url"] if isinstance(item, dict) else item})
                    if spec["url"] in exclude:
                        continue
                    entry = urls.setdefault(spec["url"], spec)
                    if isinstance(item, dict):
                        entry["fork"] = item.get("fork", False)
        repos = list(urls.values())[:_DISCOVERY_REPOS]
        checks = [pool.submit(_repository_matches, repo, manifest, normalized, slugs) for repo in repos]
        for check in checks:
            try:
                found, possible = check.result()
                matches.extend(found)
                suggestions.extend(possible)
            except (ValueError, OSError) as exc:
                if code := _discovery_error(exc):
                    errors.add(code)
    return matches[:sources.MAX_CANDIDATES], suggestions, errors


def discover_github(lib, payload, *, confirm=False):
    expected = {"id", "preview_id", "candidate"} if confirm else {"id"}
    if set(payload) != expected:
        raise SkillError("invalid_payload", "Enter a skill ID and a preview selection when confirming")
    sid = sources._string(payload, "id", limit=256)
    state = lib.state()
    row = next((item for item in state["skills"] if item["id"] == sid), None)
    if row is None:
        raise SkillError("not_found", "Unknown skill")
    if confirm:
        if row["external"]:
            raise SkillError("external_skill", "Import a copy before managing its source")
        selected = payload["candidate"]

        def validate(preview):
            if preview["id"] != sid or type(selected) is not int or not 0 <= selected < len(preview["matches"]):
                raise SkillError("invalid_payload", "Select a valid result for this skill")

        preview = sources._claim(lib, payload, "preview_id", "github_discovery", validate)
        incoming = preview["matches"][selected]
        # The preview already verified the immutable commit. Recheck local bytes and source at commit time.
        return {"skill": lib.link_github(sid, incoming, _guard(preview["before"], preview["local_digest"]))}

    path = Path(row["path"])
    read_files = not (row["external"] and row.get("mode") == "link")
    normalized = {}
    manifest = sources._manifest(path, normalized=normalized) if read_files else None
    stored = row["source"]
    tracked = stored if isinstance(stored, dict) and stored.get("kind") == "github" else None
    hint = tracked or Discovery(lib, Path(state["root"])).hint(row)
    slugs = _discovery_names(row, path, read_files)
    matches, suggestions, errors, scanned = [], [], set(), set()
    if hint is not None:
        try:
            matches, suggestions = _repository_matches(
                hint, manifest, normalized, slugs, origin="bound_source" if tracked else "installed_source",
                exact_path=True)
        except (ValueError, OSError) as exc:
            if code := _discovery_error(exc):
                errors.add(code)
    if not matches and read_files:
        for spec in _document_sources(path):
            if hint and (spec["url"], spec["ref"], spec["subdir"]) == (hint["url"], hint["ref"], hint["subdir"]):
                continue
            try:
                found, possible = _repository_matches(
                    spec, manifest, normalized, slugs, origin="documented_path" if spec["subdir"] else "documented_repo",
                    exact_path=bool(spec["subdir"]))
                matches.extend(found)
                suggestions.extend(possible)
                if not spec["subdir"]:
                    scanned.add(spec["url"])
            except (ValueError, OSError) as exc:
                if code := _discovery_error(exc):
                    errors.add(code)
    if not matches and not (hint is not None and suggestions and not read_files):
        found, possible, failed = _search_repositories(slugs, manifest, normalized, scanned)
        matches.extend(found)
        suggestions.extend(possible)
        errors |= failed
    match_by_source = {}
    for candidate in matches:
        match_by_source.setdefault((candidate["url"], candidate["subdir"]), candidate)
    matches = list(match_by_source.values())[:sources.MAX_CANDIDATES]
    seen = set(match_by_source)
    alternatives = []
    for candidate in suggestions:
        key = candidate["url"], candidate["subdir"]
        if key not in seen and len(alternatives) < 8:
            alternatives.append(candidate)
            seen.add(key)
    preview_id = None
    if matches and not row["external"]:
        preview_id = sources._keep(lib, "github_discovery", {
            "id": sid, "before": stored, "matches": matches, "local_digest": sources._digest(manifest)})
    count = len(matches) + len(alternatives)
    status = ("matched" if len(matches) == 1 else "ambiguous") if matches else (
        "ambiguous" if count > 1 else "candidates" if count else "request_failed" if errors else "not_found")
    return {"matches": matches, "suggestions": alternatives, "preview_id": preview_id, "status": status,
            "errors": sorted(errors), "incomplete": bool(errors)}


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
            work = Path(temp).resolve()
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
                        normalized = {}
                        sources._manifest(Path(row["path"]), normalized=normalized)
                        if incoming["content_digest"] == sources._digest(normalized):
                            continue
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

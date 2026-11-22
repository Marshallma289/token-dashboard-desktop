"""Preview and import skill data without executing package contents."""

from __future__ import annotations

import difflib
import hashlib
import http.client
import json
import os
import re
import secrets
import ssl
import stat
import struct
import tempfile
import threading
import time
import unicodedata
import zipfile
import zlib
from contextlib import contextmanager
from copy import deepcopy
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlencode, urljoin, urlsplit
from urllib.error import HTTPError
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener

MiB = 1024 * 1024
MAX_ARCHIVE = 64 * MiB
MAX_TREE = 128 * MiB
MAX_FILE = 24 * MiB
MAX_ENTRIES = 8192
MAX_DEPTH = 32
MAX_CANDIDATES = 512
MAX_SELECTION = 100
TICKET_TTL = 900
_CHUNK = 128 * 1024
_DEVICE = re.compile(r"(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[1-9\u00b9\u00b2\u00b3])", re.I)
_OWNER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9-]{0,38}")
_REPO = re.compile(r"[a-zA-Z0-9_.-]{1,100}")
_SHA = re.compile(r"[0-9a-f]{40}", re.I)
_GROUPS = (frozenset(("api.github.com", "codeload.github.com")), frozenset(("skills.sh", "www.skills.sh")))
_tickets = {}
_ticket_lock = threading.Lock()


class SourceError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _fail(code, message):
    raise SourceError(code, message)


def _part(name):
    if (not name or name in (".", "..") or name[-1] in " ." or len(name) > 255
            or any(c in '<>:"/\\|?*' or unicodedata.category(c) in ("Cc", "Cs", "Cf") for c in name)
            or _DEVICE.fullmatch(name.split(".")[0].rstrip()) or name.casefold() == ".git"):
        _fail("unsafe_path", "A package contains an unsafe path component.")
    return name


def _parts(value):
    if len(value) > 2048 or value.count("/") >= MAX_DEPTH:
        _fail("limit", "Package path exceeds the depth or length limit.")
    return tuple(_part(s) for s in value.split("/"))


def _key(value):
    return unicodedata.normalize("NFC", value).casefold()


def _string(obj, key, default=None, limit=4096):
    value = obj.get(key, default)
    if (not isinstance(value, str) or len(value) > limit
            or any(unicodedata.category(c) in ("Cc", "Cs") for c in value)):
        _fail("invalid_request", f"Invalid {key}.")
    return value


def _local_path(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or value.startswith(("\\\\", "//")):
        _fail("unsafe_path", "Use an absolute local path, not a network or device path.")
    if os.name == "nt":
        import ctypes

        if not re.fullmatch(r"[a-zA-Z]:\\", path.anchor) or ctypes.windll.kernel32.GetDriveTypeW(path.anchor) == 4:
            _fail("unsafe_path", "Use a local drive.")
        for part in path.parts[1:]:
            _part(part)
    return path


def _checked_stat(path, *, leaf=True):
    for parent in reversed(path.parents):
        st = parent.lstat()
        if not stat.S_ISDIR(st.st_mode) or getattr(st, "st_file_attributes", 0) & 0x400:
            _fail("unsafe_path", "Linked directories and reparse points are not allowed.")
    if leaf:
        st = path.lstat()
        if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & 0x400:
            _fail("unsafe_path", "Links and reparse points are not allowed.")
        return st


@contextmanager
def _regular_file(path):
    st = _checked_stat(path)
    if not stat.S_ISREG(st.st_mode):
        _fail("unsafe_path", "Expected a regular file.")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as file:
        opened = os.fstat(file.fileno())
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(st, opened) or not os.path.samestat(_checked_stat(path), opened):
            _fail("source_changed", "File changed while it was being opened.")
        yield file, opened.st_size


def _copy_bounded(src, dst, limit):
    count = 0
    while chunk := src.read(min(_CHUNK, limit - count + 1)):
        count += len(chunk)
        if count > limit:
            _fail("limit", "Content exceeds the allowed size.")
        dst.write(chunk)
    return count


def _zip_directory(file):
    # Bound central directory work before ZipFile builds Python objects for its entries.
    size = file.seek(0, os.SEEK_END)
    if size > MAX_ARCHIVE:
        _fail("limit", "Archive exceeds the input size limit.")
    start = max(0, size - 65557)
    file.seek(start)
    tail = file.read()
    end = tail.rfind(b"PK\x05\x06")
    if end < 0 or len(tail) - end < 22:
        _fail("unsafe_archive", "ZIP directory is missing.")
    _, disk, central_disk, on_disk, count, length, offset, comment = struct.unpack_from("<4s4H2LH", tail, end)
    if count > MAX_ENTRIES or length > 8 * MiB:
        _fail("limit", "ZIP central directory exceeds the allowed size or entry count.")
    if (disk or central_disk or on_disk != count or offset + length != start + end
            or end + 22 + comment != len(tail)):
        _fail("unsafe_archive", "Unsupported or malformed ZIP central directory.")
    file.seek(offset)
    remaining = length
    for _ in range(count):
        header = file.read(46)
        if remaining < 46 or len(header) != 46 or not header.startswith(b"PK\x01\x02"):
            _fail("unsafe_archive", "Invalid ZIP central directory entry.")
        extra = sum(struct.unpack_from("<3H", header, 28))
        remaining -= 46 + extra
        if remaining < 0:
            _fail("unsafe_archive", "ZIP central directory entry exceeds its bounds.")
        file.seek(extra, os.SEEK_CUR)
    if remaining:
        _fail("unsafe_archive", "ZIP entry count does not match its central directory.")


def _unzip(file, target):
    # Check the entire namespace and declared sizes before opening any member.
    try:
        _zip_directory(file)
        with zipfile.ZipFile(file) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ENTRIES:
                _fail("limit", "Archive contains too many entries.")
            paths, entries, total = {}, set(), 0
            for info in infos:
                directory = info.is_dir()
                name = info.orig_filename
                parts = _parts(name[:-1] if directory else name)
                mode = stat.S_IFMT(info.external_attr >> 16)
                if (mode not in (0, stat.S_IFDIR if directory else stat.S_IFREG)
                        or info.external_attr & 0x400 or (directory and info.file_size)
                        or info.flag_bits & 1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                    _fail("unsafe_archive", "Links, special files, encryption and unsupported compression are not allowed.")
                if parts in entries:
                    _fail("unsafe_archive", "Archive contains duplicate paths.")
                entries.add(parts)
                for index in range(1, len(parts) + 1):
                    prefix = parts[:index]
                    canonical = tuple(map(_key, prefix))
                    value = (prefix, directory or index < len(parts))
                    if paths.setdefault(canonical, value) != value:
                        _fail("unsafe_archive", "Archive paths collide by case, Unicode spelling, or file type.")
                total += info.file_size
                if (len(paths) > MAX_ENTRIES or total > MAX_TREE or info.file_size > MAX_FILE
                        or (info.file_size >= MiB and info.file_size > 250 * max(1, info.compress_size))):
                    _fail("limit", "Archive exceeds entry, expansion, or compression ratio limits.")
            target.mkdir()
            for info in infos:
                dest = target.joinpath(*info.orig_filename.rstrip("/").split("/"))
                if info.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as src, dest.open("xb") as dst:
                        if _copy_bounded(src, dst, info.file_size) != info.file_size:
                            _fail("unsafe_archive", "Archive entry size does not match its header.")
    except (zipfile.BadZipFile, NotImplementedError, EOFError, zlib.error, UnicodeError) as exc:
        raise SourceError("unsafe_archive", "Invalid or damaged ZIP archive.") from exc


def _walk(root, *, stop_at_skill=False):
    pending = [(root, "")]
    visited = 0
    while pending:
        directory, rel = pending.pop()
        if not stat.S_ISDIR(_checked_stat(directory).st_mode):
            _fail("unsafe_path", "Expected a directory.")
        files, dirs, seen = [], [], set()
        with os.scandir(directory) as entries:
            for entry in entries:
                visited += 1
                if visited > MAX_ENTRIES:
                    _fail("limit", "Source contains too many entries.")
                if entry.name.casefold() == ".git":
                    continue
                name = _part(entry.name)
                if _key(name) in seen:
                    _fail("unsafe_path", "Source paths collide by case or Unicode spelling.")
                seen.add(_key(name))
                child = f"{rel}/{name}" if rel else name
                _parts(child)
                st = entry.stat(follow_symlinks=False)
                if getattr(st, "st_file_attributes", 0) & 0x400:
                    _fail("unsafe_path", "Source contains a reparse point.")
                if stat.S_ISDIR(st.st_mode):
                    dirs.append((Path(entry.path), child))
                elif stat.S_ISREG(st.st_mode):
                    files.append((Path(entry.path), child))
                else:
                    _fail("unsafe_path", "Source contains links or special files.")
        is_skill = any(path.name == "SKILL.md" for path, _ in files)
        yield directory, rel, files, is_skill
        if not (stop_at_skill and is_skill):
            pending.extend(sorted(dirs, key=lambda item: item[1], reverse=True))


@contextmanager
def _snapshot_file(root, rel):
    if root is None:
        yield None
    else:
        path = root.joinpath(*rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as file:
            yield file


def _manifest(root, dest=None, budget=MAX_TREE):
    files, total = {}, 0
    for _, _, entries, _ in _walk(root):
        for path, rel in entries:
            h = hashlib.sha256()
            with _regular_file(path) as (file, size):
                if size > MAX_FILE or total + size > min(MAX_TREE, budget):
                    _fail("limit", "Skill exceeds the file or total size limit.")
                count = 0
                with _snapshot_file(dest, rel) as out:
                    while chunk := file.read(min(_CHUNK, size - count + 1)):
                        count += len(chunk)
                        if count > size:
                            _fail("source_changed", "File grew while being read.")
                        h.update(chunk)
                        if out is not None:
                            out.write(chunk)
                if count != size:
                    _fail("source_changed", "File size changed while being read.")
            total += size
            files[rel] = (h.hexdigest(), size)
    if "SKILL.md" not in files:
        _fail("no_skill", "The selected directory has no SKILL.md.")
    return files


def _size(manifest):
    return sum(size for _, size in manifest.values())


def _digest(manifest):
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("ascii")).hexdigest()


def _repo_name(owner, repo):
    if not _OWNER.fullmatch(owner) or not _REPO.fullmatch(repo) or repo in (".", ".."):
        _fail("invalid_url", "Invalid GitHub owner or repository.")
    return owner.lower(), repo.lower()


def _github_spec(payload):
    try:
        p = urlsplit(_string(payload, "url"))
        segments = [unquote(s, errors="strict") for s in p.path.strip("/").split("/")]
    except (ValueError, UnicodeError) as exc:
        raise SourceError("invalid_url", "Invalid GitHub URL.") from exc
    if (p.scheme != "https" or p.netloc.lower() != "github.com" or p.query or p.fragment
            or not p.path.startswith("/") or len(segments) < 2):
        _fail("invalid_url", "Use an HTTPS github.com repository or tree URL.")
    owner, repo = _repo_name(segments[0], segments[1].removesuffix(".git"))
    ref, subdir = _string(payload, "ref", "", 256), _string(payload, "subdir", "", 2048)
    if len(segments) > 2:
        if (len(segments) < 4 or segments[2] not in ("tree", "blob") or ref or subdir
                or (segments[2] == "blob" and (len(segments) < 5 or segments[-1] != "SKILL.md"))):
            _fail("invalid_url", "Use a GitHub tree URL, a SKILL.md URL, or a repository URL with ref and subdir.")
        ref = segments[3]
        tail = segments[4:-1] if segments[2] == "blob" else segments[4:]
        subdir = "/".join(_part(part) for part in tail)
    if ref and (len(ref) > 256 or any(not re.fullmatch(r"[\w.+@-]+", s) or s in (".", "..") for s in ref.split("/"))):
        _fail("invalid_url", "Invalid GitHub ref.")
    if subdir:
        _parts(subdir)
    return {"kind": "github", "url": f"https://github.com/{owner}/{repo}", "ref": ref, "subdir": subdir}


def _network_url(url, hosts):
    try:
        p = urlsplit(url)
    except ValueError as exc:
        raise SourceError("network_target", "Invalid network target.") from exc
    if (p.scheme != "https" or p.netloc.lower() not in hosts or p.fragment or not p.path.startswith("/")
            or any(ord(c) < 33 or ord(c) > 126 or c == "\\" for c in url)):
        _fail("network_target", "Network target is not on this provider's HTTPS allowlist.")
    allowed = {
        "api.github.com": r"/(?:repos/[\w.-]+/[\w.-]+|repositories/[0-9]+)/commits/?",
        "codeload.github.com": r"/[\w.-]+/[\w.-]+/(?:legacy\.zip|zip)/[0-9a-fA-F]{40}/?",
        "skills.sh": r"/(?:hot/?|trending/?|api/search/?)?",
        "www.skills.sh": r"/(?:hot/?|trending/?|api/search/?)?",
    }
    if not re.fullmatch(allowed[p.hostname], p.path, re.ASCII):
        _fail("network_target", "Network endpoint is not on the provider's allowlist.")
    return p


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _fetch(url, limit, output=None):
    hosts = next((group for group in _GROUPS if urlsplit(url).netloc.lower() in group), frozenset())
    deadline = time.monotonic() + 25
    # Use OS/environment routing, including fake-IP DNS, but authenticate the fixed origin with TLS.
    opener = build_opener(ProxyHandler(), HTTPSHandler(context=ssl.create_default_context()), _NoRedirect())
    try:
        for _ in range(4):
            _network_url(url, hosts)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _fail("network", "Request timed out.")
            request = Request(url, headers={"User-Agent": "SkillLibrary/1.0", "Accept-Encoding": "identity"})
            try:
                response = opener.open(request, timeout=min(8, remaining))
            except HTTPError as exc:
                response = exc
            with response:
                status = response.getcode()
                location = response.headers.get("Location")
                if status in (301, 302, 303, 307, 308) and location:
                    if any(ord(c) < 33 or c == "\\" for c in location):
                        _fail("network_target", "Invalid redirect location.")
                    url = urljoin(url, location)
                    continue
                if status != 200:
                    _fail("not_found" if status == 404 else "network", f"Provider returned HTTP {status}.")
                length = response.headers.get("Content-Length")
                if length and (not re.fullmatch(r"[0-9]{1,12}", length) or int(length) > limit):
                    _fail("limit", "Provider response exceeds the download limit.")
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    _fail("network", "Provider returned unsupported content encoding.")
                data, count = bytearray(), 0
                while True:
                    if time.monotonic() >= deadline:
                        _fail("network", "Download timed out.")
                    chunk = response.read1(min(_CHUNK, limit - count + 1))
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > limit:
                        _fail("limit", "Provider response exceeds the download limit.")
                    if output is None:
                        data.extend(chunk)
                    else:
                        output.write(chunk)
                if length and count != int(length):
                    _fail("network", "Provider response was incomplete.")
                return bytes(data) if output is None else None
    except (OSError, http.client.HTTPException) as exc:
        raise SourceError("network", "Could not read the remote provider.") from exc
    _fail("network_target", "Too many redirects.")


def _json(raw):
    try:
        return json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise SourceError("provider_format", "Provider returned invalid JSON.") from exc


def _github_head(source):
    repo = source["url"].split("github.com/", 1)[1]
    params = {"per_page": 1}
    if source["ref"]:
        params["sha"] = source["ref"]
    commits = _json(_fetch(f"https://api.github.com/repos/{repo}/commits?{urlencode(params)}", MiB))
    revision = commits[0].get("sha") if isinstance(commits, list) and commits and isinstance(commits[0], dict) else None
    if not isinstance(revision, str) or not _SHA.fullmatch(revision):
        _fail("provider_format", "GitHub did not return a valid commit SHA.")
    return revision.lower()


def _github_checkout(source, work, revision=None):
    repo = source["url"].split("github.com/", 1)[1]
    if revision is None:
        revision = _github_head(source)
    if not isinstance(revision, str) or not _SHA.fullmatch(revision):
        _fail("provider_format", "GitHub did not return a valid commit SHA.")
    archive = work / "download.zip"
    with archive.open("xb") as dst:
        _fetch(f"https://codeload.github.com/{repo}/zip/{revision}", MAX_ARCHIVE, dst)
    target = work / "unpacked"
    with archive.open("rb") as file:
        _unzip(file, target)
    wrappers = list(target.iterdir())
    if len(wrappers) != 1 or not wrappers[0].is_dir():
        _fail("unsafe_archive", "GitHub archive must have one top-level directory.")
    return wrappers[0], revision.lower()


class _MarketScripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.mode = None
        self.rows = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.mode = "next" if dict(attrs).get("id") == "__NEXT_DATA__" else "flight"

    def handle_endtag(self, tag):
        if tag == "script":
            self.mode = None

    def handle_data(self, data):
        if self.mode == "next":
            try:
                props = _json(data)["props"]["pageProps"]
                for key in ("initialSkills", "skills", "items"):
                    if isinstance(props.get(key), list):
                        self.rows.extend(props[key])
                        break
            except (SourceError, KeyError, TypeError, AttributeError):
                pass
        elif self.mode == "flight" and "self.__next_f.push(" in data:
            for match in re.finditer(r"self\.__next_f\.push\(\s*", data):
                try:
                    payload, _ = json.JSONDecoder().raw_decode(data, match.end())
                except (ValueError, RecursionError):
                    continue
                if not isinstance(payload, list) or len(payload) < 2 or not isinstance(payload[1], str):
                    continue
                for obj in re.finditer(r'\{[^{}]{1,2048}\}', payload[1]):
                    if '"source"' in obj[0] and '"installs"' in obj[0]:
                        try:
                            self.rows.append(json.loads(obj[0]))
                        except ValueError:
                            continue


def _market(payload):
    query = _string(payload, "query", "", 200).strip()
    board = _string(payload, "board", "hot", 16)
    boards = {"hot": "/hot", "trending": "/trending", "all": "/", "all_time": "/"}
    limit = payload.get("limit", 30)
    if type(limit) is not int or not 1 <= limit <= 50 or board not in boards:
        _fail("invalid_request", "Invalid market board or result limit.")
    if query:
        params = urlencode({"q": query, "limit": limit})
        result = _json(_fetch(f"https://skills.sh/api/search?{params}", 3 * MiB))
        rows = result.get("skills") if isinstance(result, dict) else result
        if not isinstance(rows, list):
            _fail("provider_format", "Unrecognized market search response.")
    else:
        parser = _MarketScripts()
        parser.feed(_fetch("https://skills.sh" + boards[board], 8 * MiB).decode("utf-8", "replace"))
        parser.close()
        rows = parser.rows
    items = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("source"), str):
            continue
        parts = row["source"].split("/")
        slug = row.get("skillId", row.get("skill_id", row.get("id")))
        if len(parts) != 2 or not isinstance(slug, str) or not re.fullmatch(r"[\w.-]{1,128}", slug):
            continue
        try:
            owner, repo = _repo_name(*parts)
            _part(slug)
        except SourceError:
            continue
        source = f"{owner}/{repo}"
        item_id = f"{source}/{slug}"
        installs = row.get("installs", 0)
        name = row.get("name")
        items.setdefault(item_id, {"id": item_id, "skill_id": slug, "name": name[:200] if isinstance(name, str) and name else slug,
                                   "source": source, "installs": installs if type(installs) is int and 0 <= installs < 2**53 else 0,
                                   "url": "https://github.com/" + source})
        if len(items) >= limit:
            break
    if not query and not items:
        _fail("provider_format", "No readable leaderboard entries were returned.")
    return {"items": list(items.values())}


def _archive_checkout(path, work):
    target = work / "unpacked"
    with _regular_file(path) as (file, size):
        if size > MAX_ARCHIVE:
            _fail("limit", "Archive exceeds the download size limit.")
        _unzip(file, target)
    return target


def _checkout(source, work, revision=None):
    kind = source["kind"]
    if kind == "github":
        return _github_checkout(source, work, revision)
    path = _local_path(source["path"])
    if kind == "archive":
        return _archive_checkout(path, work), None
    if path.name == "SKILL.md" and stat.S_ISREG(_checked_stat(path).st_mode):
        path = path.parent
    return path, None


def _subdir(root, rel):
    return root.joinpath(*_parts(rel)) if rel else root


def _metadata(root, fallback):
    from skills_core import metadata

    with _regular_file(root / "SKILL.md") as (file, _):
        text = file.read(16 * 1024).decode("utf-8-sig", "replace")
    info = metadata(fallback, text)
    return info["name"][:128], info["description"][:600]


def _fallback(source, rel, root):
    if rel:
        return rel.rsplit("/", 1)[-1]
    if source["kind"] == "github":
        return source["url"].rsplit("/", 1)[-1]
    if source["kind"] == "archive":
        return Path(source["path"]).stem
    return root.name or "skill"


def _scope(lib):
    return os.path.normcase(str(Path(lib.data_dir).resolve())), str(lib.agent)


def _keep(lib, kind, data):
    bound = _scope(lib)
    now = time.monotonic()
    token = secrets.token_urlsafe(24)
    with _ticket_lock:
        for key, entry in list(_tickets.items()):
            if entry[0] <= now:
                del _tickets[key]
        if len(_tickets) >= 32:
            del _tickets[next(iter(_tickets))]
        _tickets[token] = (now + TICKET_TTL, bound, kind, deepcopy(data))
    return token


def _claim(lib, payload, field, kind, validate):
    token = _string(payload, field, limit=80)
    bound = _scope(lib)
    with _ticket_lock:
        row = _tickets.get(token)
        if not row or row[0] <= time.monotonic() or row[1] != bound or row[2] != kind:
            _fail("stale_preview", "Preview has expired or does not belong to this library. Preview again.")
        data = row[3]
        validate(data)
        del _tickets[token]
        return data


def _preview(lib, op, payload, work):
    if op == "github_preview":
        source = _github_spec(payload)
        filter_slug = _string(payload, "skill_id", "", 128).casefold()
    else:
        path = _local_path(_string(payload, "path"))
        kind = "local" if op == "local_preview" else "archive"
        if kind == "archive" and path.suffix.lower() not in (".zip", ".skill"):
            _fail("invalid_request", "Expected a .zip or .skill archive.")
        source, filter_slug = {"kind": kind, "path": str(path), "subdir": ""}, ""
    root, revision = _checkout(source, work)
    start = source["subdir"]
    budget, candidates = MAX_TREE, []
    for directory, inner, _, is_skill in _walk(_subdir(root, start), stop_at_skill=not filter_slug):
        if not is_skill:
            continue
        rel = "/".join(x for x in (start, inner) if x)
        fallback = _fallback(source, rel, root)
        name, description = _metadata(directory, fallback)
        if filter_slug and filter_slug not in (fallback.casefold(), name.casefold()):
            continue
        if len(candidates) >= MAX_CANDIDATES:
            _fail("limit", "Too many skills; select a narrower directory.")
        manifest = _manifest(directory, budget=budget)
        budget -= _size(manifest)
        actual_source = dict(source, subdir=rel, digest=_digest(manifest))
        if revision:
            actual_source["revision"] = revision
        candidate_id = hashlib.sha256(rel.encode("utf-8")).hexdigest()[:24]
        candidates.append({"id": candidate_id, "name": name, "description": description,
                           "path": rel or ".", "source": actual_source})
    preview_id = _keep(lib, "import", (source, revision, {c["id"]: c for c in candidates})) if candidates else None
    return {"preview_id": preview_id, "candidates": candidates}


def _ids(payload):
    values = payload.get("ids")
    if (not isinstance(values, list) or not 1 <= len(values) <= MAX_SELECTION
            or any(not isinstance(value, str) or not value or len(value) > 256 for value in values)
            or len(set(values)) != len(values)):
        _fail("invalid_selection", f"Select between 1 and {MAX_SELECTION} distinct IDs.")
    return values


def _stage(work, index, name):
    # Keep a reasonable basename for the core's fallback naming, even for odd archive basenames.
    try:
        name = _part(name)
    except SourceError:
        name = "skill"
    path = work / f"stage-{index}" / name
    path.mkdir(parents=True)
    return path


def _error(exc):
    if isinstance(exc, SourceError):
        code, message = exc.code, str(exc)
    elif isinstance(exc, FileNotFoundError):
        code, message = "not_found", "The file, directory or skill no longer exists."
    elif isinstance(exc, FileExistsError):
        code, message = "already_exists", "The destination already exists."
    elif isinstance(exc, OSError):
        code, message = "io_error", "Unable to read or write the requested content."
    else:
        code, message = getattr(exc, "code", "library_error"), str(exc)[:500] or "Library operation failed."
    return {"code": str(code), "error": message}


def _import_selected(lib, payload, work):
    ids = _ids(payload)

    def validate(ticket):
        if any(cid not in ticket[2] for cid in ids):
            _fail("invalid_selection", "Selection includes a candidate outside this preview.")

    source, revision, candidates = _claim(lib, payload, "preview_id", "import", validate)
    root, _ = _checkout(source, work, revision)
    staged, budget = [], MAX_TREE
    for index, cid in enumerate(ids):
        actual_source = candidates[cid]["source"]
        rel = actual_source["subdir"]
        dest = _stage(work, index, _fallback(source, rel, root))
        manifest = _manifest(_subdir(root, rel), dest, budget)
        budget -= _size(manifest)
        if _digest(manifest) != actual_source["digest"]:
            _fail("source_changed", "Selected content changed after preview. Preview again.")
        staged.append((cid, dest, actual_source))
    imported, errors = [], []
    for cid, path, actual_source in staged:
        try:
            imported.append({"id": cid, "skill": lib.import_path(path, source=dict(actual_source))})
        except (ValueError, OSError, RuntimeError) as exc:
            errors.append({"id": cid, **_error(exc)})
    return {"imported": imported, "errors": errors}


def _records(lib):
    rows = lib.state()["skills"]
    if isinstance(rows, dict):
        return rows
    return {row["id"]: row for row in rows}


def _record(lib, sid):
    row = _records(lib).get(sid)
    if row is None:
        _fail("not_found", "Skill not found in this library.")
    return row


def _stored(row):
    source = row.get("source")
    if not isinstance(source, dict) or source.get("kind") not in ("archive", "local", "github"):
        _fail("unsupported", "This skill has no updateable source.")
    if not re.fullmatch(r"[0-9a-f]{64}", _string(source, "digest", "", 64)):
        _fail("unsupported", "The installed skill has no comparable source snapshot.")
    rel = _string(source, "subdir", "", 2048)
    if rel:
        _parts(rel)
    if source["kind"] == "github":
        _github_spec(source)
    else:
        _local_path(_string(source, "path"))
    return dict(source)


def _comparison(lib, sid, row, work, *, snapshot=False, checkout=None):
    source = _stored(row)
    current = _local_path(str(lib.skill_path(sid)))
    old_path = work / "current" if snapshot else None
    old = _manifest(current, old_path)
    root, revision = (checkout(source, work) if checkout else _checkout(source, work))
    source_root = _subdir(root, source["subdir"])
    new_path = work / "proposed" if snapshot else None
    new = _manifest(source_root, new_path)
    incoming = dict(source, digest=_digest(new))
    if revision:
        incoming["revision"] = revision
    return source, incoming, old, new, old_path, new_path


def _check_updates(lib, payload, work):
    rows = _records(lib)
    ids = _ids(payload) if "ids" in payload else list(rows)[:MAX_SELECTION]
    updates, cache = [], {}

    def checkout(source, directory):
        kind = source["kind"]
        if kind == "local":
            return _checkout(source, directory)
        key = (kind, source.get("url", source.get("path")), source.get("ref"))
        if key not in cache:
            if len(cache) >= 4:
                _fail("limit", "Check at most four distinct archives or GitHub refs per request.")
            try:
                cache[key] = _checkout(source, directory)
            except (SourceError, OSError) as exc:
                cache[key] = exc
        result = cache[key]
        if isinstance(result, Exception):
            raise result
        return result

    for index, sid in enumerate(ids):
        try:
            directory = work / str(index)
            directory.mkdir()
            row = rows.get(sid) or _record(lib, sid)
            source, incoming, old, new, _, _ = _comparison(lib, sid, row, directory, checkout=checkout)
            local_modified = bool(row.get("modified")) or _digest(old) != source["digest"]
            available = incoming["digest"] != source["digest"] and old != new
            updates.append({"id": sid, "status": "available" if available else "unchanged", "local_modified": local_modified,
                            "revision": incoming.get("revision")})
        except (ValueError, OSError, RuntimeError) as exc:
            item = _error(exc)
            updates.append({"id": sid, "status": "unsupported" if item["code"] == "unsupported" else "error", **item})
    return {"updates": updates, "truncated": "ids" not in payload and len(rows) > MAX_SELECTION}


def _diff_text(root, rel, entry):
    if entry is None:
        return [], False, False
    if entry[1] > 32 * 1024:
        return [], False, True
    raw = _subdir(root, rel).read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        return [], True, False
    if "\x00" in text:
        return [], True, False
    lines = text.splitlines(keepends=True)
    return lines, False, len(lines) > 400


def _changes(old, new, old_path, new_path):
    changed = sorted(path for path in old.keys() | new.keys() if old.get(path) != new.get(path))
    rows, budget = [], 256 * 1024
    for path in changed[:300]:
        status = "added" if path not in old else "deleted" if path not in new else "modified"
        row = {"path": path, "status": status}
        before, former_binary, former_large = _diff_text(old_path, path, old.get(path))
        after, latter_binary, latter_large = _diff_text(new_path, path, new.get(path))
        if former_binary or latter_binary:
            row["binary"] = True
        elif former_large or latter_large or not budget:
            row["omitted"] = True
        else:
            diff = "".join(difflib.unified_diff(before, after, "a/" + path, "b/" + path, lineterm="\n"))
            size = len(diff.encode("utf-8"))
            if size > min(budget, 32 * 1024):
                row["omitted"] = True
            else:
                row["diff"] = diff
                budget -= size
        rows.append(row)
    return rows, len(changed) > len(rows) or any(row.get("omitted") for row in rows)


def _update_preview(lib, payload, work):
    sid = _string(payload, "id", limit=256)
    row = _record(lib, sid)
    source, incoming, old, new, old_path, new_path = _comparison(lib, sid, row, work, snapshot=True)
    changes, truncated = _changes(old, new, old_path, new_path)
    base = _digest(old)
    token = _keep(lib, "update", (sid, source, base, incoming)) if changes else None
    return {"id": sid, "update_token": token, "local_modified": bool(row.get("modified")) or base != source["digest"],
            "changes": changes, "truncated": truncated, "revision": incoming.get("revision")}


def _apply_update(lib, payload, work):
    sid = _string(payload, "id", limit=256)
    if payload.get("confirm") is not True:
        _fail("confirmation_required", "Review the changes and explicitly confirm the update.")
    replace = getattr(lib, "replace_skill", None)
    if not callable(replace):
        _fail("unsupported", "This version of the library cannot replace skills safely.")

    def validate(ticket):
        if ticket[0] != sid:
            _fail("invalid_selection", "Update preview belongs to a different skill.")

    _, before, base, incoming = _claim(lib, payload, "update_token", "update", validate)
    root, _ = _checkout(incoming, work, incoming.get("revision"))
    rel = incoming["subdir"]
    staged = _stage(work, 0, _fallback(incoming, rel, root))
    if _digest(_manifest(_subdir(root, rel), staged)) != incoming["digest"]:
        _fail("source_changed", "The source changed after preview. Preview again.")

    def validate_current(current_path, current_record):
        if current_record.get("source") != before or _digest(_manifest(Path(current_path))) != base:
            _fail("stale_preview", "The installed skill changed after preview. Preview again.")

    return {"id": sid, "skill": replace(sid, staged, source=incoming, validate_current=validate_current)}


def _export(lib, payload, work):
    sid = _string(payload, "id", limit=256)
    target = _local_path(_string(payload, "path"))
    if target.suffix.lower() not in (".zip", ".skill"):
        _fail("invalid_request", "Export to a .zip or .skill file.")
    _checked_stat(target, leaf=False)
    if os.path.lexists(target):
        _fail("already_exists", "The export destination already exists.")
    current = _local_path(str(lib.skill_path(sid)))
    if target.is_relative_to(current):
        _fail("unsafe_path", "Export outside the skill directory.")
    snapshot = work / "export"
    manifest = _manifest(current, snapshot, MAX_ARCHIVE)
    # Stored entries keep export/import symmetric under the compression ratio limit.
    name = _part(current.name)
    with tempfile.NamedTemporaryFile(prefix=".skill-export-", dir=target.parent, delete=False) as file:
        prepared = Path(file.name)
        try:
            with zipfile.ZipFile(file, "w", compression=zipfile.ZIP_STORED) as archive:
                for rel in sorted(manifest):
                    _parts(f"{name}/{rel}")
                    archive.write(_subdir(snapshot, rel), f"{name}/{rel}")
            if file.tell() > MAX_ARCHIVE:
                _fail("limit", "Export exceeds the archive size limit.")
            file.close()
            _checked_stat(target, leaf=False)
            if os.name == "nt":
                os.rename(prepared, target)  # Windows rename fails if target already exists.
            else:
                os.link(prepared, target)
        finally:
            file.close()
            prepared.unlink(missing_ok=True)
    return {"id": sid, "path": str(target)}


def handle(lib, op, payload):
    """Dispatch a source operation. A successful batch may contain per-item errors."""
    try:
        if not isinstance(op, str) or not isinstance(payload, dict):
            _fail("invalid_request", "Expected an operation and a JSON object.")
        if op == "market":
            return {"ok": True, **_market(payload)}
        preview = op in ("local_preview", "archive_preview", "github_preview")
        handlers = {"import_selected": _import_selected, "check_updates": _check_updates,
                    "update_preview": _update_preview, "apply_update": _apply_update, "export": _export}
        if not preview and op not in handlers:
            _fail("unknown_operation", "Unrecognized source operation.")
        with tempfile.TemporaryDirectory(prefix="skills-source-") as temp:
            work = Path(temp).resolve()
            result = _preview(lib, op, payload, work) if preview else handlers[op](lib, payload, work)
        return {"ok": True, **result}
    except (ValueError, OSError, RuntimeError) as exc:
        return {"ok": False, **_error(exc)}

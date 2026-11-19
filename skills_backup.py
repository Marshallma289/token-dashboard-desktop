"""Agent-local recovery snapshots and explicit Git sync."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import unicodedata
from urllib.parse import urlsplit
import uuid
import zipfile
import zlib


_MAX_FILES = 10_000
_MAX_FILE = 32 * 1024 * 1024
_MAX_TOTAL = 128 * 1024 * 1024
_MAX_ARCHIVE = _MAX_TOTAL + 16 * 1024 * 1024
_MAX_MANIFEST = 16 * 1024
_ID = re.compile(r"[0-9a-f]{32}\Z")
_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_AGENT = re.compile(r"[a-z][a-z0-9_-]{0,39}\Z")
_GH_REMOTE = re.compile(r"https://github\.com/[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]+\.git\Z")
_BAD_PART = re.compile(r'[\x00-\x1f\x7f<>:"\\|?*]')
_DEVICE = re.compile(r"(?:con|prn|aux|nul|com[0-9\xb9\xb2\xb3]|lpt[0-9\xb9\xb2\xb3])(?:\.|$)", re.I)
_PRIVATE = {".git", ".env", ".ssh", ".aws", ".netrc", ".npmrc", ".pypirc", "auth.json", "token.json",
            "tokens.json", "credentials.json", "secrets.json", "id_rsa", "id_ed25519"}
_OP_FIELDS = {
    "backup_create": (), "backup_list": (), "backup_restore": ("backup_id", "confirm"),
    "sync_status": (), "sync_connect": ("remote_url", "credential_helper", "confirm"),
    "sync_push": ("confirm",), "sync_pull": ("confirm",),
    "sync_resolve": ("strategy", "expected_remote_revision", "expected_local_digest", "confirm"),
}
_STRATEGIES = ("keep_local", "use_remote", "keep_both")
_HISTORY_DEPTH = 64


class _Error(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code = code
        self.details = details


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _decode(data):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate JSON key")
            value[key] = item
        return value

    return json.loads(data.decode("utf-8"), object_pairs_hook=unique)


def _invalid_archive():
    return _Error("invalid_archive", "The snapshot is invalid or exceeds safety limits.")


def _validate_files(sizes):
    if len(sizes) > _MAX_FILES:
        raise _invalid_archive()
    aliases, leaves, parents = {}, set(), set()
    total = 0
    for name, size in sizes.items():
        if not isinstance(name, str) or len(name) > 1024:
            raise _invalid_archive()
        parts = name.split("/")
        if len(parts) > 32:
            raise _invalid_archive()
        canonical = []
        for i, part in enumerate(parts):
            lower = unicodedata.normalize("NFC", part).casefold()
            if (not part or len(part.encode("utf-8")) > 255 or part in {".", ".."}
                    or part.endswith((" ", ".")) or _BAD_PART.search(part) or _DEVICE.match(part)
                    or lower in _PRIVATE or lower.startswith(".env.") or lower.endswith((".pem", ".key"))):
                raise _invalid_archive()
            canonical.append(lower)
            key = tuple(canonical)
            if aliases.setdefault(key, part) != part:
                raise _invalid_archive()
            if i + 1 == len(parts):
                if key in parents or key in leaves:
                    raise _invalid_archive()
                leaves.add(key)
            else:
                if key in leaves:
                    raise _invalid_archive()
                parents.add(key)
        total += size
        if not 0 <= size <= _MAX_FILE or total > _MAX_TOTAL:
            raise _invalid_archive()


def _digest(files):
    return hashlib.sha256(_json({name: hashlib.sha256(data).hexdigest()
                                 for name, data in files.items()})).hexdigest()


def _portable_scope(files):
    """Keep device-local configuration out even if a non-client built the archive."""
    try:
        manifest = _decode(files["manifest.json"])
        if (not isinstance(manifest, dict) or set(manifest) != {"schema", "skills", "presets"}
                or type(manifest["schema"]) is not int or manifest["schema"] != 1
                or not isinstance(manifest["skills"], list) or not isinstance(manifest["presets"], list)):
            raise _invalid_archive()
        ids = set()
        for item in manifest["skills"]:
            if (not isinstance(item, dict) or set(item) != {"id", "tags", "dirs", "executable"}
                    or not isinstance(item["id"], str) or "/" in item["id"] or item["id"] in ids
                    or any(not isinstance(item[key], list) or not all(isinstance(v, str) for v in item[key])
                           for key in ("tags", "dirs", "executable"))):
                raise _invalid_archive()
            skill_id = item["id"]
            prefix = f"skills/{skill_id}/"
            if prefix + "SKILL.md" not in files or any(prefix + name not in files for name in item["executable"]):
                raise _invalid_archive()
            _validate_files({prefix + name + "/placeholder": 0 for name in item["dirs"]})
            ids.add(skill_id)
        for name in files.keys() - {"manifest.json"}:
            parts = name.split("/")
            if len(parts) < 3 or parts[0] != "skills" or parts[1] not in ids:
                raise _invalid_archive()
        preset_names = set()
        for preset in manifest["presets"]:
            if (not isinstance(preset, dict) or set(preset) != {"name", "ids"}
                    or not isinstance(preset["name"], str) or not preset["name"].strip()
                    or preset["name"].strip() != preset["name"] or preset["name"] in preset_names
                    or not isinstance(preset["ids"], list)
                    or not all(isinstance(item, str) and item in ids for item in preset["ids"])):
                raise _invalid_archive()
            preset_names.add(preset["name"])
    except (KeyError, ValueError, RecursionError) as exc:
        raise _invalid_archive() from exc


def _pack(files, manifest):
    _validate_files({name: len(data) for name, data in files.items()})
    sink = io.BytesIO()
    with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=False) as archive:
        for name, data in [("manifest.json", _json(manifest)),
                           *(("files/" + name, files[name]) for name in sorted(files))]:
            entry = zipfile.ZipInfo(name)
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | 0o600) << 16
            archive.writestr(entry, data, compress_type=zipfile.ZIP_DEFLATED)
    return sink.getvalue()


def _unpack(data, kind, agent, *, library_id=None, backup_id=None):
    if len(data) > _MAX_ARCHIVE:
        raise _invalid_archive()
    try:
        with zipfile.ZipFile(io.BytesIO(data), "r") as archive:
            entries = archive.infolist()
            if not 1 <= len(entries) <= _MAX_FILES + 1:
                raise _invalid_archive()
            manifest_entry = None
            indexed = {}
            for entry in entries:
                name = entry.filename
                mode = stat.S_IFMT(entry.external_attr >> 16)
                if (entry.orig_filename != name or mode not in {0, stat.S_IFREG}
                        or entry.external_attr & 0x410 or entry.flag_bits & 1
                        or entry.compress_type not in {zipfile.ZIP_DEFLATED, zipfile.ZIP_STORED}):
                    raise _invalid_archive()
                if name == "manifest.json" and manifest_entry is None and entry.file_size <= _MAX_MANIFEST:
                    manifest_entry = entry
                elif name.startswith("files/") and name[6:] not in indexed:
                    indexed[name[6:]] = entry
                else:
                    raise _invalid_archive()
            if manifest_entry is None:
                raise _invalid_archive()
            _validate_files({name: entry.file_size for name, entry in indexed.items()})
            manifest = _decode(archive.read(manifest_entry))
            if (not isinstance(manifest, dict) or type(manifest.get("version")) is not int
                    or manifest["version"] != 1 or manifest.get("kind") != kind):
                raise _invalid_archive()
            if manifest.get("agent") != agent or (kind == "backup" and manifest.get("library_id") != library_id):
                raise _Error("foreign_backup" if kind == "backup" else "foreign_snapshot",
                             "The snapshot belongs to a different agent or library.")
            keys = {"version", "kind", "agent", "digest"}
            if kind == "backup":
                keys |= {"library_id", "backup_id", "created_at", "reason"}
                if (manifest.get("backup_id") != backup_id or manifest.get("reason") not in
                        {"manual", "before_restore", "before_pull", "conflict_local", "conflict_remote"}
                        or not isinstance(manifest.get("created_at"), str)
                        or not manifest["created_at"].endswith("Z")):
                    raise _invalid_archive()
                datetime.fromisoformat(manifest["created_at"].replace("Z", "+00:00"))
            if set(manifest) != keys:
                raise _invalid_archive()
            files = {name: archive.read(entry) for name, entry in indexed.items()}
            if manifest["digest"] != _digest(files):
                raise _invalid_archive()
            _portable_scope(files)
            return manifest, files
    except (zipfile.BadZipFile, zlib.error, ValueError, TypeError, RecursionError,
            RuntimeError, EOFError, NotImplementedError) as exc:
        raise _invalid_archive() from exc


def _plain(path, *, directory=False, missing=False):
    try:
        st = path.lstat()
    except FileNotFoundError:
        if missing:
            return False
        raise
    proper_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not proper_type(st.st_mode) or getattr(st, "st_file_attributes", 0) & 0x400:
        raise _Error("unsafe_path", "The backup storage path is not a regular local path.")
    return True


def _directory(parent, name):
    path = parent / name
    if not _plain(path, directory=True, missing=True):
        path.mkdir(exist_ok=True)
        _plain(path, directory=True)
    return path


def _read(path, limit):
    _plain(path)
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise _invalid_archive()
    return data


def _atomic(path, data):
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _backup_record(path, manifest, files):
    return {"backup_id": manifest["backup_id"], "created_at": manifest["created_at"],
            "reason": manifest["reason"], "file_count": len(files), "size_bytes": path.stat().st_size}


def _snapshot(root, agent, library_id, files, reason):
    backup_id = uuid.uuid4().hex
    manifest = {"version": 1, "kind": "backup", "agent": agent, "library_id": library_id,
                "backup_id": backup_id, "created_at": _now(), "reason": reason, "digest": _digest(files)}
    path = _directory(root, "backups") / (backup_id + ".zip")
    _atomic(path, _pack(files, manifest))
    return _backup_record(path, manifest, files)


def _read_backup(path, agent, library_id):
    manifest, files = _unpack(_read(path, _MAX_ARCHIVE), "backup", agent, library_id=library_id,
                              backup_id=path.stem)
    return _backup_record(path, manifest, files), files


def _remote(value):
    invalid = _Error("invalid_remote", "Use a credential-free HTTPS or SSH Git URL, or an absolute local Git path.")
    if (not isinstance(value, str) or len(value) > 2048 or not value or value != value.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise invalid
    if "://" in value:
        try:
            url = urlsplit(value)
            if (url.scheme not in {"https", "ssh"} or not url.hostname or url.password is not None
                    or (url.username is not None and (url.scheme != "ssh" or url.username != "git"))
                    or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.:-]*", url.hostname)
                    or not re.fullmatch(r"/[a-zA-Z0-9._~/-]+", url.path) or ".." in url.path.split("/")
                    or url.query or url.fragment or "?" in value or "#" in value
                    or (url.port is not None and not 1 <= url.port <= 65535)):
                raise invalid
        except ValueError as exc:
            raise invalid from exc
        return value, url.scheme
    if re.fullmatch(r"git@[a-zA-Z0-9][a-zA-Z0-9.-]*:[a-zA-Z0-9._~/-]+", value):
        host, path = value[4:].split(":", 1)
        if path.startswith("/") or ".." in path.split("/"):
            raise invalid
        return f"ssh://git@{host}/{path}", "ssh"
    local = Path(value)
    if not local.is_absolute() or value.startswith(("//", "\\\\")) or ":" in value[len(local.drive):]:
        raise invalid
    if local.as_uri().startswith("file:///"):
        return str(local), "file"
    raise invalid


def _read_sync(root):
    path = root / "sync.json"
    if not _plain(path, missing=True):
        return None
    try:
        cfg = _decode(_read(path, 4096))
        if not isinstance(cfg, dict) or set(cfg) - {"credential_helper"} != {
                "remote_url", "revision", "digest", "last_synced_at"}:
            raise ValueError
        _remote(cfg["remote_url"])
        _credential_mode(cfg.get("credential_helper"), cfg["remote_url"])
        if cfg["revision"] is None:
            if cfg["digest"] is not None or cfg["last_synced_at"] is not None:
                raise ValueError
        elif (not _SHA.fullmatch(cfg["revision"]) or not _DIGEST.fullmatch(cfg["digest"])
              or not isinstance(cfg["last_synced_at"], str)):
            raise ValueError
        return cfg
    except (_Error, ValueError, TypeError, RecursionError) as exc:
        raise _Error("invalid_sync_config", "The saved sync configuration is invalid.") from exc


def _credential_mode(mode, remote):
    if mode is None:
        return None
    if mode != "gh" or not _GH_REMOTE.fullmatch(remote):
        raise _Error("invalid_remote", "The gh helper requires a canonical HTTPS GitHub repository URL.")
    return "gh"


def _status(cfg, agent, *, digest=None, checked=False, state=None, remote_revision=None):
    changed = None if cfg is None or cfg["digest"] is None or digest is None else cfg["digest"] != digest
    if state is None:
        state = ("disconnected" if cfg is None else "unknown" if changed is None
                 else "local_changes" if changed else "in_sync")
    return {"connected": cfg is not None, "remote_url": cfg["remote_url"] if cfg else None,
            "credential_helper": cfg.get("credential_helper") if cfg else None,
            "branch": f"skills-{agent}", "git_available": shutil.which("git") is not None,
            "state": state, "last_synced_at": cfg["last_synced_at"] if cfg else None,
            "revision": cfg["revision"] if cfg else None, "local_changed": changed,
            "remote_checked": checked, "remote_revision": remote_revision}


class _Git:
    """Use an isolated bare repo; no checkout or externally supplied client hooks."""

    def __init__(self, exe, cwd, remote, protocol, agent, credential_helper=None):
        self.exe = exe
        self.cwd = cwd
        self.remote = remote
        self.ref = f"refs/heads/skills-{agent}"
        self.env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
        self.env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
                         "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never",
                         "GIT_ALLOW_PROTOCOL": protocol, "LC_ALL": "C",
                         "GIT_AUTHOR_NAME": "Skill Library", "GIT_COMMITTER_NAME": "Skill Library",
                         "GIT_AUTHOR_EMAIL": "skills@local.invalid", "GIT_COMMITTER_EMAIL": "skills@local.invalid"})
        if protocol == "ssh":
            self.env["GIT_SSH_COMMAND"] = ("ssh -o BatchMode=yes -o StrictHostKeyChecking=yes "
                                           "-o PermitLocalCommand=no -o ClearAllForwardings=yes")
        self.command = [exe, "-c", f"core.hooksPath={cwd / 'no-hooks'}", "-c", "credential.helper=",
                        "-c", "http.followRedirects=false", "-c", "http.sslVerify=true", "-c",
                        "transfer.fsckObjects=true", "-c", "fetch.writeCommitGraph=false"]
        if credential_helper == "gh":
            try:
                from skills_github import credential_helper as github_helper
            except ImportError as exc:
                raise _Error("github_unavailable", "GitHub credential support is not available.") from exc
            try:
                args, self.env = github_helper(remote, self.env)
            except ValueError as exc:
                code = getattr(exc, "code", "github_verification_failed")
                if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
                    code = "github_verification_failed"
                raise _Error(code, "GitHub could not verify write access to the private repository.") from exc
            self.command.extend(args)
        self.run("init", "--bare", "--quiet", "--template=", "--object-format=sha1")
        if protocol == "file":
            # Use the file transport, not Git's local clone optimizations.
            try:
                local = Path(remote)
                _plain(local, directory=True)
                result = self.run("--git-dir=" + str(local), "rev-parse", "--is-bare-repository")
                if result.stdout.strip() != b"true":
                    raise ValueError
                self.remote = local.resolve(strict=True).as_uri()
            except (OSError, ValueError, _Error) as exc:
                raise _Error("invalid_remote", "The local Git remote must be an existing bare repository.") from exc

    def run(self, *args, data=None, allowed=(0,)):
        try:
            result = subprocess.run([*self.command, *args], cwd=self.cwd, env=self.env,
                                    input=b"" if data is None else data,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as exc:
            raise _Error("git_timeout", "Git did not finish in time. Check the remote before retrying.") from exc
        if result.returncode not in allowed:
            # Never pass through Git's stderr: it can contain credentials or local paths.
            raise _Error("git_failed", "Git failed. Check the Git remote, permissions, and connection.")
        return result

    def revision(self):
        result = self.run("ls-remote", "--exit-code", "--refs", "--", self.remote, self.ref, allowed=(0, 2))
        if result.returncode == 2:
            return None
        output = result.stdout.rstrip(b"\r\n").split(b"\t")
        if len(output) != 2 or output[1] != self.ref.encode("ascii"):
            raise _Error("git_failed", "The remote returned an invalid branch reference.")
        return self._oid(output[0])

    @staticmethod
    def _oid(output):
        try:
            value = output.strip().decode("ascii")
            if _SHA.fullmatch(value):
                return value
        except UnicodeError:
            pass
        raise _Error("git_failed", "Git returned an invalid object identifier.")

    def fetch(self, agent):
        if self.revision() is None:
            return None, None, None
        self.run("fetch", "--quiet", "--no-tags", "--no-recurse-submodules", "--depth=1", "--", self.remote, self.ref)
        head = self._oid(self.run("rev-parse", "--verify", "FETCH_HEAD^{commit}").stdout)
        digest, files = self.snapshot(head, agent)
        return head, digest, files

    def ancestor(self, head, cfg, agent):
        base = cfg["revision"]
        if base is None:
            return None
        self.run("fetch", "--quiet", "--no-tags", "--no-recurse-submodules", f"--depth={_HISTORY_DEPTH}",
                 "--", self.remote, self.ref)
        fetched = self._oid(self.run("rev-parse", "--verify", "FETCH_HEAD^{commit}").stdout)
        if head != fetched:
            raise _Error("remote_changed", "The remote changed while checking the common history. Retry sync.")
        ancestor = self.run("merge-base", "--is-ancestor", base, head, allowed=(0, 1, 128))
        if ancestor.returncode:
            return None
        try:
            digest, files = self.snapshot(base, agent)
        except _Error as exc:
            if exc.code in {"invalid_archive", "invalid_remote_snapshot", "foreign_snapshot"}:
                return None
            raise
        return files if digest == cfg["digest"] else None

    def snapshot(self, revision, agent):
        tree = self.run("ls-tree", "-z", revision).stdout
        entry = re.fullmatch(rb"100644 blob ([a-f0-9]{40}(?:[a-f0-9]{24})?)\tsnapshot\.zip\x00", tree)
        if entry is None:
            raise _Error("invalid_remote_snapshot", "The remote branch does not contain a Skill Library snapshot.")
        blob = entry[1].decode("ascii")
        size = self.run("cat-file", "-s", blob).stdout.strip()
        if not size.isdigit() or int(size) > _MAX_ARCHIVE:
            raise _invalid_archive()
        data = self.run("cat-file", "blob", blob).stdout
        manifest, files = _unpack(data, "sync", agent)
        return manifest["digest"], files

    def push(self, data, parent):
        blob = self._oid(self.run("hash-object", "-w", "--stdin", data=data).stdout)
        tree = self._oid(self.run("mktree", data=f"100644 blob {blob}\tsnapshot.zip\n".encode("ascii")).stdout)
        ancestry = ("-p", parent) if parent is not None else ()
        revision = self._oid(self.run("commit-tree", tree, *ancestry, "-m", "Skill Library snapshot").stdout)
        # Check Git's actual advertised old OID as well as its normal fast-forward protection.
        hooks = _directory(self.cwd, "checked-push-hooks")
        hook = hooks / "pre-push"
        expected = parent if parent is not None else "0" * 40
        hook.write_bytes(("#!/bin/sh\n"
                          "IFS=' ' read -r local_ref local_oid remote_ref remote_oid || exit 1\n"
                          f'[ "$remote_ref" = "{self.ref}" ] && [ "$remote_oid" = "{expected}" ] '
                          '|| { echo SKILL_REMOTE_MOVED >&2; exit 1; }\n'
                          "! IFS= read -r extra\n").encode("ascii"))
        hook.chmod(0o700)
        result = self.run("-c", f"core.hooksPath={hooks}", "push", "--porcelain", "--no-follow-tags", "--", self.remote,
                          f"{revision}:{self.ref}", allowed=(0, 1))
        if result.returncode:
            if (b"[rejected]" in result.stdout or b"[remote rejected]" in result.stdout
                    or b"SKILL_REMOTE_MOVED" in result.stderr):
                raise _Error("remote_changed", "The remote changed during push. Fetch its changes before retrying.")
            raise _Error("git_failed", "The remote rejected the push. Check repository permissions.")
        return revision


@contextmanager
def _operation_lock(root):
    """Serialize module operations across processes without taking the library's SQLite lock."""
    path = root / "operation.lock"
    _plain(path, missing=True)
    with path.open("ab+") as stream:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            lock = lambda: msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            lock = lambda: fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            lock()
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise _Error("backup_busy", "Another backup or sync operation is in progress.") from exc
            raise
        yield  # Closing the stream always releases its lock.


def _export(lib):
    files = lib.export_portable()
    if not isinstance(files, dict) or "manifest.json" not in files or not all(isinstance(data, bytes)
                                                                               for data in files.values()):
        raise _Error("library_export_failed", "The library returned an invalid portable snapshot.")
    _validate_files({name: len(data) for name, data in files.items()})
    _portable_scope(files)
    return files


def _is_empty(files):
    if set(files) != {"manifest.json"}:
        return False
    try:
        manifest = _decode(files["manifest.json"])
        return manifest == {"schema": 1, "skills": [], "presets": []}
    except (ValueError, RecursionError):
        return False


def _replace(lib, files, current, safety_backup):
    result = lib.import_portable(files, expected_current=current)
    if not isinstance(result, dict) or result.get("ok") is not True:
        code = result.get("code") if isinstance(result, dict) else None
        if code is None and isinstance(result, dict) and isinstance(result.get("error"), dict):
            code = result["error"].get("code")
        if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            code = "library_import_failed"
        msg = {"library_changed": "The local library changed during this operation. Retry with its new state.",
               "deployments_active": "Disable affected deployed skills before replacing local library content."}
        raise _Error(code, msg.get(code, "The library rejected the snapshot. Local content was not replaced."),
                     safety_backup=safety_backup)


def _fold(name):
    return unicodedata.normalize("NFC", name).casefold()


def _units(files):
    manifest = _decode(files["manifest.json"])
    data = {item["id"]: {} for item in manifest["skills"]}
    for path, content in files.items():
        if path != "manifest.json":
            _, skill_id, relative = path.split("/", 2)
            data[skill_id][relative] = content
    return ({_fold(item["id"]): (item, data[item["id"]]) for item in manifest["skills"]},
            {item["name"]: item["ids"] for item in manifest["presets"]})


def _remote_copy(name, occupied, *, skill):
    for n in range(1, len(occupied) + 2):
        suffix = ("-remote" if skill else " (remote)") if n == 1 else (
            f"-remote-{n}" if skill else f" (remote {n})")
        if skill:
            prefix = name.encode("utf-8")[:255 - len(suffix)].decode("utf-8", errors="ignore").rstrip(" .")
        else:
            prefix = name
        candidate = (prefix or "skill") + suffix
        key = _fold(candidate)
        if key not in occupied:
            occupied.add(key)
            return candidate
    raise AssertionError("No available copy name")


def _decision(base, local, remote, known):
    if local == remote:
        return "both"
    if (known and local == base) or (not known and local is None):
        return "remote"
    if (known and remote == base) or (not known and remote is None):
        return "local"
    return "conflict"


def _merge_portable(common, local_files, remote_files, *, keep_both=False):
    known = common is not None
    bases, base_presets = _units(common) if known else ({}, {})
    local, local_presets = _units(local_files)
    remote, remote_presets = _units(remote_files)
    skills, presets, skill_conflicts, preset_conflicts = {}, {}, [], []
    renamed_skills, renamed_presets, remote_ids = [], [], {}
    occupied = set(local) | set(remote) | set(bases)
    for key in sorted(set(local) | set(remote)):
        lval, rval = local.get(key), remote.get(key)
        choice = _decision(bases.get(key), lval, rval, known)
        if choice == "conflict":
            skill_conflicts.append((lval or rval)[0]["id"])
            if keep_both and rval is not None:
                item, content = rval
                new_id = _remote_copy(item["id"], occupied, skill=True)
                skills[new_id] = ({**item, "id": new_id}, content)
                remote_ids[item["id"]] = new_id
                renamed_skills.append({"from_id": item["id"], "to_id": new_id})
        item = rval if choice == "remote" else lval
        if item is not None:
            skills[item[0]["id"]] = item

    occupied = {_fold(name) for name in local_presets.keys() | remote_presets.keys() | base_presets.keys()}

    def put_preset(name, ids, side):
        if ids is None:
            return
        ids = [remote_ids.get(item, item) for item in ids] if side == "remote" else list(ids)
        if side == "both" and keep_both:
            shared_ids = []
            for skill_id in ids:
                remote_id = remote_ids.get(skill_id)
                if skill_id in skills or remote_id is None:
                    shared_ids.append(skill_id)
                if remote_id is not None:
                    shared_ids.append(remote_id)
            ids = shared_ids
        for skill_id in set(ids) - skills.keys():
            if name not in preset_conflicts:
                preset_conflicts.append(name)
            if keep_both:
                # A surviving preset can require a skill that the other side deleted.
                candidate = (remote if side == "remote" else local).get(_fold(skill_id))
                if candidate is not None and not any(_fold(existing) == _fold(skill_id) for existing in skills):
                    skills[skill_id] = candidate
        presets[name] = ids

    for name in sorted(local_presets.keys() | remote_presets.keys(), key=lambda val: (val.casefold(), val)):
        lval, rval = local_presets.get(name), remote_presets.get(name)
        choice = _decision(base_presets.get(name), lval, rval, known)
        if choice == "conflict":
            preset_conflicts.append(name)
            if keep_both and rval is not None:
                new_name = _remote_copy(name, occupied, skill=False)
                put_preset(new_name, rval, "remote")
                renamed_presets.append({"from_name": name, "to_name": new_name})
        put_preset(name, rval if choice == "remote" else lval, choice)

    conflicts = {"skill_ids": skill_conflicts, "preset_names": sorted(set(preset_conflicts)),
                 "baseline_available": known}
    details = {"mode": "keep_both" if keep_both else "auto", "renamed_skills": renamed_skills,
               "renamed_presets": renamed_presets}
    if not keep_both and (not known or skill_conflicts or preset_conflicts):
        return None, conflicts, details
    # Match SkillLibrary's serialization so the next CAS and sync compare exact bytes.
    manifest = {"schema": 1, "skills": [skills[key][0] for key in sorted(skills)],
                "presets": [{"name": name, "ids": presets[name]}
                            for name in sorted(presets, key=lambda val: (val.casefold(), val))]}
    result = {"manifest.json": json.dumps(manifest, ensure_ascii=True, sort_keys=True,
                                          separators=(",", ":"), allow_nan=False).encode("utf-8")}
    for skill_id, (_, data) in skills.items():
        result.update({f"skills/{skill_id}/{path}": content for path, content in data.items()})
    _validate_files({name: len(data) for name, data in result.items()})
    _portable_scope(result)
    return result, conflicts, details


def _conflict(head, local_digest, details):
    return {"remote_revision": head, "local_digest": local_digest, "choices": list(_STRATEGIES), **details}


def _sync_manifest(agent, digest):
    return {"version": 1, "kind": "sync", "agent": agent, "digest": digest}


def _double_backup(root, agent, library_id, current, remote_files):
    local_backup = _snapshot(root, agent, library_id, current, "conflict_local")
    remote_backup = _snapshot(root, agent, library_id, remote_files, "conflict_remote")
    return {"local_backup": local_backup, "remote_backup": remote_backup}


def _apply_merge(git, lib, root, agent, library_id, current, head, remote_files, merged, details, *, publish):
    copies = _double_backup(root, agent, library_id, current, remote_files)
    stale_code = "sync_conflict_stale" if details["mode"] == "keep_both" else "remote_changed"
    if git.revision() != head:
        raise _Error(stale_code, "The remote changed after comparison. Retry sync.", **copies)
    digest = _digest(merged)
    data = _pack(merged, _sync_manifest(agent, digest)) if publish else None
    replaced = False
    try:
        if merged != current:
            _replace(lib, merged, current, copies["local_backup"])
            replaced = True
        if publish:
            if _export(lib) != merged:
                raise _Error("library_changed", "The local library changed before publishing the merge.")
            head = git.push(data, head)
    except (OSError, ValueError, _Error) as exc:
        if replaced:
            try:
                _replace(lib, current, merged, copies["local_backup"])
                replaced = False
            except (OSError, ValueError, _Error):
                pass  # Leave later edits and both recovery snapshots intact.
        code = exc.code if isinstance(exc, _Error) else "backup_io_failed"
        if details["mode"] == "keep_both" and code in {"remote_changed", "library_changed"}:
            code = "sync_conflict_stale"
        raise _Error(code, str(exc) if isinstance(exc, _Error) else "Could not apply the merge.",
                     local_merge_applied=replaced, **copies) from exc
    return head, digest, {**copies, "merge": {**details, "published": publish}}


def _resolve(git, lib, payload, root, agent, library_id, current, local_digest, head, remote_files, remote_digest):
    copies = _double_backup(root, agent, library_id, current, remote_files)
    if git.revision() != head or _export(lib) != current:
        raise _Error("sync_conflict_stale", "A library changed after confirmation. Check the conflict again.", **copies)
    if payload["strategy"] == "keep_local":
        try:
            return git.push(_pack(current, _sync_manifest(agent, local_digest)), head), local_digest, copies
        except _Error as exc:
            if exc.code == "remote_changed":
                raise _Error("sync_conflict_stale", "The remote changed after confirmation. Check the conflict again.",
                             **copies) from exc
            raise _Error(exc.code, str(exc), **copies) from exc
    try:
        _replace(lib, remote_files, current, copies["local_backup"])
    except _Error as exc:
        code = "sync_conflict_stale" if exc.code == "library_changed" else exc.code
        raise _Error(code, str(exc), **copies) from exc
    return head, remote_digest, copies


def _sync(lib, op, payload, root, agent, library_id, cfg):
    if cfg is None:
        raise _Error("sync_not_connected", "Configure the Git remote before syncing.")
    exe = shutil.which("git")
    if exe is None:
        raise _Error("git_unavailable", "Git must be installed to sync.")
    local_files = _export(lib)
    local_digest = _digest(local_files)
    remote_url, protocol = _remote(cfg["remote_url"])
    safety_backup, changed = None, False
    extra = {}
    with tempfile.TemporaryDirectory(prefix="git-", dir=root) as temp:
        git = _Git(exe, Path(temp), remote_url, protocol, agent, cfg.get("credential_helper"))
        head, remote_digest, remote_files = git.fetch(agent)
        base = cfg["digest"]
        local_changed = (not _is_empty(local_files)) if base is None else local_digest != base
        remote_changed = base is None or remote_digest != base
        conflict = head is not None and remote_digest != local_digest and local_changed and remote_changed
        merged, common = None, None
        if conflict:
            common = git.ancestor(head, cfg, agent)
            merged, conflicts, merge_details = _merge_portable(common, local_files, remote_files)
        if op == "sync_resolve":
            if (not conflict or merged is not None or head != payload["expected_remote_revision"]
                    or local_digest != payload["expected_local_digest"]):
                details = {"conflict": _conflict(head, local_digest, conflicts)} if conflict and merged is None else {}
                raise _Error("sync_conflict_stale", "A library changed after confirmation. Check the conflict again.",
                             **details)
            if payload["strategy"] == "keep_both":
                merged, _, merge_details = _merge_portable(common, local_files, remote_files, keep_both=True)
                head, baseline_digest, extra = _apply_merge(git, lib, root, agent, library_id, local_files,
                                                            head, remote_files, merged, merge_details, publish=True)
            else:
                head, baseline_digest, extra = _resolve(git, lib, payload, root, agent, library_id, local_files,
                                                         local_digest, head, remote_files, remote_digest)
            changed = True
        if head is None and base is not None:
            raise _Error("remote_missing", "The previously synced branch no longer exists.",
                         status=_status(cfg, agent, digest=local_digest, checked=True, state="conflict"))
        if head is None and op == "sync_pull":
            return {"status": _status(cfg, agent, digest=local_digest, checked=True, state="local_changes"),
                    "changed": False, "safety_backup": None}
        if op != "sync_resolve" and conflict:
            if merged is None:
                raise _Error("sync_conflict", "Both libraries have different changes. Both copies were preserved.",
                             status=_status(cfg, agent, digest=local_digest, checked=True, state="conflict",
                                            remote_revision=head), conflict=_conflict(head, local_digest, conflicts))
            publish = op == "sync_push"
            head, merged_digest, extra = _apply_merge(git, lib, root, agent, library_id, local_files,
                                                      head, remote_files, merged, merge_details, publish=publish)
            baseline_digest = merged_digest if publish else remote_digest
            changed = True
        elif op != "sync_resolve" and head is not None and remote_digest != local_digest:
            if op == "sync_push" and remote_changed:
                raise _Error("remote_ahead", "Pull the remote snapshot before pushing.",
                             status=_status(cfg, agent, digest=local_digest, checked=True, state="remote_ahead",
                                            remote_revision=head))
            if op == "sync_pull" and local_changed:
                return {"status": _status(cfg, agent, digest=local_digest, checked=True, state="local_changes",
                                          remote_revision=head), "changed": False, "safety_backup": None}
        if op != "sync_resolve" and not conflict:
            if local_digest != remote_digest:
                if op == "sync_push":
                    if _export(lib) != local_files:
                        raise _Error("library_changed", "The local library changed during sync. Retry before pushing.")
                    head = git.push(_pack(local_files, _sync_manifest(agent, local_digest)), head)
                else:
                    safety_backup = _snapshot(root, agent, library_id, local_files, "before_pull")
                    _replace(lib, remote_files, local_files, safety_backup)
                    local_digest = remote_digest
                changed = True
            baseline_digest = local_digest
    cfg = {**cfg, "revision": head, "digest": baseline_digest, "last_synced_at": _now()}
    try:
        _atomic(root / "sync.json", _json(cfg))
    except OSError as exc:
        raise _Error("sync_state_not_saved", "Sync transferred the snapshot but could not save its local status.",
                     changed=changed, safety_backup=safety_backup, **extra) from exc
    return {"status": _status(cfg, agent, digest=_digest(_export(lib)), checked=True, remote_revision=head),
            "changed": changed, "safety_backup": safety_backup, **extra}


def _handle(lib, op, payload, root, agent, library_id):
    if op == "backup_create":
        return {"backup": _snapshot(root, agent, library_id, _export(lib), "manual")}
    if op == "backup_list":
        backups, skipped = [], 0
        for path in _directory(root, "backups").glob("*.zip"):
            if path.suffix != ".zip" or not _ID.fullmatch(path.stem):
                continue
            try:
                record, _ = _read_backup(path, agent, library_id)
                backups.append(record)
            except (_Error, OSError):
                skipped += 1
        return {"backups": sorted(backups, key=lambda item: (item["created_at"], item["backup_id"]), reverse=True),
                "skipped": skipped}
    if op == "backup_restore":
        backup_id = payload.get("backup_id")
        if not isinstance(backup_id, str) or not _ID.fullmatch(backup_id):
            raise _Error("invalid_backup_id", "Choose a valid backup from this library.")
        try:
            record, files = _read_backup(_directory(root, "backups") / (backup_id + ".zip"), agent, library_id)
        except FileNotFoundError as exc:
            raise _Error("backup_not_found", "The selected backup was not found in this library.") from exc
        current = _export(lib)
        safety_backup = _snapshot(root, agent, library_id, current, "before_restore")
        _replace(lib, files, current, safety_backup)
        return {"backup": record, "safety_backup": safety_backup}

    cfg = _read_sync(root)
    if op == "sync_connect":
        value, _ = _remote(payload.get("remote_url"))
        helper = _credential_mode(payload.get("credential_helper"), value)
        if cfg is None or cfg["remote_url"] != value or cfg.get("credential_helper") != helper:
            if cfg is None or cfg["remote_url"] != value:
                cfg = {"remote_url": value, "revision": None, "digest": None, "last_synced_at": None}
            else:
                cfg = {key: value for key, value in cfg.items() if key != "credential_helper"}
            if helper is not None:
                cfg["credential_helper"] = helper
            _atomic(root / "sync.json", _json(cfg))
    if op in {"sync_status", "sync_connect"}:
        digest = _digest(_export(lib)) if cfg is not None and cfg["digest"] is not None else None
        return {"status": _status(cfg, agent, digest=digest)}
    return _sync(lib, op, payload, root, agent, library_id, cfg)


def handle(lib, op, payload):
    """Dispatch a backup or sync operation for the given SkillLibrary, returning JSON-compatible data."""
    agent = getattr(lib, "agent", None)
    if not isinstance(agent, str) or not _AGENT.fullmatch(agent):
        return {"ok": False, "agent": None, "error": {"code": "invalid_agent", "message": "Invalid library agent."}}
    try:
        if not isinstance(op, str) or op not in _OP_FIELDS:
            raise _Error("unknown_operation", "Unknown backup or sync operation.")
        if not isinstance(payload, dict) or set(payload) - set(_OP_FIELDS[op]):
            raise _Error("invalid_payload", "Unexpected backup or sync request fields.")
        if "confirm" in _OP_FIELDS[op] and payload.get("confirm") is not True:
            raise _Error("confirmation_required", "Confirm this operation before continuing.")
        if op == "sync_resolve":
            revision = payload.get("expected_remote_revision")
            digest = payload.get("expected_local_digest")
            if (payload.get("strategy") not in _STRATEGIES or not isinstance(revision, str)
                    or not _SHA.fullmatch(revision) or not isinstance(digest, str) or not _DIGEST.fullmatch(digest)):
                raise _Error("invalid_payload", "Choose a conflict resolution using the latest conflict details.")
        library_id = uuid.UUID(str(lib.library_id)).hex
        data_dir = Path(lib.data_dir)
        if not data_dir.is_absolute():
            raise _Error("unsafe_path", "The library data path must be absolute.")
        _plain(data_dir, directory=True)
        root = _directory(_directory(_directory(data_dir, "skills-backup"), agent), library_id)
        with _operation_lock(root):
            result = _handle(lib, op, payload, root, agent, library_id)
        return {"ok": True, "agent": agent, **result}
    except _Error as exc:
        return {"ok": False, "agent": agent, "error": {"code": exc.code, "message": str(exc)}, **exc.details}
    except (OSError, ValueError, TypeError):
        return {"ok": False, "agent": agent,
                "error": {"code": "backup_io_failed", "message": "Could not read or write the backup data."}}

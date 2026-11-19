"""Optional private GitHub backup setup via an existing GitHub CLI login."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path


_HOST = "github.com"
_HTTPS = f"https://{_HOST}/"
_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_REPO = re.compile(r"[A-Za-z0-9_.-]{1,100}")
_REPO_FIELDS = "{full_name, private, visibility, archived, disabled, permissions: {push: .permissions.push}}"
_GUIDANCE = {
    "login_command": "gh auth login --hostname github.com",
    "ssh_remote": "git@github.com:OWNER/REPO.git",
    "ssh_test_command": "ssh -T git@github.com",
}
_MESSAGES = {
    "github_no_gh": "Install the official GitHub CLI and run gh auth login --hostname github.com, or use a private repository with SSH.",
    "github_auth_required": "Sign in manually with gh auth login --hostname github.com, or use a private repository with SSH.",
    "github_request_failed": "GitHub could not verify the request. Check connectivity and repository access, then retry.",
    "github_create_failed": "GitHub did not confirm creation. Check whether the repository exists before retrying; verify your account can create private repositories.",
    "github_not_private": "Select a private GitHub repository. Public and internal repositories cannot be used here.",
    "github_repo_mismatch": "GitHub returned a different repository. Enter the current OWNER/REPO and confirm again.",
    "github_no_write": "The repository must be writable by the active GitHub account and must not be archived or disabled.",
    "github_invalid_repo": "Enter a GitHub repository as OWNER/REPO.",
    "github_account_changed": "The active GitHub CLI account changed. Refresh the account and confirm the operation again.",
    "github_helper_unavailable": "Update the official GitHub CLI to use its HTTPS credential helper, or choose SSH.",
    "confirmation_required": "Confirm the exact GitHub repository and operation first.",
    "invalid_request": "Invalid GitHub backup request.",
    "unknown_operation": "Unrecognized GitHub backup operation.",
}


class GitHubError(ValueError):
    """A public error containing no gh output, local paths or credentials."""

    def __init__(self, code):
        super().__init__(_MESSAGES[code])
        self.code = code


def _split_repo(value):
    if isinstance(value, str) and len(value) <= 140 and value.count("/") == 1:
        owner, repo = value.split("/")
        if _LOGIN.fullmatch(owner) and _REPO.fullmatch(repo) and repo not in (".", "..") and not repo.lower().endswith(".git"):
            return owner, repo
    raise GitHubError("github_invalid_repo")


def _login_name(value):
    return isinstance(value, str) and _LOGIN.fullmatch(value) is not None


def _find_gh(path):
    # Do not let Windows search the current directory or run .cmd/.bat wrappers.
    name = "gh.exe" if os.name == "nt" else "gh"
    for directory in path.split(os.pathsep):
        directory = Path(directory.strip('"'))
        if not directory.is_absolute() or str(directory).startswith(("\\\\", "//")):
            continue
        candidate = directory / name
        try:
            resolved = candidate.resolve(strict=True)
            if (resolved.is_file() and os.access(resolved, os.X_OK)
                    and not str(resolved).startswith(("\\\\", "//"))
                    and not any(ord(char) < 32 or ord(char) == 127 for char in str(resolved))
                    and (os.name != "nt" or resolved.suffix.lower() == ".exe")):
                return resolved
        except (OSError, RuntimeError):
            continue
    return None


def _environment(base):
    # Keep the user's real gh configuration / keyring. Ignore all env-token and repo overrides.
    allowed_gh = {"GH_CONFIG_DIR"}
    discard = {"BASH_ENV", "ENV", "DEBUG", "SSLKEYLOGFILE", "PAGER", "GIT_ASKPASS", "SSH_ASKPASS",
               "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_SSL_NO_VERIFY", "GIT_CONFIG_PARAMETERS"}
    prefixes = ("GITHUB_", "GIT_TRACE", "GIT_CURL_VERBOSE", "GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
    env = {str(k): str(v) for k, v in base.items()
           if k.upper() not in discard and not k.upper().startswith(prefixes)
           and (not k.upper().startswith("GH_") or k.upper() in allowed_gh)
           and k.upper() not in {"GIT_CONFIG_COUNT", "GIT_CONFIG", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM"}}
    env.update({"GH_HOST": _HOST, "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1",
                "GH_NO_EXTENSION_UPDATE_NOTIFIER": "1", "GH_TELEMETRY": "false", "GH_PAGER": "",
                "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_CONFIG_GLOBAL": os.devnull, "GCM_INTERACTIVE": "never"})
    return env


class _CLI:
    def __init__(self, exe, env, cwd):
        self.exe, self.env, self.cwd = exe, env, cwd

    def _run(self, args, *, capture=True, error="github_request_failed"):
        # Only methods below construct argv; all commands are built-ins, never aliases or extensions.
        try:
            result = subprocess.run([str(self.exe), *args], cwd=self.cwd, env=self.env, shell=False,
                                    stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE if capture else subprocess.DEVNULL, timeout=20,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError, ValueError):
            raise GitHubError(error) from None
        if capture and len(result.stdout) > 64 * 1024:
            raise GitHubError(error)
        return result

    def _api(self, method, endpoint, *options, capture=True, error="github_request_failed"):
        return self._run(("api", "--hostname", _HOST, "--method", method, endpoint, *options),
                         capture=capture, error=error)

    def login(self):
        result = self._api("GET", "user", "--jq", ".login")
        if result.returncode == 0:
            login = result.stdout.decode("ascii", "replace").strip()
            if _login_name(login):
                return login
            raise GitHubError("github_request_failed")
        # Keep status output out of Python, even though the standard command masks the token.
        if result.returncode == 4 or self._run(("auth", "status", "--hostname", _HOST), capture=False).returncode == 1:
            raise GitHubError("github_auth_required")
        raise GitHubError("github_request_failed")

    def verify(self, repo):
        owner, name = _split_repo(repo)
        result = self._api("GET", f"repos/{owner}/{name}", "--jq", _REPO_FIELDS)
        if result.returncode != 0:
            raise GitHubError("github_request_failed")
        try:
            data = json.loads(result.stdout)
            full_name = data.get("full_name")
            _split_repo(full_name)
        except (ValueError, AttributeError, RecursionError):
            raise GitHubError("github_request_failed") from None
        if full_name.lower() != repo.lower():
            raise GitHubError("github_repo_mismatch")
        if data.get("private") is not True or data.get("visibility") != "private":
            raise GitHubError("github_not_private")
        permissions = data.get("permissions")
        if (data.get("archived") is not False or data.get("disabled") is not False
                or not isinstance(permissions, dict) or permissions.get("push") is not True):
            raise GitHubError("github_no_write")
        return full_name

    def create(self, repo, login):
        owner, name = _split_repo(repo)
        endpoint = "user/repos" if owner.lower() == login.lower() else f"orgs/{owner}/repos"
        result = self._api("POST", endpoint, "--raw-field", f"name={name}", "--field", "private=true",
                           "--field", "auto_init=false", "--jq", "{full_name, private, visibility}", error="github_create_failed")
        if result.returncode != 0:
            raise GitHubError("github_create_failed")
        try:
            data = json.loads(result.stdout)
            full_name = data.get("full_name")
            _split_repo(full_name)
        except (ValueError, AttributeError, RecursionError):
            raise GitHubError("github_create_failed") from None
        if full_name.lower() != repo.lower():
            raise GitHubError("github_repo_mismatch")
        if data.get("private") is not True or data.get("visibility") != "private":
            raise GitHubError("github_not_private")

    def check_helper(self):
        result = self._run(("auth", "git-credential", "--help"), capture=False, error="github_helper_unavailable")
        if result.returncode != 0:
            raise GitHubError("github_helper_unavailable")


@contextmanager
def _cli(base):
    exe = _find_gh(base.get("PATH", os.defpath))
    if exe is None:
        raise GitHubError("github_no_gh")
    try:
        with tempfile.TemporaryDirectory(prefix="skills-gh-") as cwd:
            yield _CLI(exe, _environment(base), cwd)
    except OSError:
        raise GitHubError("github_request_failed") from None


def _status():
    try:
        with _cli(os.environ) as gh:
            login = gh.login()
    except GitHubError as exc:
        return {"available": exc.code != "github_no_gh", "authenticated": False, "login": None,
                "code": exc.code, "error": str(exc), "guidance": dict(_GUIDANCE)}
    return {"available": True, "authenticated": True, "login": login, "code": "ok", "guidance": dict(_GUIDANCE)}


def _connect(payload):
    if payload.get("confirm") is not True:
        raise GitHubError("confirmation_required")
    if set(payload) - {"confirm", "mode", "protocol", "repo", "login"}:
        raise GitHubError("invalid_request")
    mode, protocol, repo = payload.get("mode"), payload.get("protocol", "https"), payload.get("repo")
    expected = payload.get("login")
    if mode not in ("select", "create") or protocol not in ("https", "ssh") or ("login" in payload and not _login_name(expected)):
        raise GitHubError("invalid_request")
    _split_repo(repo)
    with _cli(os.environ) as gh:
        login = gh.login()
        if expected is not None and login.lower() != expected.lower():
            raise GitHubError("github_account_changed")
        if protocol == "https":
            gh.check_helper()
        if mode == "create":
            gh.create(repo, login)
        canonical = gh.verify(repo)
    https = protocol == "https"
    remote = f"{_HTTPS}{canonical}.git" if https else f"git@{_HOST}:{canonical}.git"
    return {"repo": canonical, "protocol": protocol, "remote": remote, "created": mode == "create",
            "credential_helper": "gh" if https else None}


def credential_helper(remote, env):
    """Return ephemeral Git argv/env for one verified GitHub HTTPS backup remote.

    Use in an isolated Git context. Never persist the helper command or log its
    subprocess output. The application does not invoke `git credential fill`.
    """
    if not isinstance(remote, str) or not remote.startswith(_HTTPS) or not remote.endswith(".git"):
        raise GitHubError("github_invalid_repo")
    repo = remote[len(_HTTPS):-4]
    _split_repo(repo)
    with _cli(env) as gh:
        login = gh.login()
        gh.verify(repo)
        gh.check_helper()
        executable = shlex.quote(gh.exe.as_posix())
        args = ["-c", "credential.helper=", "-c", f"credential.{remote}.useHttpPath=true",
                "-c", f"credential.{remote}.username={login}",
                "-c", f"credential.{remote}.helper=!exec {executable} auth git-credential",
                "-c", "http.followRedirects=false", "-c", "http.sslVerify=true"]
        return args, {**gh.env, "GIT_ALLOW_PROTOCOL": "https"}


def handle(lib, op, payload):
    """GitHub setup has no local library writes or synchronization side effects."""
    try:
        if not isinstance(payload, dict):
            raise GitHubError("invalid_request")
        if op == "github_status":
            if payload:
                raise GitHubError("invalid_request")
            return {"ok": True, **_status()}
        if op == "github_connect":
            return {"ok": True, **_connect(payload)}
        raise GitHubError("unknown_operation")
    except GitHubError as exc:
        return {"ok": False, "code": exc.code, "error": str(exc)}

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import skills_github as github


REPO = "Alice/backup"
REMOTE = "https://github.com/Alice/backup.git"
SECRET = "ghp_mock_secret_undefined_in_public_result"
ENV = {"PATH": os.defpath, "GH_CONFIG_DIR": "existing-gh-config", "GH_HOST": "untrusted.test",
       "GH_REPO": "evil/override", "GH_TOKEN": SECRET, "GITHUB_TOKEN": SECRET, "GH_ENTERPRISE_TOKEN": SECRET,
       "GITHUB_ENTERPRISE_TOKEN": SECRET, "GH_DEBUG": "api", "GH_EXTENSION": "1", "GIT_TRACE_CURL": "1",
       "GIT_CURL_VERBOSE": "1", "GIT_SSL_NO_VERIFY": "1", "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "x",
       "GIT_CONFIG_VALUE_0": SECRET, "SSLKEYLOGFILE": "keylog.txt"}


def returned(repo=REPO, **overrides):
    data = {"full_name": repo, "private": True, "visibility": "private", "archived": False,
            "disabled": False, "permissions": {"push": True}}
    return json.dumps(data | overrides).encode("utf-8")


class GitHubTest(unittest.TestCase):
    def setUp(self):
        self.exe = Path(os.path.abspath("Gh mock path/with quote's/gh.exe"))
        self.find = self.enterContext(patch.object(github, "_find_gh", return_value=self.exe))
        self.run = self.enterContext(patch.object(github.subprocess, "run"))
        self.enterContext(patch.dict(os.environ, ENV, clear=False))

    def respond(self, *results):
        self.run.side_effect = [subprocess.CompletedProcess([], code, stdout=out, stderr=SECRET.encode()) for code, out in results]

    def call(self, op, **payload):
        result = github.handle(object(), op, payload)
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertNotIn(str(self.exe), json.dumps(result))
        return result

    def connect(self, **payload):
        return self.call("github_connect", **({"mode": "select", "repo": REPO, "protocol": "https", "confirm": True} | payload))

    def argv(self):
        return [call.args[0][1:] for call in self.run.call_args_list]

    def test_missing_gh_reports_manual_login_and_ssh(self):
        self.find.return_value = None
        status = self.call("github_status")
        self.assertTrue(status["ok"])
        self.assertFalse(status["available"])
        self.assertEqual(status["guidance"]["login_command"], "gh auth login --hostname github.com")
        self.assertEqual(status["guidance"]["ssh_remote"], "git@github.com:OWNER/REPO.git")
        self.assertEqual(self.connect()["code"], "github_no_gh")
        self.run.assert_not_called()

    def test_status_uses_existing_login_and_scrubs_auth_overrides(self):
        self.respond((0, b"Alice\n"))
        status = self.call("github_status")
        self.assertTrue(status["authenticated"])
        self.assertEqual(status["login"], "Alice")
        self.assertEqual(self.argv(), [["api", "--hostname", "github.com", "--method", "GET", "user", "--jq", ".login"]])
        kwargs = self.run.call_args.kwargs
        env = kwargs["env"]
        self.assertEqual(env["GH_HOST"], "github.com")
        self.assertEqual(env["GH_CONFIG_DIR"], "existing-gh-config")
        self.assertEqual(env["GH_PROMPT_DISABLED"], "1")
        self.assertEqual(env["GH_NO_EXTENSION_UPDATE_NOTIFIER"], "1")
        self.assertNotIn(SECRET, repr(kwargs))
        self.assertTrue(all(k not in env for k in ENV.keys() - {"PATH", "GH_CONFIG_DIR", "GH_HOST"}))
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertFalse(Path(kwargs["cwd"]).exists())

    def test_not_logged_in_and_network_failure_are_handled(self):
        self.respond((4, SECRET.encode()))
        status = self.call("github_status")
        self.assertFalse(status["authenticated"])
        self.assertEqual(status["code"], "github_auth_required")
        self.respond((1, SECRET.encode()), (1, None))
        self.assertEqual(self.call("github_status")["code"], "github_auth_required")
        self.assertEqual(self.run.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.respond((1, SECRET.encode()), (0, None))
        self.assertEqual(self.call("github_status")["code"], "github_request_failed")

    def test_validation_and_confirmation_happen_before_gh(self):
        for confirm in (False, None, 1, "true"):
            self.assertEqual(self.connect(confirm=confirm)["code"], "confirmation_required")
        for repo in ("--help", "a/..", "a/name.git", "a/name with spaces", "x/-x?cmd", "a/b/c",
                     "https://github.com/a/b", "a/b\n--help", "a/%2E%2E", "--a/b", "a/b;id", None):
            with self.subTest(repo=repo):
                self.assertEqual(self.connect(repo=repo)["code"], "github_invalid_repo")
        self.assertEqual(self.connect(protocol="file")["code"], "invalid_request")
        self.assertEqual(self.connect(repo_override="evil/repo")["code"], "invalid_request")
        self.assertEqual(self.call("github_status", hostname="enterprise.test")["code"], "invalid_request")
        self.assertEqual(self.call("extension")["code"], "unknown_operation")
        self.run.assert_not_called()

    def test_select_https_returns_checked_canonical_remote(self):
        self.respond((0, b"Alice\n"), (0, None), (0, returned()))
        result = self.connect(repo="alice/Backup", login="alice")
        self.assertEqual(result, {"ok": True, "repo": REPO, "created": False, "protocol": "https", "remote": REMOTE,
                                  "credential_helper": "gh"})
        self.assertIn(["auth", "git-credential", "--help"], self.argv())
        self.assertIn("repos/alice/Backup", self.argv()[-1])
        self.assertTrue(all("POST" not in args for args in self.argv()))

    def test_select_ssh_checks_repo_without_credential_helper(self):
        self.respond((0, b"Alice\n"), (0, returned()))
        result = self.connect(protocol="ssh")
        self.assertEqual(result["remote"], "git@github.com:Alice/backup.git")
        self.assertIsNone(result["credential_helper"])
        self.assertEqual(len(self.argv()), 2)

    def test_create_private_personal_and_organization_repos_only(self):
        for repo, endpoint in ((REPO, "user/repos"), ("Team/backup", "orgs/Team/repos")):
            with self.subTest(repo=repo):
                self.run.reset_mock()
                self.respond((0, b"Alice\n"), (0, returned(repo)), (0, returned(repo)))
                result = self.connect(repo=repo, mode="create", protocol="ssh", login="Alice")
                self.assertTrue(result["ok"], result)
                self.assertTrue(result["created"])
                self.assertEqual(self.argv()[1][:6], ["api", "--hostname", "github.com", "--method", "POST", endpoint])
                self.assertEqual(self.argv()[1][6:12], ["--raw-field", "name=backup", "--field", "private=true", "--field", "auto_init=false"])
                self.assertEqual(self.argv()[2][4:6], ["GET", "repos/" + repo])
                self.assertTrue(all(args[0] == "api" for args in self.argv()))

    def test_create_refuses_changed_login_and_does_not_retry_failure(self):
        self.respond((0, b"Other\n"))
        self.assertEqual(self.connect(mode="create", login="Alice")["code"], "github_account_changed")
        self.assertEqual(len(self.argv()), 1)
        self.run.reset_mock()
        self.respond((0, b"Alice\n"), (1, SECRET.encode()))
        self.assertEqual(self.connect(mode="create", protocol="ssh")["code"], "github_create_failed")
        self.assertEqual(len(self.argv()), 2)
        self.respond((0, b"Alice\n"), (0, returned("Other/backup")))
        self.assertEqual(self.connect(mode="create", protocol="ssh")["code"], "github_repo_mismatch")

    def test_rejects_untrusted_visibility_repo_and_access(self):
        cases = ((returned(private=False, visibility="public"), "github_not_private"),
                 (returned(visibility="internal"), "github_not_private"),
                 (returned("other/backup"), "github_repo_mismatch"),
                 (returned(archived=True), "github_no_write"), (returned(disabled=True), "github_no_write"),
                 (returned(permissions={"push": False}), "github_no_write"),
                 (returned(permissions={"push": "true"}), "github_no_write"),
                 (SECRET.encode(), "github_request_failed"),
                 (returned("github.com@evil.test/repo"), "github_request_failed"))
        for response, code in cases:
            with self.subTest(code=code):
                self.respond((0, b"Alice\n"), (0, response))
                self.assertEqual(self.connect(protocol="ssh")["code"], code)

    def test_helper_is_scoped_to_one_https_repo_and_never_retrieves_token_in_app(self):
        self.respond((0, b"Alice\n"), (0, returned()), (0, None))
        args, env = github.credential_helper(REMOTE, ENV | {"GIT_ALLOW_PROTOCOL": "ssh:file"})
        self.assertIn(f"credential.{REMOTE}.useHttpPath=true", args)
        self.assertIn(f"credential.{REMOTE}.username=Alice", args)
        self.assertIn(f"credential.{REMOTE}.helper=!exec {github.shlex.quote(self.exe.as_posix())} auth git-credential", args)
        self.assertIn("http.followRedirects=false", args)
        self.assertIn("http.sslVerify=true", args)
        self.assertIn("credential.helper=", args)
        self.assertEqual(env["GIT_ALLOW_PROTOCOL"], "https")
        self.assertNotIn(SECRET, repr((args, env)))
        self.assertEqual(self.argv()[-1], ["auth", "git-credential", "--help"])
        self.assertTrue(all("token" not in args and "get" not in args for args in self.argv()))

    def test_helper_rejects_other_hosts_url_overrides_and_public_repo(self):
        for remote in ("https://github.com.evil/x/y.git", "https://github.com:443/x/y.git",
                       "https://token@github.com/x/y.git", "https://github.com/x/y?param=.git",
                       "https://github.com/x/y.git#fragment", "git@github.com:x/y.git",
                       "https://github.com/x/../y.git", "https://github.com/x%2fother/y.git",
                       "https://github.com/x/y;test.git"):
            with self.subTest(remote=remote), self.assertRaises(github.GitHubError):
                github.credential_helper(remote, ENV)
        self.run.assert_not_called()
        self.respond((0, b"Alice\n"), (0, returned(private=False, visibility="public")))
        with self.assertRaises(github.GitHubError) as error:
            github.credential_helper(REMOTE, ENV)
        self.assertEqual(error.exception.code, "github_not_private")

    def test_unavailable_helper_or_timeout_never_attempts_mutation(self):
        self.respond((0, b"Alice\n"), (1, None))
        self.assertEqual(self.connect(mode="create")["code"], "github_helper_unavailable")
        self.assertFalse(any("POST" in args for args in self.argv()))
        self.run.side_effect = subprocess.TimeoutExpired([SECRET], 20, output=SECRET.encode())
        self.assertEqual(self.call("github_status")["code"], "github_request_failed")

    def test_host_routes_github_calls_and_audits_without_payload(self):
        from skills_service import SkillsService

        folder = self.enterContext(tempfile.TemporaryDirectory())
        service = SkillsService(Path(folder))

        def dispatch(op, **payload):
            return service.dispatch({"agent": "codex", "op": op, "payload": payload})

        self.respond((0, b"Alice\n"), (0, b"Alice\n"), (0, returned()))
        self.assertTrue(dispatch("github_status")["authenticated"])
        connected = dispatch("github_connect", mode="select", repo=REPO, protocol="ssh", confirm=True, login="Alice")
        self.assertTrue(connected["ok"])
        activity = dispatch("activity")["entries"]
        self.assertEqual([row["op"] for row in activity], ["github_connect"])
        self.assertNotIn(REPO, json.dumps(activity))
        self.assertNotIn(SECRET, json.dumps(activity))


if __name__ == "__main__":
    unittest.main()

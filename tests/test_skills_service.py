import contextlib
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
import json
import os
from pathlib import Path
from queue import Queue
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

import backend
import build_desktop
import desktop
import skills_service


@contextlib.contextmanager
def _test_server(data_dir, host="127.0.0.1"):
    service = Mock()
    service.health.return_value = {"service": "codex-token-dashboard"}
    with backend.create_server(service, host, 0, skills_data_dir=data_dir) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join(timeout=3)


def _skills_request(server, data, *, method="POST", header_changes=(), duplicate=()):
    port = server.server_port
    host = f"127.0.0.1:{port}"
    headers = {"Host": host, "Origin": f"http://{host}", "X-Dashboard-Request": "1", "Content-Type": "application/json"}
    for name, value in header_changes:
        if value is None:
            headers.pop(name, None)
        else:
            headers[name] = value
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.putrequest(method, "/api/skills", skip_host=True)
        connection.putheader("Content-Length", str(len(data)))
        for key, value in (*headers.items(), *duplicate):
            connection.putheader(key, value)
        connection.endheaders(data)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), json.load(response)
    finally:
        connection.close()


class SkillsServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.library_cls = Mock()
        self.lib = self.library_cls.return_value
        self.lib.state.return_value = {"skills": []}
        self.lib.action.return_value = {"ok": True, "applied": True}
        self.source = Mock(return_value={"candidates": []})
        self.backup = Mock(return_value={"ok": True, "backups": []})
        modules = patch.dict(sys.modules, {
            "skills_core": types.SimpleNamespace(SkillLibrary=self.library_cls),
            "skills_sources": types.SimpleNamespace(handle=self.source),
            "skills_backup": types.SimpleNamespace(handle=self.backup),
        })
        modules.start()
        self.addCleanup(modules.stop)

    @staticmethod
    def envelope(op="state", payload=None, agent="codex"):
        return {"agent": agent, "op": op, "payload": {} if payload is None else payload}

    def test_dispatch_whitelist_and_redacted_errors(self):
        data = self.root / "data"
        service = skills_service.SkillsService(data)
        self.assertEqual(service.dispatch(self.envelope()), {"skills": []})
        self.library_cls.assert_called_once_with(data.resolve(), "codex")
        service.dispatch(self.envelope("local_preview", {"path": "example"}, "hermes"))
        self.source.assert_called_once_with(self.lib, "local_preview", {"path": "example"})
        self.library_cls.assert_called_with(data.resolve(), "hermes")
        service.dispatch(self.envelope("backup_list"))
        self.backup.assert_called_once_with(self.lib, "backup_list", {})
        service.dispatch(self.envelope("install", {"id": "demo"}))
        self.lib.action.assert_called_once_with("install", {"id": "demo"})
        service.dispatch(self.envelope("preset_delete", {"name": "demo"}))
        self.lib.action.assert_called_with("preset_delete", {"name": "demo"})
        for op in ("sources.local_preview", "backup.backup_list", "backup_arbitrary", "__dict__"):
            with self.subTest(op=op), self.assertRaisesRegex(skills_service.InvalidSkillsRequest, "invalid_operation"):
                service.dispatch(self.envelope(op))
        sensitive = "C:/private/secret https://user:pass@example.invalid/"
        self.backup.return_value = {"ok": False, "error": {"code": "invalid_remote", "message": sensitive}}
        with self.assertRaisesRegex(skills_service.InvalidSkillsRequest, "credential-free") as error:
            service.dispatch(self.envelope("sync_connect", {"remote_url": sensitive}))
        self.assertNotIn(sensitive, str(error.exception))
        self.source.side_effect = ValueError(sensitive)
        with self.assertRaisesRegex(skills_service.InvalidSkillsRequest, "new preview"):
            service.dispatch(self.envelope("local_preview", {"path": sensitive}))

    def test_github_route_requires_confirmation_and_preserves_safe_errors(self):
        import skills_github

        payload = {"mode": "select", "repo": "owner/example", "protocol": "https", "login": "owner"}
        expected = {"repo": "owner/example", "protocol": "https", "remote": "https://github.com/owner/example.git",
                    "created": False, "credential_helper": "gh", "ok": True}
        gh = Mock()
        gh.login.return_value = "owner"
        gh.verify.return_value = "owner/example"
        with (patch.object(skills_github, "_cli", return_value=contextlib.nullcontext(gh)) as cli,
              _test_server(self.root) as server):
            def call(op, options=None):
                return self.request(server, data=json.dumps(self.envelope(op, options)).encode())

            self.assertEqual(call("github_status")[2]["result"]["login"], "owner")
            cli.reset_mock()
            self.assertEqual(call("github_connect", payload)[0], 400)
            cli.assert_not_called()
            self.assertEqual(call("github_connect", {**payload, "confirm": True})[2], {"ok": True, "result": expected})
            gh.check_helper.assert_called_once_with()
            gh.create.assert_not_called()
            gh.verify.side_effect = skills_github.GitHubError("github_not_private")
            status, _, value = call("github_connect", {**payload, "confirm": True})
            self.assertEqual((status, value), (400, {"ok": False, "error": "Select a private GitHub repository."}))
            self.backup.assert_not_called()
            self.library_cls.assert_not_called()
            log = skills_service.activity_file(self.root, "codex").read_text(encoding="utf-8")
            self.assertEqual([(item["op"], item["result"]) for item in map(json.loads, log.splitlines())],
                             [("github_connect", "error"), ("github_connect", "ok"), ("github_connect", "error")])
            self.assertNotIn("owner/example", log)

    def test_activity_is_filtered_bounded_and_isolated_by_agent(self):
        service = skills_service.SkillsService(self.root)
        self.assertFalse((self.root / "skill_data").exists())
        service.dispatch(self.envelope())
        self.assertFalse((self.root / "skill_data").exists())
        with patch.object(skills_service, "ACTIVITY_LIMIT", 2):
            for op in ("install", "delete"):
                service.dispatch(self.envelope(op, {"id": "secret-id", "path": "C:/secret", "content": "secret"}))
            self.lib.action.return_value = {"ok": False, "code": "io_error", "error": "secret-token"}
            with self.assertRaisesRegex(ValueError, "permissions"):
                service.dispatch(self.envelope("disable", {"id": "secret-id"}))
            activity = service.dispatch(self.envelope("activity"))["entries"]
            self.assertEqual([(item["op"], item["result"]) for item in activity], [("disable", "error"), ("delete", "ok")])
            self.assertTrue(all(set(item) == {"time", "agent", "op", "result"} for item in activity))
            export = service.dispatch(self.envelope("activity_export"))
            self.assertEqual(export["filename"], "skills-activity-codex.jsonl")
            self.assertEqual([json.loads(line) for line in export["content"].splitlines()], activity)
            self.assertEqual(service.dispatch(self.envelope("activity", agent="hermes")), {"entries": []})
            self.assertNotIn("secret", skills_service.activity_file(self.root, "codex").read_text(encoding="utf-8"))

    def test_serializes_construction_and_calls_across_server_instances_and_agents(self):
        a, b = (skills_service.SkillsService(self.root) for _ in range(2))
        entered, contender, second, release = (threading.Event() for _ in range(4))

        def construct(data_dir, agent):
            if agent == "codex":
                entered.set()
                if not release.wait(3):
                    raise RuntimeError("test synchronization failed")
            else:
                second.set()
            return self.lib

        def run_second():
            contender.set()
            return b.dispatch(self.envelope("install", agent="hermes"))

        self.library_cls.side_effect = construct
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(a.dispatch, self.envelope("install"))
            try:
                self.assertTrue(entered.wait(2))
                next_request = pool.submit(run_second)
                self.assertTrue(contender.wait(2))
                self.assertFalse(second.wait(0.10))
            finally:
                release.set()
            self.assertTrue(first.result(timeout=2)["ok"])
            self.assertTrue(next_request.result(timeout=2)["ok"])
        self.assertTrue(second.is_set())

    def test_manager_requires_explicit_confirmation(self):
        service = skills_service.SkillsService(self.root)
        with self.assertRaisesRegex(ValueError, "Confirm"):
            service.dispatch(self.envelope("install_manager", {"confirm": 1}))
        self.lib.import_path.assert_not_called()
        service.dispatch(self.envelope("install_manager", {"confirm": True}, "hermes"))
        source = self.lib.import_path.call_args.args[0]
        self.assertTrue((source / "SKILL.md").is_file())
        self.lib.action.assert_called_once_with("install", {"id": source.name, "mode": "copy"})

    def request(self, server, *, method="POST", data=None, header_changes=(), duplicate=()):
        data = json.dumps(self.envelope()).encode() if data is None else data
        return _skills_request(server, data, method=method, header_changes=header_changes, duplicate=duplicate)

    def test_http_rejects_cross_site_hosts_duplicates_wildcard_and_bad_bodies(self):
        with _test_server(self.root) as server:
            success = self.request(server, header_changes=(("Sec-Fetch-Site", "same-origin"),))
            self.assertEqual(success[0], 200)
            self.assertEqual(success[2], {"ok": True, "result": {"skills": []}})
            self.assertEqual(success[1]["Cache-Control"], "no-store")
            port = server.server_port
            self.lib.reset_mock()
            cases = [(("Origin", "null"),), (("Origin", None),), (("Host", None),),
                     (("Host", "evil.invalid"), ("Origin", "http://evil.invalid")),
                     (("Host", f"127.0.0.2:{port}"), ("Origin", f"http://127.0.0.2:{port}")),
                     (("Origin", f"http://127.0.0.1:{port}/"),), (("X-Dashboard-Request", None),),
                     (("Sec-Fetch-Site", "same-site"),), (("Sec-Fetch-Site", "cross-site"),)]
            for headers in cases:
                with self.subTest(headers=headers):
                    status, response_headers, value = self.request(server, header_changes=headers)
                    self.assertEqual((status, value), (403, {"ok": False, "error": "forbidden_origin"}))
                    self.assertNotIn("Access-Control-Allow-Origin", response_headers)
            for duplicate in (("Host", f"127.0.0.1:{port}"), ("Origin", f"http://127.0.0.1:{port}"),
                              ("X-Dashboard-Request", "1")):
                self.assertEqual(self.request(server, duplicate=(duplicate,))[0], 403)
            self.assertEqual(self.request(server, duplicate=(("Content-Length", "5"),))[0], 400)
            self.assertEqual(self.request(server, method="OPTIONS", data=b"")[0], 403)
            self.assertEqual(self.request(server, method="GET", data=b"")[0], 405)
            self.assertEqual(self.request(server, data=b"[]")[0], 400)
            self.assertEqual(self.request(server, data=b"[" * 2000)[0], 400)
            self.assertEqual(self.request(server, header_changes=(("Content-Type", "text/plain"),))[0], 415)
            self.lib.state.assert_not_called()
        with _test_server(self.root, "0.0.0.0") as server:
            self.assertEqual(self.request(server)[0], 403)

    def test_cli_uses_http_and_does_not_mutate_without_apply(self):
        with _test_server(self.root / "workspace") as server:
            appdata = self.root / "profile"
            endpoint_dir = appdata / "CodexTokenDashboard"
            endpoint_dir.mkdir(parents=True)
            skills_service.publish_endpoint(endpoint_dir, server.server_port)
            command = [sys.executable, "-B", str(Path(skills_service.__file__)), "--agent", "hermes", "--op"]
            env = dict(os.environ, LOCALAPPDATA=str(appdata), HOME=str(self.root), USERPROFILE=str(self.root))
            options = dict(capture_output=True, text=True, env=env, timeout=10)
            denied = subprocess.run(command + ["install", "--port", str(server.server_port)], **options)
            self.assertEqual(json.loads(denied.stdout), {"ok": False, "error": "explicit_apply_required"})
            for op in ("github_status", "github_connect", "autosync_set", "autosync_resume"):
                denied = subprocess.run(command + [op, "--port", str(server.server_port)], **options)
                self.assertEqual(json.loads(denied.stdout), {"ok": False, "error": "explicit_apply_required"})
            self.lib.action.assert_not_called()
            allowed = subprocess.run(command + ["install", "--apply", "--payload-stdin", "--port", str(server.server_port)],
                                     input='{"id":"demo"}', **options)
            self.assertEqual(allowed.returncode, 0, allowed.stderr)
            self.lib.action.assert_called_once_with("install", {"id": "demo"})
            if sys.platform == "darwin":
                command += ["--port", str(server.server_port)]
            activity = subprocess.run(command + ["activity"], **options)
            self.assertEqual(json.loads(activity.stdout)["result"]["entries"][0]["op"], "install")

    def test_sync_conflict_and_stale_details_are_safe_and_cli_can_read_them(self):
        revision, digest = "a" * 40, "b" * 64
        conflict = {"remote_revision": revision, "local_digest": digest, "choices": ["keep_local", "use_remote", "keep_both"],
                    "skill_ids": ["my-skill"], "preset_names": ["test preset"], "baseline_available": True}
        sensitive = "https://user:secret@private.invalid/a"
        status = {"connected": True, "branch": "skills-codex", "state": "conflict",
                  "local_changed": None, "remote_checked": True, "remote_revision": revision}
        self.backup.return_value = {"ok": False, "error": {"code": "sync_conflict", "message": sensitive},
                                    "status": {**status, "remote_url": sensitive},
                                    "conflict": {**conflict, "token": sensitive,
                                                 "skill_ids": ["my-skill", sensitive]}, "local_backup": sensitive}
        with _test_server(self.root) as server:
            http, _, body = self.request(server, data=json.dumps(self.envelope("sync_push", {"confirm": True})).encode())
            self.assertEqual(http, 409)
            self.assertEqual(body, {"ok": False, "error": skills_service._PUBLIC_ERRORS["sync_conflict"],
                                    "code": "sync_conflict", "conflict": conflict, "status": status})
            command = [sys.executable, "-B", str(Path(skills_service.__file__)), "--agent", "codex", "--op", "sync_push",
                       "--apply", "--payload-stdin", "--port", str(server.server_port)]
            completed = subprocess.run(command, input='{"confirm":true}', capture_output=True, text=True, timeout=10)
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(json.loads(completed.stdout), body)

            self.backup.return_value["error"]["code"] = "sync_conflict_stale"
            self.backup.return_value["conflict"]["local_digest"] = sensitive
            self.backup.return_value["status"]["branch"] = "skills-hermes"
            http, _, stale = self.request(server, data=json.dumps(self.envelope("sync_resolve", {"confirm": True})).encode())
            self.assertEqual(http, 409)
            self.assertEqual(stale["code"], "sync_conflict_stale")
            self.assertNotIn("conflict", stale)
            self.assertNotIn("branch", stale["status"])
            self.assertNotIn(sensitive, json.dumps(stale))

        self.assertTrue(skills_service._cli_envelope(body, 409, "sync_push", "codex"))
        for invalid in ({**body, "token": sensitive},
                        {**body, "status": {**status, "remote_url": sensitive}},
                        {**body, "conflict": {**conflict, "local_digest": "not-a-digest"}},
                        {**body, "conflict": {**conflict, "choices": ["keep_both", "keep_both"]}}):
            self.assertFalse(skills_service._cli_envelope(invalid, 409, "sync_push", "codex"))
        self.assertFalse(skills_service._cli_envelope(body, 409, "state", "codex"))
        self.assertFalse(skills_service._cli_envelope(body, 400, "sync_push", "codex"))

    def test_dialog_checks_origin_and_never_writes(self):
        bridge = desktop.DesktopBridge(types.SimpleNamespace(url="http://127.0.0.1:49123/?desktop=1"))
        window = Mock()
        bridge.attach_window(window)
        window.get_current_url.return_value = "https://example.invalid/"
        self.assertEqual(bridge.choose_skills_directory("codex")["error"], "forbidden_origin")
        window.create_file_dialog.assert_not_called()
        window.get_current_url.return_value = "http://127.0.0.1:49123/"
        window.create_file_dialog.return_value = (str(self.root / "not-created"),)
        webview = types.SimpleNamespace(FileDialog=types.SimpleNamespace(FOLDER=1))
        with patch.dict(sys.modules, {"webview": webview}):
            self.assertEqual(bridge.choose_skills_directory("hermes"), {"ok": True, "path": str(self.root / "not-created")})
        self.assertFalse((self.root / "not-created").exists())

    def test_packaging_stages_and_hashes_runtime_modules_and_bundled_skill(self):
        source = self.root / "source"
        source.mkdir()
        for filename in build_desktop.RUNTIME_FILES:
            (source / filename).write_text("fixture", encoding="utf-8")
        (source / "skills_unrelated.py").touch()
        manager = Path("assets/codex-token-skill-manager/SKILL.md")
        (source / manager).parent.mkdir(parents=True)
        (source / manager).write_text("manager", encoding="utf-8")
        stage = self.root / "stage"
        build_desktop.stage_source(source, stage)
        new_modules = ("skills_github.py", "skills_autosync.py")
        for name in ("hermes_source.py", "skills_core.py", "skills_sources.py", "skills_backup.py", "skills_service.py",
                     *new_modules, manager):
            self.assertTrue((stage / name).is_file())
        self.assertFalse((stage / "skills_unrelated.py").exists())
        original = build_desktop.source_digest(stage)
        for name in (*new_modules, manager):
            with self.subTest(name=name):
                path = stage / name
                old = path.read_bytes()
                path.write_text("changed", encoding="utf-8")
                self.assertNotEqual(original, build_desktop.source_digest(stage))
                path.write_bytes(old)


class RealSkillsServiceTests(unittest.TestCase):
    def test_developer_and_lan_servers_do_not_start_worker(self):
        from skills_autosync import AutoSyncManager, Observation

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            adapter = Mock()
            adapter.observe.return_value = Observation("connection-fingerprint", "digest", True)
            manager = AutoSyncManager(root, adapter=adapter)
            with patch("skills_autosync.get_autosync", return_value=manager), patch.object(manager, "start") as start:
                with _test_server(root) as server:
                    request = {"agent": "codex", "op": "autosync_set", "payload": {"enabled": True, "confirm": True}}
                    status, _, response = _skills_request(server, json.dumps(request).encode())
                    self.assertEqual(status, 200)
                    self.assertTrue(response["result"]["status"]["enabled"])
                    options = dict(capture_output=True, text=True, timeout=10)
                    completed = subprocess.run([sys.executable, "-B", skills_service.__file__, "--port",
                                                str(server.server_port), "--agent", "codex", "--op", "autosync_status"], **options)
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    self.assertTrue(json.loads(completed.stdout)["result"]["status"]["enabled"])
                with _test_server(root, "0.0.0.0") as server:
                    self.assertEqual(_skills_request(server, json.dumps(request).encode())[0], 403)
                start.assert_not_called()
                adapter.transfer.assert_not_called()
                adapter.observe.assert_called_once_with("codex")

    def test_desktop_worker_requires_consent_and_cancels_on_disable_and_stop(self):
        from skills_autosync import AutoSyncManager, Observation

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            adapter = Mock()
            adapter.observe.return_value = Observation("connection-fingerprint", "digest", True)
            transfers = Queue()
            release = threading.Event()
            ticked = threading.Event()

            def transfer(agent, op, observed, cancelled):
                transfers.put((agent, op, cancelled))
                release.wait(5)
                return {"ok": True}

            adapter.transfer.side_effect = transfer
            manager = AutoSyncManager(root, adapter=adapter, debounce=0.01, poll_interval=0.01, pull_interval=10)
            tick = manager.tick

            def signal_tick():
                tick()
                ticked.set()

            with (patch.object(desktop, "local_data_dir", return_value=root),
                  patch.object(desktop, "DashboardService"), patch("skills_autosync.get_autosync", return_value=manager),
                  patch.object(manager, "tick", side_effect=signal_tick)):
                runtime = desktop.DesktopRuntime(database=Mock())

                def call(op, payload=None, agent="codex", headers=()):
                    data = json.dumps({"agent": agent, "op": op, "payload": payload or {}}).encode()
                    status, _, value = _skills_request(runtime.server, data, header_changes=headers)
                    return status, value

                try:
                    runtime.start()
                    self.assertIs(runtime.server.skills_service.autosync, manager)
                    self.assertTrue(ticked.wait(2))
                    self.assertEqual(call("autosync_status")[1]["result"]["status"]["state"], "disabled")
                    enable = {"enabled": True, "confirm": True}
                    self.assertEqual(call("autosync_set", enable, headers=(("Origin", "https://evil.invalid"),))[0], 403)
                    self.assertEqual(call("autosync_set", {"enabled": True})[0], 400)
                    self.assertEqual(call("autosync_resume", {"confirm": True})[0], 400)
                    adapter.observe.assert_not_called()
                    adapter.transfer.assert_not_called()
                    self.assertTrue(call("autosync_set", enable)[1]["result"]["status"]["enabled"])
                    agent, op, cancelled = transfers.get(timeout=2)
                    self.assertEqual((agent, op), ("codex", "sync_push"))
                    self.assertFalse(cancelled.is_set())
                    self.assertFalse(call("autosync_status", agent="hermes")[1]["result"]["status"]["enabled"])

                    # The fake transfer is still in flight: the HTTP disable must not wait for it.
                    self.assertFalse(call("autosync_set", {"enabled": False})[1]["result"]["status"]["enabled"])
                    self.assertTrue(cancelled.is_set())
                    self.assertFalse(AutoSyncManager(root, adapter=adapter).status("codex")["enabled"])
                    ticked.clear()
                    release.set()
                    self.assertTrue(ticked.wait(2))
                    self.assertEqual(call("autosync_status")[1]["result"]["status"]["state"], "disabled")
                    self.assertEqual(adapter.transfer.call_count, 1)

                    release.clear()
                    self.assertEqual(call("autosync_set", enable)[0], 200)
                    _, _, cancelled = transfers.get(timeout=2)
                    self.assertFalse(cancelled.is_set())
                    runtime.stop()
                    self.assertTrue(cancelled.is_set())
                    release.set()
                    self.assertTrue(manager.stop(wait=2))
                    self.assertEqual(manager.status("codex")["state"], "stopped")
                    self.assertEqual(adapter.transfer.call_count, 2)
                finally:
                    release.set()
                    runtime.stop()
                    manager.stop(wait=2)

    def test_bundled_manager_installs_for_the_selected_agent_only(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            codex, hermes = root / "codex", root / "hermes"
            with patch.dict(os.environ, CODEX_HOME=str(codex), HERMES_HOME=str(hermes)):
                service = skills_service.SkillsService(root / "app")
                result = service.dispatch({"agent": "hermes", "op": "install_manager", "payload": {"confirm": True}})
                self.assertTrue(result["ok"])
                self.assertTrue((hermes / "skills" / "codex-token-skill-manager" / "SKILL.md").is_file())
                self.assertFalse(codex.exists())


if __name__ == "__main__":
    unittest.main()

import errno
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid
import zipfile

import skills_backup as backup


def portable(text=None):
    return portable_library({} if text is None else {"test": text})


def portable_library(skills, presets=None):
    presets = presets or {}
    entries = [{"id": key, "tags": [], "dirs": [], "executable": []} for key in sorted(skills)]
    groups = [{"name": key, "ids": presets[key]} for key in sorted(presets, key=lambda name: (name.casefold(), name))]
    return {"manifest.json": json.dumps({"schema": 1, "skills": entries, "presets": groups},
                                        sort_keys=True, separators=(",", ":")).encode(),
            **{f"skills/{key}/SKILL.md": text.encode() for key, text in skills.items()}}


class Library:
    def __init__(self, data_dir, agent="codex", text=None):
        self.data_dir = data_dir
        data_dir.mkdir(exist_ok=True)
        self.agent = agent
        self.library_id = str(uuid.uuid4())
        self.files = portable(text)
        self.root = "Unshared Real Root"
        self.token = "Unshared Token"
        self.import_calls = 0

    @property
    def backups(self):
        return self.data_dir / "skills-backup" / self.agent / uuid.UUID(self.library_id).hex / "backups"

    def export_portable(self):
        return dict(self.files)

    def import_portable(self, files, *, expected_current=None):
        self.import_calls += 1
        if expected_current != self.files:
            return {"ok": False, "code": "library_changed"}
        self.files = dict(files)
        return {"ok": True}


class BackupTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="skills-backup-test-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.a = Library(self.root / "a", text="original")

    def call(self, lib, op, **payload):
        response = backup.handle(lib, op, payload)
        json.dumps(response)
        return response

    def success(self, lib, op, **payload):
        result = self.call(lib, op, **payload)
        self.assertTrue(result["ok"], result)
        return result

    def failure(self, lib, op, code, **payload):
        result = self.call(lib, op, **payload)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["error"]["code"], code, result)
        return result

    def test_backup_scope_and_restore_with_rollback_snapshot(self):
        other = Library(self.a.data_dir, "hermes", "other agent")
        (self.a.data_dir / "credentials.json").write_text("private config", encoding="utf-8")
        rec = self.success(self.a, "backup_create")["backup"]
        ident = rec["backup_id"]
        with zipfile.ZipFile(self.a.backups / (ident + ".zip")) as archive:
            self.assertEqual(set(archive.namelist()), {"manifest.json", "files/manifest.json", "files/skills/test/SKILL.md"})
            all_content = b"".join(archive.read(name) for name in archive.namelist())
            for private in [self.a.root, self.a.token, str(self.a.data_dir), "private config", "other agent"]:
                self.assertNotIn(private.encode(), all_content)
        self.a.files = portable("latest")
        self.failure(self.a, "backup_restore", "confirmation_required", backup_id=ident, confirm=1)
        restored = self.success(self.a, "backup_restore", backup_id=ident, confirm=True)
        self.assertEqual(self.a.files, portable("original"))
        self.assertEqual(restored["safety_backup"]["reason"], "before_restore")
        self.assertEqual(self.success(other, "backup_list")["backups"], [])
        self.assertEqual(self.success(self.a, "backup_list")["backups"][0], restored["safety_backup"])
        self.success(self.a, "backup_restore", backup_id=restored["safety_backup"]["backup_id"], confirm=True)
        self.assertEqual(self.a.files, portable("latest"))
        self.assertEqual((self.a.data_dir / "credentials.json").read_text(), "private config")

    def test_copied_archive_refuses_other_library_and_agent(self):
        rec = self.success(self.a, "backup_create")["backup"]
        name = rec["backup_id"] + ".zip"
        for dest in [Library(self.root / "b"), Library(self.a.data_dir, "hermes")]:
            self.success(dest, "backup_list")
            shutil.copyfile(self.a.backups / name, dest.backups / name)
            self.failure(dest, "backup_restore", "foreign_backup", backup_id=rec["backup_id"], confirm=True)
            self.assertEqual(self.success(dest, "backup_list")["skipped"], 1)
            self.assertEqual(dest.import_calls, 0)

    def test_unsafe_zip_members_never_reach_import(self):
        ident = self.success(self.a, "backup_create")["backup"]["backup_id"]
        path = self.a.backups / (ident + ".zip")
        with zipfile.ZipFile(path) as archive:
            manifest = archive.read("manifest.json")
        for names, mode in [(["../escaped"], stat.S_IFREG), (["x\\y"], stat.S_IFREG),
                            (["C:/escaped"], stat.S_IFREG), (["a/NUL.txt"], stat.S_IFREG),
                            (["foo/a", "FOO/b"], stat.S_IFREG), (["x", "x/y"], stat.S_IFREG),
                            ([".env"], stat.S_IFREG), (["linked"], stat.S_IFLNK)]:
            with self.subTest(names=names, mode=mode), zipfile.ZipFile(path, "w") as archive:
                archive.writestr("manifest.json", manifest)
                for name in names:
                    entry = zipfile.ZipInfo("files/" + name)
                    entry.create_system = 3
                    entry.external_attr = (mode | 0o600) << 16
                    archive.writestr(entry, b"../outside")
            self.failure(self.a, "backup_restore", "invalid_archive", backup_id=ident, confirm=True)
        self.assertEqual(self.a.import_calls, 0)
        self.failure(self.a, "backup_restore", "invalid_backup_id", backup_id="../outside", confirm=True)

    def test_restore_compare_and_swap_keeps_concurrent_edit(self):
        ident = self.success(self.a, "backup_create")["backup"]["backup_id"]
        original_import = self.a.import_portable

        def race(files, *, expected_current):
            self.a.files = portable("concurrent")
            return original_import(files, expected_current=expected_current)

        with patch.object(self.a, "import_portable", side_effect=race):
            result = self.failure(self.a, "backup_restore", "library_changed", backup_id=ident, confirm=True)
        self.assertEqual(self.a.files, portable("concurrent"))
        self.assertEqual(result["safety_backup"]["reason"], "before_restore")

    def test_confirmation_and_url_validation_are_offline(self):
        with patch.object(backup, "_Git") as git:
            for url in ["https://user:secret@example.com/x", "https://example.com/r?token=secret",
                        "ext::echo hi", "http://example.com/r", "../relative.git"]:
                self.failure(self.a, "sync_connect", "invalid_remote", confirm=True, remote_url=url)
            self.failure(self.a, "sync_connect", "invalid_payload", confirm=True,
                         remote_url="https://example.com/repo", token="secret")
            self.failure(self.a, "sync_connect", "confirmation_required", remote_url="https://example.com/repo")
            self.failure(self.a, "sync_push", "sync_not_connected", confirm=True)
            result = self.success(self.a, "sync_connect", confirm=True, remote_url="https://example.com/repo")
            self.assertFalse(result["status"]["remote_checked"])
            self.success(self.a, "sync_status")
            git.assert_not_called()

    def test_gh_helper_configuration_is_offline_and_only_saves_mode(self):
        url = "https://github.com/example/private.git"
        with patch.dict(sys.modules, {"skills_github": None}):
            self.failure(self.a, "sync_connect", "invalid_remote", confirm=True, credential_helper="gh",
                         remote_url="https://example.com/repo.git")
            connected = self.success(self.a, "sync_connect", confirm=True, credential_helper="gh", remote_url=url)
            self.assertEqual(connected["status"]["credential_helper"], "gh")
            saved = self.a.backups.parent / "sync.json"
            self.assertEqual(json.loads(saved.read_text())["credential_helper"], "gh")
            self.assertIsNone(self.success(self.a, "sync_connect", confirm=True, remote_url=url)["status"]["credential_helper"])
            self.assertNotIn("credential_helper", json.loads(saved.read_text()))

    def test_gh_helper_arguments_and_environment_are_transfer_only(self):
        url = "https://github.com/example/private.git"
        helper = SimpleNamespace(credential_helper=lambda remote, env: (
            ["-c", "credential.helper=test-marker"], {**env, "TEST_SANITIZED": "yes"}))
        with patch.dict(sys.modules, {"skills_github": helper}), patch.object(backup._Git, "run") as run:
            context = backup._Git("git", self.root, url, "https", "codex", "gh")
            self.assertLess(context.command.index("credential.helper="), context.command.index("credential.helper=test-marker"))
            self.assertEqual(context.env["TEST_SANITIZED"], "yes")
            run.assert_called_once()
        class VerificationError(ValueError):
            code = "github_not_private"
        with patch.dict(sys.modules, {"skills_github": helper}), \
                patch.object(helper, "credential_helper", side_effect=VerificationError("private detail")), \
                patch.object(backup._Git, "run") as run:
            with self.assertRaises(backup._Error) as failure:
                backup._Git("git", self.root, url, "https", "codex", "gh")
            self.assertEqual(failure.exception.code, "github_not_private")
            self.assertNotIn("private detail", str(failure.exception))
            run.assert_not_called()

    def test_rejects_repacked_snapshot_with_device_configuration(self):
        rec = self.success(self.a, "backup_create")["backup"]
        path = self.a.backups / (rec["backup_id"] + ".zip")
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        files = {**self.a.files, "settings.json": b'{"token":"not portable"}'}
        manifest["digest"] = backup._digest(files)
        path.write_bytes(backup._pack(files, manifest))
        self.failure(self.a, "backup_restore", "invalid_archive", backup_id=rec["backup_id"], confirm=True)
        self.assertEqual(self.a.import_calls, 0)

    def test_real_library_preserves_root_and_managed_deployment(self):
        from skills_core import SkillLibrary

        codex_home = self.root / "codex-home"
        hermes_home = self.root / "hermes-home"
        connect_root = self.root / "selected-root"
        with patch.dict(os.environ, {"CODEX_HOME": str(codex_home), "HERMES_HOME": str(hermes_home)}):
            lib = SkillLibrary(self.root / "real-library", "codex")
            self.assertTrue(lib.action("root", {"path": str(connect_root)})["ok"])
            empty = self.success(lib, "backup_create")["backup"]["backup_id"]
            source = self.root / "example-skill"
            source.mkdir()
            body = "---\nname: example-skill\ndescription: An example skill\n---\nInstructions.\n"
            (source / "SKILL.md").write_text(body, encoding="utf-8")
            lib.import_path(source, source={"url": str(source)})
            full = self.success(lib, "backup_create")["backup"]["backup_id"]
            installed = lib.action("install", {"id": source.name, "mode": "copy"})
            self.assertTrue(installed["ok"], installed)
            external = connect_root / "external-skill"
            external.mkdir()
            (external / "SKILL.md").write_text(body, encoding="utf-8")
            self.failure(lib, "backup_restore", "deployments_active", backup_id=empty, confirm=True)
            self.assertTrue((connect_root / source.name / "SKILL.md").is_file())
            self.assertTrue(lib.action("disable", {"id": source.name})["ok"])
            self.success(lib, "backup_restore", backup_id=empty, confirm=True)
            self.assertEqual(lib.export_portable().keys(), {"manifest.json"})
            self.success(lib, "backup_restore", backup_id=full, confirm=True)
            self.assertEqual(lib.export_portable()["skills/example-skill/SKILL.md"].decode().replace("\r\n", "\n"), body)
            self.assertEqual(lib.state()["root"], str(connect_root))
            self.assertTrue((external / "SKILL.md").is_file())
            self.assertFalse((connect_root / source.name).exists())


@unittest.skipUnless(shutil.which("git"), "Git required for the local bare remote integration tests")
class GitTests(unittest.TestCase):
    call = BackupTests.call
    success = BackupTests.success
    failure = BackupTests.failure

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="skills-git-test-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.remote = self.root / "local remote.git"
        self.git("init", "--bare", "--quiet", "--template=", str(self.remote))
        self.a = Library(self.root / "a", text="base")
        self.b = Library(self.root / "b")
        for lib in (self.a, self.b):
            self.success(lib, "sync_connect", remote_url=str(self.remote), confirm=True)

    def git(self, *args):
        return subprocess.run([shutil.which("git"), *args], cwd=self.root, check=True,
                              capture_output=True).stdout.decode().strip()

    def remote_head(self):
        return self.git("--git-dir=" + str(self.remote), "rev-parse", "refs/heads/skills-codex")

    def seed(self):
        result = self.success(self.a, "sync_push", confirm=True)
        self.assertTrue(result["changed"])
        self.assertEqual(result["status"]["revision"], self.remote_head())
        self.failure(self.b, "sync_push", "remote_ahead", confirm=True)
        pulled = self.success(self.b, "sync_pull", confirm=True)
        self.assertEqual(pulled["safety_backup"]["reason"], "before_pull")
        self.assertEqual(self.b.files, self.a.files)

    def diverge(self):
        self.seed()
        self.a.files = portable("remote side")
        self.b.files = portable("local side")
        self.success(self.a, "sync_push", confirm=True)
        before = self.remote_head()
        for op in ("sync_push", "sync_pull"):
            result = self.failure(self.b, op, "sync_conflict", confirm=True)
            self.assertEqual(result["conflict"]["remote_revision"], before)
            self.assertEqual(self.remote_head(), before)
            self.assertEqual(self.b.files, portable("local side"))
        return {"expected_remote_revision": result["conflict"]["remote_revision"],
                "expected_local_digest": result["conflict"]["local_digest"]}

    def check_resolution_copies(self, result):
        for side in ("local", "remote"):
            record = result[side + "_backup"]
            self.assertEqual(record["reason"], "conflict_" + side)
            self.success(self.b, "backup_restore", backup_id=record["backup_id"], confirm=True)
            self.assertEqual(self.b.files, portable(side + " side"))

    def test_disposable_git_workspace_disables_background_maintenance(self):
        with tempfile.TemporaryDirectory(dir=self.root) as folder:
            git = backup._Git(shutil.which("git"), Path(folder), str(self.remote), "file", "codex")
            self.assertEqual(git.run("config", "--bool", "--get", "maintenance.auto",
                                     allowed=(0, 1)).stdout.strip(), b"false")
            self.assertEqual(git.run("config", "--get", "gc.auto", allowed=(0, 1)).stdout.strip(), b"0")

    def test_transient_scratch_cleanup_does_not_hide_sync_conflict(self):
        self.seed()
        self.a.files = portable("remote side")
        self.b.files = portable("local side")
        self.success(self.a, "sync_push", confirm=True)
        before = self.remote_head()
        cleanup = tempfile.TemporaryDirectory.cleanup
        attempts = []

        def delayed_cleanup(directory):
            path = Path(directory.name)
            if path.name.startswith("git-"):
                attempts.append(path)
                if len(attempts) == 1:
                    raise OSError(errno.ENOTEMPTY, "simulated helper race", directory.name)
            return cleanup(directory)

        with patch.object(tempfile.TemporaryDirectory, "cleanup", delayed_cleanup):
            result = self.failure(self.b, "sync_pull", "sync_conflict", confirm=True)
        self.assertEqual(len(attempts), 2)
        self.assertFalse(attempts[0].exists())
        self.assertEqual(result["conflict"]["remote_revision"], before)
        self.assertEqual(self.remote_head(), before)
        self.assertEqual(self.b.files, portable("local side"))

    def test_persistent_scratch_cleanup_preserves_completed_push_and_logs_safely(self):
        rmtree = shutil.rmtree

        def denied_cleanup(path, *args, **kwargs):
            if Path(path).name.startswith("git-"):
                raise PermissionError(errno.EACCES, "private token or path", path)
            return rmtree(path, *args, **kwargs)

        with self.assertLogs("skills_backup", level="WARNING") as logs:
            with patch.object(shutil, "rmtree", denied_cleanup):
                result = self.success(self.a, "sync_push", confirm=True)
        self.assertTrue(result["changed"])
        self.assertEqual(result["status"]["revision"], self.remote_head())
        logged = " ".join(logs.output)
        self.assertIn("PermissionError", logged)
        self.assertNotIn("private token or path", logged)
        self.assertNotIn(str(self.root), logged)

    def test_push_pull_and_agent_branches(self):
        self.seed()
        self.a.files = portable("next")
        self.assertTrue(self.success(self.a, "sync_status")["status"]["local_changed"])
        self.assertFalse(self.success(self.a, "sync_pull", confirm=True)["changed"])
        self.success(self.a, "sync_push", confirm=True)
        self.success(self.b, "sync_pull", confirm=True)
        self.assertEqual(self.b.files, portable("next"))
        self.assertFalse(self.success(self.b, "sync_push", confirm=True)["changed"])
        other = Library(self.root / "b", "hermes", "hermes only")
        self.success(other, "sync_connect", remote_url=str(self.remote), confirm=True)
        self.success(other, "sync_push", confirm=True)
        self.assertEqual(self.b.files, portable("next"))
        refs = self.git("--git-dir=" + str(self.remote), "for-each-ref", "--format=%(refname)")
        self.assertEqual(set(refs.splitlines()), {"refs/heads/skills-codex", "refs/heads/skills-hermes"})

    def test_keep_local_advances_confirmed_revision_and_both_copies_restore(self):
        payload = self.diverge()
        self.failure(self.b, "sync_resolve", "confirmation_required", strategy="keep_local", **payload)
        result = self.success(self.b, "sync_resolve", confirm=True, strategy="keep_local", **payload)
        head = self.remote_head()
        self.assertEqual(self.git("--git-dir=" + str(self.remote), "rev-parse", head + "^"),
                         payload["expected_remote_revision"])
        self.success(self.a, "sync_pull", confirm=True)
        self.assertEqual(self.a.files, portable("local side"))
        self.check_resolution_copies(result)

    def test_use_remote_keeps_git_head_and_both_copies_restore(self):
        payload = self.diverge()
        result = self.success(self.b, "sync_resolve", confirm=True, strategy="use_remote", **payload)
        self.assertEqual(self.remote_head(), payload["expected_remote_revision"])
        self.assertEqual(self.b.files, portable("remote side"))
        self.check_resolution_copies(result)

    def test_changed_remote_or_local_requires_new_confirmation(self):
        payload = self.diverge()
        self.b.files = portable("local changed again")
        result = self.failure(self.b, "sync_resolve", "sync_conflict_stale", confirm=True,
                              strategy="use_remote", **payload)
        payload["expected_local_digest"] = result["conflict"]["local_digest"]
        self.a.files = portable("remote changed again")
        self.success(self.a, "sync_push", confirm=True)
        head = self.remote_head()
        result = self.failure(self.b, "sync_resolve", "sync_conflict_stale", confirm=True,
                              strategy="keep_local", **payload)
        self.assertEqual(result["conflict"]["remote_revision"], head)
        self.assertEqual(self.remote_head(), head)
        self.assertEqual(self.b.files, portable("local changed again"))

    def test_deleted_branch_between_confirmation_and_push_is_not_recreated(self):
        payload = self.diverge()
        original = backup._Git.push

        def remove_ref_then_push(git, data, parent):
            self.git("--git-dir=" + str(self.remote), "update-ref", "-d", "refs/heads/skills-codex", parent)
            return original(git, data, parent)

        with patch.object(backup._Git, "push", new=remove_ref_then_push):
            result = self.failure(self.b, "sync_resolve", "sync_conflict_stale", confirm=True,
                                  strategy="keep_local", **payload)
        self.assertEqual(self.git("--git-dir=" + str(self.remote), "for-each-ref"), "")
        self.assertEqual(self.b.files, portable("local side"))
        self.check_resolution_copies(result)

    def test_three_way_pull_combines_edits_additions_deletions_and_presets_without_pushing(self):
        self.a.files = portable_library({"a": "a0", "b": "b0", "obsolete": "old"}, {"A": ["a"], "B": ["b"]})
        self.seed()
        base = self.remote_head()
        self.a.files = portable_library({"a": "a1", "b": "b0", "r": "new remote"}, {"A": ["a", "r"], "B": ["b"]})
        self.success(self.a, "sync_push", confirm=True)
        self.a.files = portable_library({"a": "a2", "b": "b0", "r": "new remote"}, {"A": ["a", "r"], "B": ["b"]})
        self.success(self.a, "sync_push", confirm=True)
        remote_before = self.remote_head()
        self.b.files = portable_library({"a": "a0", "b": "b1", "obsolete": "old", "l": "new local"},
                                        {"A": ["a"], "B": ["b", "l"]})
        local_before = self.b.files.copy()
        pulled = self.success(self.b, "sync_pull", confirm=True)
        self.assertFalse(pulled["merge"]["published"])
        self.assertEqual(pulled["merge"]["mode"], "auto")
        self.assertEqual(pulled["status"]["state"], "local_changes")
        self.assertNotEqual(base, remote_before)
        self.assertEqual(self.remote_head(), remote_before)
        expected = portable_library({"a": "a2", "b": "b1", "l": "new local", "r": "new remote"},
                                    {"A": ["a", "r"], "B": ["b", "l"]})
        self.assertEqual(self.b.files, expected)
        self.assertTrue(self.success(self.b, "sync_push", confirm=True)["changed"])
        self.success(self.a, "sync_pull", confirm=True)
        self.assertEqual(self.a.files, expected)
        for side, files in [("local", local_before), ("remote", portable_library(
                {"a": "a2", "b": "b0", "r": "new remote"}, {"A": ["a", "r"], "B": ["b"]}))]:
            self.success(self.b, "backup_restore", confirm=True, backup_id=pulled[side + "_backup"]["backup_id"])
            self.assertEqual(self.b.files, files)

    def test_three_way_push_combines_disjoint_changes_and_is_fast_forward(self):
        self.seed()
        self.a.files = portable_library({"test": "remote update"}, {"remote": ["test"]})
        self.success(self.a, "sync_push", confirm=True)
        prior = self.remote_head()
        self.b.files = portable_library({"test": "base", "new": "local"}, {"local": ["new"]})
        result = self.success(self.b, "sync_push", confirm=True)
        self.assertTrue(result["merge"]["published"])
        self.assertEqual(result["status"]["state"], "in_sync")
        self.assertEqual(self.git("--git-dir=" + str(self.remote), "rev-parse", self.remote_head() + "^"), prior)
        self.success(self.a, "sync_pull", confirm=True)
        self.assertEqual(self.a.files, self.b.files)
        self.assertEqual(self.b.files, portable_library({"test": "remote update", "new": "local"},
                                                        {"remote": ["test"], "local": ["new"]}))

    def test_three_way_push_checks_local_cas_and_rolls_back_a_rejected_push(self):
        self.seed()
        self.a.files = portable_library({"test": "remote update"})
        self.success(self.a, "sync_push", confirm=True)
        prior = self.remote_head()
        current = self.b.files = portable_library({"test": "base", "new": "local"})
        with patch.object(self.b, "import_portable", return_value={"ok": False, "code": "library_changed"}), \
                patch.object(backup._Git, "push") as send:
            result = self.failure(self.b, "sync_push", "library_changed", confirm=True)
            self.assertFalse(result["local_merge_applied"])
            send.assert_not_called()
        with patch.object(backup._Git, "push", side_effect=backup._Error("remote_changed", "Remote moved.")):
            result = self.failure(self.b, "sync_push", "remote_changed", confirm=True)
            self.assertFalse(result["local_merge_applied"])
        self.assertEqual(self.remote_head(), prior)
        self.assertEqual(self.b.files, current)
        self.assertIn("remote_backup", result)

    def test_keep_both_renames_remote_collision_and_rewrites_presets(self):
        self.seed()
        self.a.files = portable_library({"test": "remote", "additional": "remote"}, {"Release": ["test", "additional"]})
        self.b.files = portable_library({"test": "local", "TEST-REMOTE": "reserved"}, {"Release": ["test"]})
        self.success(self.a, "sync_push", confirm=True)
        result = self.failure(self.b, "sync_pull", "sync_conflict", confirm=True)
        conflict = result["conflict"]
        self.assertEqual(conflict["skill_ids"], ["test"])
        self.assertEqual(conflict["preset_names"], ["Release"])
        self.assertTrue(conflict["baseline_available"])
        resolved = self.success(self.b, "sync_resolve", confirm=True, strategy="keep_both",
                                expected_remote_revision=conflict["remote_revision"],
                                expected_local_digest=conflict["local_digest"])
        expected = portable_library({"test": "local", "test-remote-2": "remote", "TEST-REMOTE": "reserved",
                                     "additional": "remote"},
                                    {"Release": ["test"], "Release (remote)": ["test-remote-2", "additional"]})
        self.assertEqual(self.b.files, expected)
        self.assertEqual(resolved["merge"]["renamed_skills"], [{"from_id": "test", "to_id": "test-remote-2"}])
        self.assertEqual(resolved["merge"]["renamed_presets"], [{"from_name": "Release", "to_name": "Release (remote)"}])
        self.assertEqual(self.git("--git-dir=" + str(self.remote), "rev-parse", self.remote_head() + "^"),
                         conflict["remote_revision"])
        self.success(self.a, "sync_pull", confirm=True)
        self.assertEqual(self.a.files, expected)

    def test_unrelated_history_cannot_auto_merge_after_rewrite(self):
        self.seed()
        self.b.files = portable_library({"test": "base", "local": "local"})
        self.git("--git-dir=" + str(self.remote), "update-ref", "-d", "refs/heads/skills-codex")
        c = Library(self.root / "new-device")
        c.files = portable_library({"test": "base", "remote": "remote"})
        self.success(c, "sync_connect", remote_url=str(self.remote), confirm=True)
        self.success(c, "sync_push", confirm=True)
        result = self.failure(self.b, "sync_pull", "sync_conflict", confirm=True)
        self.assertFalse(result["conflict"]["baseline_available"])
        self.assertEqual(self.b.files, portable_library({"test": "base", "local": "local"}))

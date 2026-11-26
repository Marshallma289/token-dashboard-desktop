import json
import os
import tempfile
import unittest
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from skills_core import SkillLibrary, link_directory
from skills_service import SkillsService
import skills_sources
from skills_upstream import UpstreamWorker, run_updates


class SkillAutomationTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(patch.dict("os.environ", {
            "CODEX_HOME": str(self.base / "codex"),
            "HERMES_HOME": str(self.base / "hermes"),
            "XDG_STATE_HOME": str(self.base / "xdg"),
            "DEEPL_AUTH_KEY": "test-only:fx",
        }))
        self.lib = SkillLibrary(self.base / "data", "codex")
        self.service = SkillsService(self.lib.data_dir)
        self.source = self.base / "source" / "sample"
        self.remote = self.base / "repo" / "skills" / "sample"
        self.old = self.write_skill(self.source, "Read a spreadsheet.")
        self.write_skill(self.remote, "Read a spreadsheet.")
        self.gh = {"kind": "github", "url": "https://github.com/example/skills",
                   "ref": "main", "subdir": "skills/sample", "revision": "a" * 40}

    @staticmethod
    def write_skill(path, description):
        path.mkdir(parents=True, exist_ok=True)
        file = path / "SKILL.md"
        file.write_text(f"---\nname: sample\ndescription: {description}\n---\n", encoding="utf-8")
        return file

    def call(self, op, **payload):
        return self.service.dispatch({"agent": "codex", "op": op, "payload": payload})

    def test_translation_requires_opt_in_and_reuses_persistent_cache(self):
        with patch("skills_translation._request", return_value=["读取电子表格。"]) as request:
            value = ["Read a spreadsheet.", "已经是中文。", "Read a spreadsheet."]
            self.assertEqual(self.call("translate_descriptions", texts=value)["translations"], {})
            self.call("skill_automation", translate=True)
            self.assertEqual(self.call("translate_descriptions", texts=value)["translations"],
                             {"Read a spreadsheet.": "读取电子表格。"})
            self.service = SkillsService(self.lib.data_dir)
            self.call("translate_descriptions", texts=value)
            request.assert_called_once_with("test-only:fx", ["Read a spreadsheet."])

    def test_deepl_free_uses_expected_host_and_language(self):
        from skills_translation import _request

        with patch("skills_translation.HTTPSConnection") as connection:
            response = connection.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = json.dumps(
                {"translations": [{"text": "译文"}]}, ensure_ascii=False).encode()
            self.assertEqual(_request("test-only:fx", ["Example"]), ["译文"])
            connection.assert_called_once_with("api-free.deepl.com", timeout=10)
            args = connection.return_value.request.call_args
            self.assertEqual(args.args[:2], ("POST", "/v2/translate"))
            self.assertEqual(json.loads(args.kwargs["body"])["target_lang"], "ZH-HANS")

    def test_links_imported_skill_from_lock_and_protects_external_installation(self):
        local = self.base / "codex" / "skills" / "sample"
        self.write_skill(local, "Read a spreadsheet.")
        self.lib.import_path(local)
        lock = self.base / "xdg" / "skills" / ".skill-lock.json"
        lock.parent.mkdir(parents=True)
        lock.write_text(json.dumps({"version": 3, "skills": {"sample": {
            "source": "example/skills", "sourceType": "github",
            "ref": "main", "skillPath": "skills/sample/SKILL.md", "commit": "a" * 40,
        }}}), encoding="utf-8")

        with patch.object(skills_sources, "_github_checkout", return_value=(self.base / "repo", "a" * 40)):
            self.assertEqual(run_updates(self.lib)["linked"], 0)
            self.call("skill_automation", github_auto=True)
            self.assertEqual(run_updates(self.lib)["linked"], 1)
        row = next(row for row in self.lib.state()["skills"] if row["managed"])
        self.assertEqual(row["source"]["url"], self.gh["url"])
        self.assertTrue(row["installed_externally"])
        self.write_skill(self.remote, "Read and write a spreadsheet.")
        with patch.object(skills_sources, "_github_checkout", return_value=(self.base / "repo", "b" * 40)):
            self.assertEqual(run_updates(self.lib)["updated"], 0)
        self.assertEqual((local / "SKILL.md").read_text(encoding="utf-8"), self.old.read_text(encoding="utf-8"))

    def test_git_origin_uses_checked_out_revision_and_checks_file_identity(self):
        git = self.source.parent / ".git"
        head = git / "refs" / "heads" / "release" / "current"
        head.parent.mkdir(parents=True)
        head.write_text("a" * 40 + "\n", encoding="utf-8")
        (git / "HEAD").write_text("ref: refs/heads/release/current\n", encoding="utf-8")
        (git / "config").write_text('[remote "origin"]\n  url = git@github.com:example/skills.git\n', encoding="utf-8")
        self.lib.import_path(self.source)
        hint = self.call("github_source", id="sample")["source"]
        self.assertEqual(hint, {**self.gh, "ref": "release/current", "subdir": "sample"})
        self.write_skill(self.base / "repo" / "sample", "Different files.")
        with patch.object(skills_sources, "_github_checkout", return_value=(self.base / "repo", "a" * 40)) as checkout:
            with self.assertRaisesRegex(ValueError, "local files differ"):
                self.call("github_link", id="sample")
        self.assertEqual(checkout.call_args.args[2], "a" * 40)
        self.assertEqual(self.lib.state()["skills"][0]["source"], str(self.source))

    def test_discovers_public_sources_without_url_and_only_links_on_confirmation(self):
        self.lib.import_path(self.source)
        mirror = self.base / "mirror"
        self.write_skill(mirror / "deep" / "exact-match", "Read a spreadsheet.")
        wrong = self.base / "wrong"
        self.write_skill(wrong / "sample", "Read a spreadsheet.")
        (wrong / "sample" / "different.txt").write_text("Other files.", encoding="utf-8")
        repos = {"example/skills": (self.base / "repo", "a" * 40), "example/mirror": (mirror, "b" * 40),
                 "example/sample": (wrong, "c" * 40)}

        def checkout(source, work, revision):
            return repos[source["url"].removeprefix("https://github.com/")]

        with (patch.object(skills_sources, "_market", return_value={"items": [
                  {"url": self.gh["url"], "skill_id": "sample", "name": "sample"},
                  {"url": "https://github.com/example/mirror", "skill_id": "sample", "name": "sample"}]}),
              patch.object(skills_sources, "_github_search_repos", return_value=[
                  self.gh["url"], "https://github.com/example/sample"]) as github_search,
              patch.object(skills_sources, "_github_checkout", side_effect=checkout) as fetch_repo):
            result = self.call("github_discover", id="sample")
            github_search.assert_called_once_with("sample", 6)
            self.assertEqual(fetch_repo.call_count, 3)
            self.assertEqual([candidate["subdir"] for candidate in result["matches"]],
                             ["skills/sample", "deep/exact-match"])
            self.assertEqual(self.lib.state()["skills"][0]["source"], str(self.source))
            with self.assertRaises(ValueError):
                self.call("github_link_discovered", id="sample", preview_id=result["preview_id"], candidate=-1)
            linked = self.call("github_link_discovered", id="sample", preview_id=result["preview_id"], candidate=1)
            self.assertEqual(linked["skill"]["source"], result["matches"][1])
            self.assertEqual(fetch_repo.call_count, 3)
            with self.assertRaises(ValueError):
                self.call("github_link_discovered", id="sample", preview_id=result["preview_id"], candidate=0)

    def test_source_discovery_uses_known_origin_before_public_search(self):
        self.lib.import_path(self.source, source={**self.gh, "digest":
                             skills_sources._digest(skills_sources._manifest(self.source))})
        with (patch.object(skills_sources, "_github_checkout", return_value=(self.base / "repo", "a" * 40)),
              patch("skills_upstream._search_repositories") as public_search):
            preview = self.call("github_discover", id="sample")
            self.assertEqual(len(preview["matches"]), 1)
            public_search.assert_not_called()
        row = self.lib.state()["skills"][0]
        self.write_skill(Path(row["path"]), "Different local content.")
        with self.assertRaises(ValueError):
            self.call("github_link_discovered", id="sample", preview_id=preview["preview_id"], candidate=0)

    def test_installed_discovery_uses_directory_and_declared_names_and_ignores_text_line_endings(self):
        root = self.base / "codex" / "skills"
        local = root / "nature-proposal-writer"
        upstream = self.base / "nature-repo" / "skills" / local.name
        for folder, eol in [(local, b"\r\n"), (upstream, b"\n")]:
            folder.mkdir(parents=True)
            (folder / "SKILL.md").write_bytes(eol.join([b"---", b"name: researchwrite", b"---", b"Public fixture", b""]))
            (folder / "README.md").write_bytes(eol.join([b"Proposal guidance", b""]))
        (root / "hatch-pet").mkdir()
        (root / "left-side-only").mkdir()
        self.write_skill(root / "left-side-only", "Installed only on one machine.")
        repo_url = "https://github.com/example/nature-skills"
        with (patch.object(skills_sources, "_market", return_value={"items": []}),
              patch.object(skills_sources, "_github_search_repos",
                           side_effect=lambda slug, limit: [repo_url] if slug == local.name else []) as search,
              patch.object(skills_sources, "_github_checkout", return_value=(self.base / "nature-repo", "a" * 40))):
            result = self.call("github_discover", id="external:nature-proposal-writer")
        self.assertEqual({call.args[0] for call in search.call_args_list}, {"researchwrite", "nature-proposal-writer"})
        self.assertEqual(result["status"], "matched")
        self.assertIsNone(result["preview_id"])
        self.assertEqual(result["matches"][0]["subdir"], "skills/nature-proposal-writer")
        self.assertEqual(result["matches"][0]["comparison"], "line_endings")
        state = self.lib.state()
        self.assertEqual(state["root"], str(root))
        self.assertIn("external:left-side-only", [row["id"] for row in state["skills"]])
        self.assertEqual(state["missing_skill_docs"], [str(root / "hatch-pet")])
        self.assertEqual((local / "SKILL.md").read_bytes().count(b"\r\n"), 4)

    def test_documented_subdir_and_different_fork_are_suggestions_only(self):
        self.lib.import_path(self.source)
        (self.source / "README.md").write_text(
            "Repository: https://github.com/example/skills/tree/main/skills/sample\n", encoding="utf-8")
        self.lib.import_path(self.source)
        self.write_skill(self.remote, "A different upstream version.")
        fork = self.base / "fork"
        self.write_skill(fork / "sample", "A same-name skill by another author.")
        def checkout(source, *_):
            return (fork, "b" * 40) if "/fork/" in source["url"] else (self.base / "repo", "a" * 40)
        with (patch.object(skills_sources, "_market", return_value={"items": []}),
              patch.object(skills_sources, "_github_search_repos", return_value=[
                  {"url": "https://github.com/fork/sample", "fork": True}]),
              patch.object(skills_sources, "_github_checkout", side_effect=checkout)):
            result = self.call("github_discover", id="sample")
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(result["matches"], [])
        self.assertIsNone(result["preview_id"])
        self.assertIn("documented_path", result["suggestions"][0]["evidence"])
        self.assertTrue(result["suggestions"][1]["fork"])
        self.assertNotIsInstance(self.lib.state()["skills"][0]["source"], dict)

    def test_discovery_separates_no_result_and_provider_failure(self):
        self.lib.import_path(self.source)
        with (patch.object(skills_sources, "_market", return_value={"items": []}),
              patch.object(skills_sources, "_github_search_repos", return_value=[]) as search):
            self.assertEqual(self.call("github_discover", id="sample")["status"], "not_found")
            search.side_effect = skills_sources.SourceError("github_rate_limited", "Rate limit reached")
            failure = self.call("github_discover", id="sample")
        self.assertEqual(failure["status"], "request_failed")
        self.assertIn("github_rate_limited", failure["errors"])

    def test_unattributed_fork_with_identical_files_stays_a_suggestion(self):
        self.lib.import_path(self.source)
        fork = {"url": "https://github.com/example/sample-fork", "fork": True}
        with (patch.object(skills_sources, "_market", return_value={"items": []}),
              patch.object(skills_sources, "_github_search_repos", return_value=[fork]),
              patch.object(skills_sources, "_github_checkout", return_value=(self.base / "repo", "a" * 40))):
            result = self.call("github_discover", id="sample")
        self.assertEqual(result["status"], "candidates")
        self.assertEqual(result["matches"], [])
        self.assertIsNone(result["preview_id"])
        self.assertTrue(result["suggestions"][0]["fork"])
        self.assertIn("same_files", result["suggestions"][0]["evidence"])

    def test_default_and_custom_home_keep_presence_separate(self):
        home = self.base / "windows-profile"
        default_root = home / ".codex" / "skills"
        custom_root = self.base / "codex" / "skills"
        names = [f"nature-fixture-{index:02d}" for index in range(24)]
        for root, line_ending, specific in [(default_root, b"\n", "default-only"),
                                            (custom_root, b"\r\n", "custom-only")]:
            for name in [*names, specific]:
                folder = root / name
                folder.mkdir(parents=True)
                (folder / "SKILL.md").write_bytes(line_ending.join(
                    [b"---", ("name: " + name).encode(), b"---", b"example", b""]))
        for name in names:
            normalized = []
            for root in (default_root, custom_root):
                comparison = {}
                skills_sources._manifest(root / name, normalized=comparison)
                normalized.append(comparison)
            self.assertEqual(normalized[0], normalized[1])
        custom = self.lib.state()
        self.assertEqual(custom["root"], str(custom_root))
        self.assertIn("external:custom-only", {row["id"] for row in custom["skills"]})
        with patch.object(Path, "home", return_value=home), patch.dict(os.environ):
            del os.environ["CODEX_HOME"]
            standard = SkillLibrary(self.base / "another-device", "codex").state()
        self.assertEqual(standard["root"], str(default_root))
        ids = {row["id"] for row in standard["skills"]}
        self.assertIn("external:default-only", ids)
        self.assertNotIn("external:custom-only", ids)

    def test_hermes_hub_hint_uses_install_path(self):
        lib = SkillLibrary(self.lib.data_dir, "hermes")
        root = self.base / "hermes" / "skills"
        self.write_skill(root / "category" / "sample", "Read a spreadsheet.")
        lock = root / ".hub" / "lock.json"
        lock.parent.mkdir()
        lock.write_text(json.dumps({"version": 1, "installed": {"sample": {
            "source": "github", "identifier": "example/skills/skills/sample",
            "install_path": "category/sample",
        }}}), encoding="utf-8")
        from skills_upstream import skill_source

        hint = skill_source(lib, {"id": "external:category/sample"})["source"]
        self.assertEqual(hint, {"kind": "github", "url": self.gh["url"], "subdir": self.gh["subdir"], "ref": ""})

    def test_automatic_update_preserves_deployment_edits_and_respects_rollback(self):
        self.lib.import_path(self.source, source={**self.gh, "digest":
                             skills_sources._digest(skills_sources._manifest(self.source))})
        self.assertTrue(self.lib.action("install", {"id": "sample"})["ok"])
        self.call("skill_automation", github_auto=True)
        deployed = self.base / "codex" / "skills" / "sample" / "SKILL.md"
        self.write_skill(self.remote, "Read and write a spreadsheet.")
        temp_root = self.base / "real-temp"
        temp_root.mkdir()
        temp_alias = temp_root
        if os.name != "nt":
            temp_alias = self.base / "temp-alias"
            link_directory(temp_root, temp_alias)
        with (patch.object(skills_sources, "_github_head", return_value="b" * 40),
              patch("skills_upstream.TemporaryDirectory", return_value=nullcontext(str(temp_alias))),
              patch.object(skills_sources, "_github_checkout", return_value=(self.base / "repo", "b" * 40))):
            self.write_skill(deployed.parent, "My edits.")
            self.assertEqual(run_updates(self.lib)["skipped"], 1)
            self.assertIn("My edits.", deployed.read_text(encoding="utf-8"))
            self.write_skill(deployed.parent, "Read a spreadsheet.")
            self.assertEqual(run_updates(self.lib)["updated"], 1)
            self.assertIn("Read and write", deployed.read_text(encoding="utf-8"))
            self.assertTrue(self.lib.action("rollback", {"id": "sample", "version": 1})["ok"])
            self.assertEqual(run_updates(self.lib)["updated"], 0)
            self.assertNotIn("Read and write", deployed.read_text(encoding="utf-8"))

    def test_checks_only_on_first_launch_per_local_date_and_preserves_status(self):
        self.call("skill_automation", github_auto=True)
        today = datetime(2026, 11, 22, 8, tzinfo=timezone.utc)
        tomorrow = datetime(2026, 11, 23, 8, tzinfo=timezone.utc)
        summary = {"linked": 0, "updated": 1, "skipped": 0, "failed": 0}
        with patch("skills_upstream.run_updates", return_value=summary) as run:
            first_launch = UpstreamWorker(self.lib.data_dir, clock=lambda: today)
            first_launch.check("codex")
            reopen = UpstreamWorker(self.lib.data_dir, clock=lambda: today)
            reopen.check("codex")
            reopen.check("hermes")
            run.assert_called_once()
            self.assertEqual(reopen.status("codex")["updated"], 1)
            next_launch = UpstreamWorker(self.lib.data_dir, clock=lambda: tomorrow)
            next_launch.check("codex")
            self.assertEqual(run.call_count, 2)
            self.assertEqual(next_launch.status("codex")["day"], "2026-11-23")


if __name__ == "__main__":
    unittest.main()

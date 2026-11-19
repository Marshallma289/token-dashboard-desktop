import io
import json
import os
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import skills_sources as sources
from skills_core import SkillLibrary


def doc(name, text="original"):
    return f"---\nname: {name}\ndescription: Example skill\n---\n\n{text}\n"


def archive_bytes(entries, compression=zipfile.ZIP_STORED):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        for name, value in entries:
            archive.writestr(name, value)
    return output.getvalue()


class SkillSourcesTest(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory(prefix="test-skill-source-"))
        self.base = Path(temp).resolve()
        self.enterContext(patch.dict(os.environ, {"CODEX_HOME": str(self.base / "codex"), "HERMES_HOME": str(self.base / "hermes")}))
        self.lib = SkillLibrary(self.base / "data", "codex")

    def call(self, op, **payload):
        result = sources.handle(self.lib, op, payload)
        json.dumps(result, allow_nan=False)
        return result

    def ok(self, op, **payload):
        result = self.call(op, **payload)
        self.assertTrue(result["ok"], result)
        return result

    def skill(self, name, parent="input"):
        root = self.base / parent / name
        root.mkdir(parents=True)
        (root / "SKILL.md").write_text(doc(name), encoding="utf-8")
        return root

    def imported(self, path):
        preview = self.ok("local_preview", path=str(path))
        out = self.ok("import_selected", preview_id=preview["preview_id"], ids=[preview["candidates"][0]["id"]])
        self.assertEqual(out["errors"], [])
        return out["imported"][0]["skill"]["id"]

    def test_local_discovery_requires_scoped_explicit_selection(self):
        a = self.skill("first")
        self.skill("second")
        marker = self.base / "executed"
        (a / "run.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n", encoding="utf-8")
        preview = self.ok("local_preview", path=str(a.parent))
        self.assertEqual([c["name"] for c in preview["candidates"]], ["first", "second"])
        self.assertEqual(self.lib.state()["skills"], [])
        token = preview["preview_id"]
        first = preview["candidates"][0]["id"]
        other = SkillLibrary(self.lib.data_dir, "hermes")
        self.assertEqual(sources.handle(other, "import_selected", {"preview_id": token, "ids": [first]})["code"], "stale_preview")
        self.assertEqual(self.call("import_selected", preview_id=token, ids=["not-a-candidate"])["code"], "invalid_selection")
        preview["candidates"][0]["source"]["subdir"] = "../should-not-affect-ticket"
        self.lib = SkillLibrary(self.lib.data_dir, "codex")
        result = self.ok("import_selected", preview_id=token, ids=[first])
        self.assertEqual(len(result["imported"]), 1)
        self.assertEqual([row["id"] for row in self.lib.state()["skills"]], ["first"])
        self.assertTrue((self.lib.skill_path("first") / "run.py").is_file())
        self.assertFalse(marker.exists())
        self.assertEqual(self.call("import_selected", preview_id=token, ids=[first])["code"], "stale_preview")

    def test_changed_selected_content_rejects_whole_batch_before_import(self):
        first, second = self.skill("first"), self.skill("second")
        preview = self.ok("local_preview", path=str(first.parent))
        (second / "SKILL.md").write_text(doc("second", "changed"), encoding="utf-8")
        config = {"preview_id": preview["preview_id"], "ids": [c["id"] for c in preview["candidates"]]}
        self.assertEqual(self.call("import_selected", **config)["code"], "source_changed")
        self.assertEqual(self.lib.state()["skills"], [])

    def test_archive_export_roundtrip_without_overwrite(self):
        package = self.base / "input.skill"
        package.write_bytes(archive_bytes([("demo/SKILL.md", doc("demo")), ("demo/resources/a.bin", b"\x00\x01")]))
        preview = self.ok("archive_preview", path=str(package))
        result = self.ok("import_selected", preview_id=preview["preview_id"], ids=[preview["candidates"][0]["id"]])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["imported"][0]["skill"]["source"]["kind"], "archive")
        output = self.base / "export.zip"
        self.ok("export", id="demo", path=str(output))
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(archive.read("demo/resources/a.bin"), b"\x00\x01")
        self.assertEqual(self.call("export", id="demo", path=str(output))["code"], "already_exists")
        self.assertEqual(self.ok("archive_preview", path=str(output))["candidates"][0]["name"], "demo")

    def test_unsafe_archives_are_rejected(self):
        bad_paths = ["../escape", "/absolute", "C:/escape", "a\\..\\escape", "demo/x:stream", "demo/CON.txt", "demo/.git/config"]
        package = self.base / "bad.zip"
        for name in bad_paths:
            with self.subTest(name=name):
                package.write_bytes(archive_bytes([(name, "untrusted")]))
                self.assertEqual(self.call("archive_preview", path=str(package))["code"], "unsafe_path")
        package.write_bytes(archive_bytes([("Skill/SKILL.md", doc("test")), ("skill/a", "x")]))
        self.assertEqual(self.call("archive_preview", path=str(package))["code"], "unsafe_archive")
        link = zipfile.ZipInfo("skill/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        package.write_bytes(archive_bytes([(link, "../escape")]))
        self.assertEqual(self.call("archive_preview", path=str(package))["code"], "unsafe_archive")
        package.write_bytes(archive_bytes([("skill/SKILL.md", b"z" * sources.MiB)], zipfile.ZIP_DEFLATED))
        self.assertEqual(self.call("archive_preview", path=str(package))["code"], "limit")
        package.write_bytes(archive_bytes([("skill/SKILL.md", doc("skill")), ("skill/resource", "x")]))
        with patch.object(sources, "MAX_ENTRIES", 1), patch.object(sources.zipfile, "ZipFile") as constructor:
            self.assertEqual(self.call("archive_preview", path=str(package))["code"], "limit")
            constructor.assert_not_called()
        package.write_bytes(b"bad zip")
        self.assertEqual(self.call("archive_preview", path=str(package))["code"], "unsafe_archive")

    def test_updates_preview_confirmation_replay_and_export(self):
        directory = self.skill("demo")
        (directory / "remove.txt").write_text("remove me", encoding="utf-8")
        sid = self.imported(directory / "SKILL.md")
        self.assertEqual(self.ok("check_updates")["updates"][0]["status"], "unchanged")
        (directory / "remove.txt").unlink()
        (directory / "bytes.bin").write_bytes(b"\xff\x00")
        (directory / "SKILL.md").write_text(doc("demo", "revised"), encoding="utf-8")
        self.assertEqual(self.ok("check_updates", ids=[sid])["updates"][0]["status"], "available")
        preview = self.ok("update_preview", id=sid)
        changes = {row["path"]: row for row in preview["changes"]}
        self.assertIn("+revised", changes["SKILL.md"]["diff"])
        self.assertEqual(changes["remove.txt"]["status"], "deleted")
        self.assertTrue(changes["bytes.bin"]["binary"])
        payload = {"id": sid, "update_token": preview["update_token"]}
        self.assertEqual(self.call("apply_update", **payload, confirm="true")["code"], "confirmation_required")
        self.assertTrue((self.lib.skill_path(sid) / "remove.txt").exists())
        applied = self.ok("apply_update", **payload, confirm=True)
        self.assertEqual(applied["skill"]["id"], sid)
        self.assertFalse((self.lib.skill_path(sid) / "remove.txt").exists())
        self.assertEqual((self.lib.skill_path(sid) / "bytes.bin").read_bytes(), b"\xff\x00")
        self.assertEqual(self.call("apply_update", **payload, confirm=True)["code"], "stale_preview")
        self.assertIsNone(self.ok("update_preview", id=sid)["update_token"])

    def test_update_refuses_stale_source_and_changes_to_installed_skill(self):
        directory = self.skill("demo")
        sid = self.imported(directory)
        local = self.lib.skill_path(sid) / "SKILL.md"
        source = directory / "SKILL.md"
        source.write_text(doc("demo", "first update"), encoding="utf-8")
        preview = self.ok("update_preview", id=sid)
        source.write_text(doc("demo", "second update"), encoding="utf-8")
        self.assertEqual(self.call("apply_update", id=sid, update_token=preview["update_token"], confirm=True)["code"], "source_changed")
        preview = self.ok("update_preview", id=sid)
        local.write_text(doc("demo", "my changes"), encoding="utf-8")
        self.assertEqual(self.call("apply_update", id=sid, update_token=preview["update_token"], confirm=True)["code"], "stale_preview")
        self.assertTrue(self.ok("update_preview", id=sid)["local_modified"])
        self.assertIn("my changes", local.read_text(encoding="utf-8"))

    def test_update_protects_modified_deployed_copy(self):
        directory = self.skill("demo")
        sid = self.imported(directory)
        self.assertTrue(self.lib.action("install", {"id": sid, "mode": "copy"})["ok"])
        deployed = self.base / "codex" / "skills" / sid / "SKILL.md"
        deployed.write_text(doc("demo", "deployed edits"), encoding="utf-8")
        (directory / "SKILL.md").write_text(doc("demo", "upstream edits"), encoding="utf-8")
        preview = self.ok("update_preview", id=sid)
        self.assertTrue(preview["local_modified"])
        self.assertFalse(self.call("apply_update", id=sid, update_token=preview["update_token"], confirm=True)["ok"])
        self.assertIn("deployed edits", deployed.read_text(encoding="utf-8"))
        self.assertIn("original", (self.lib.skill_path(sid) / "SKILL.md").read_text(encoding="utf-8"))

    def test_github_subdir_market_filter_and_pinned_commit(self):
        first, second = "a" * 40, "b" * 40
        head = [first]
        visits = []

        def fetch(url, limit, output=None):
            visits.append(url)
            if "/commits?" in url:
                return json.dumps([{"sha": head[0]}]).encode()
            data = archive_bytes([("repo-root/skills/alpha/SKILL.md", doc("alpha", url[-40:])),
                                  ("repo-root/skills/beta/SKILL.md", doc("beta"))])
            output.write(data)

        with patch.object(sources, "_fetch", side_effect=fetch):
            preview = self.ok("github_preview", url="https://github.com/Org/Repo/tree/feature%2Fx/skills", skill_id="alpha")
            self.assertIn("sha=feature%2Fx", visits[0])
            self.assertEqual([c["path"] for c in preview["candidates"]], ["skills/alpha"])
            head[0] = second
            imported = self.ok("import_selected", preview_id=preview["preview_id"], ids=[preview["candidates"][0]["id"]])
            self.assertEqual(imported["errors"], [])
            self.assertTrue(visits[-1].endswith(first))
            self.assertEqual(self.ok("check_updates", ids=["alpha"])["updates"][0]["revision"], second)
            update = self.ok("update_preview", id="alpha")
            head[0] = first
            self.ok("apply_update", id="alpha", update_token=update["update_token"], confirm=True)
            self.assertTrue(visits[-1].endswith(second))

    def test_market_uses_reference_search_and_flight_boards(self):
        row = {"source": "Example/Repo", "skillId": "demo", "name": "Demo", "installs": 13}
        flight = "<script>self.__next_f.push(" + json.dumps([1, "1:" + json.dumps([row])]) + ")</script>"
        with patch.object(sources, "_fetch", side_effect=[json.dumps({"skills": [row, row]}).encode(), flight.encode()]) as fetch:
            result = self.ok("market", query="x & y", limit=2)
            self.assertEqual(result["items"][0]["url"], "https://github.com/example/repo")
            self.assertEqual(len(result["items"]), 1)
            self.assertIn("q=x+%26+y&limit=2", fetch.call_args.args[0])
            self.assertEqual(self.ok("market")["items"][0]["installs"], 13)
            self.assertEqual(fetch.call_args.args[0], "https://skills.sh/hot")

    def test_network_redirects_are_checked_and_tls_is_verified(self):
        class Response(io.BytesIO):
            def __init__(self, status, data=b"", **headers):
                super().__init__(data)
                self.status, self.headers = status, headers

            def getcode(self):
                return self.status

        for location in ("http://skills.sh/hot", "https://127.0.0.1/", "https://skills.sh/api/private", "//api.github.com/repos/x/y/commits"):
            with self.subTest(location=location), patch.object(sources, "build_opener") as build:
                build.return_value.open.return_value = Response(302, Location=location)
                with self.assertRaises(sources.SourceError) as error:
                    sources._fetch("https://skills.sh/hot", 128)
                self.assertEqual(error.exception.code, "network_target")
                self.assertEqual(build.return_value.open.call_count, 1)
        with patch.object(sources, "build_opener") as build:
            build.return_value.open.side_effect = [Response(302, Location="https://www.skills.sh/hot"), Response(200, b"ok")]
            self.assertEqual(sources._fetch("https://skills.sh/hot", 2), b"ok")
            proxy, tls, redirects = build.call_args.args
            self.assertIsInstance(proxy, sources.ProxyHandler)
            self.assertTrue(tls._context.check_hostname)
            self.assertEqual(tls._context.verify_mode, sources.ssl.CERT_REQUIRED)
            self.assertIsNone(redirects.redirect_request())
        for url in ("https://github.com.evil.test/o/r", "https://u@github.com/o/r", "https://github.com:443/o/r", "http://github.com/o/r"):
            self.assertEqual(self.call("github_preview", url=url)["code"], "invalid_url")


if __name__ == "__main__":
    unittest.main()

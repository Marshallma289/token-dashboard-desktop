from pathlib import Path
import tempfile
import unittest

from publish_update import release_notes


class ReleaseNotesTests(unittest.TestCase):
    def test_extracts_only_the_released_version(self):
        with tempfile.TemporaryDirectory() as directory:
            readme = Path(directory) / 'README.md'
            readme.write_text('# Title\n\n## 1.8.50 other\n- Not this.\n\n## 1.8.5：修复\n\n- 费用展示\n\n### 验证\n- 双平台\n\n## 1.8.4 旧版\n- 不应出现在发布说明\n', encoding='utf-8')
            notes = release_notes('1.8.5', readme)
            self.assertIn('费用展示', notes)
            self.assertIn('双平台', notes)
            self.assertNotIn('Not this.', notes)
            self.assertNotIn('不应出现在发布说明', notes)

    def test_unlisted_version_falls_back_to_general_notes(self):
        with tempfile.TemporaryDirectory() as directory:
            readme = Path(directory) / 'README.md'
            readme.write_text('# Title\n\n## 1.8.4 旧版\n- Only old notes.\n', encoding='utf-8')
            self.assertNotIn('Only old notes', release_notes('1.8.5', readme))
            self.assertIn('Windows', release_notes('1.8.5', readme))


class GuiSmokeGateTests(unittest.TestCase):
    def test_publication_requires_an_actual_usable_frozen_window(self):
        from publish_update import gui_smoke_verified

        good = {'status': 'passed', 'frozen': True, 'gui_tested': True,
                'renderer': 'edgechromium', 'app_version': '1.8.5', 'rendered_total_tokens': 173}
        artifact = {'platform': 'win32', 'version': '1.8.5'}
        self.assertTrue(gui_smoke_verified({**artifact, 'gui_smoke_test': good}))
        self.assertFalse(gui_smoke_verified(artifact))
        for field, bad in (('status', 'failed'), ('gui_tested', False), ('frozen', False),
                           ('renderer', 'cocoa'), ('app_version', '1.8.4'), ('rendered_total_tokens', 174)):
            with self.subTest(field=field):
                self.assertFalse(gui_smoke_verified({**artifact, 'gui_smoke_test': {**good, field: bad}}))

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import skills_core
from skills_core import SkillError, SkillLibrary


class SkillLibraryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        env = patch.dict(os.environ, {'CODEX_HOME': str(self.base / 'codex'),
                                     'HERMES_HOME': str(self.base / 'hermes')})
        env.start()
        self.addCleanup(env.stop)
        self.data = self.base / 'state'
        self.lib = SkillLibrary(self.data, 'codex')
        self.hermes = SkillLibrary(self.data, 'hermes')
        self.root = self.base / 'codex' / 'skills'

    @staticmethod
    def skill(path, text='v1'):
        path.mkdir(parents=True, exist_ok=True)
        (path / 'SKILL.md').write_text(f'---\nname: {path.name}\ndescription: >-\n  first line\n  second line\n---\n{text}',
                                       encoding='utf-8')
        return path

    def import_skill(self, name='alpha'):
        return self.lib.import_path(self.skill(self.base / 'input' / name))['id']

    def run_action(self, op, **payload):
        result = self.lib.action(op, payload)
        self.assertTrue(result['ok'], result)
        json.dumps(result)
        return result

    def assert_code(self, code, result):
        self.assertFalse(result['ok'], result)
        self.assertEqual(code, result['code'], result)

    def test_isolated_defaults_and_persisted_custom_root(self):
        self.assertEqual(Path(self.lib.state()['root']), self.root)
        self.assertFalse(self.root.exists())
        skill_id = self.import_skill()
        self.assertEqual(self.hermes.state()['skills'], [])
        self.assert_code('not_found', self.hermes.action('install', {'id': skill_id}))
        self.assert_code('unsafe_root', self.hermes.action('root', {'path': str(self.root)}))
        custom = self.base / 'custom'
        self.run_action('root', path=str(custom))
        self.run_action('install', id=skill_id)
        self.assertTrue((custom / skill_id / 'SKILL.md').exists())
        self.assertEqual(SkillLibrary(self.data, 'codex').state()['root'], str(custom))
        self.assert_code('deployments_active', self.lib.action('root', {'path': None}))
        self.assert_code('unsafe_root', self.hermes.action('root', {'path': str(custom / 'nested')}))
        self.run_action('disable', id=skill_id)
        self.assertEqual(self.run_action('root', path=None)['root'], str(self.root))
        self.assertFalse(self.root.exists())

    def test_recursive_hermes_external_details_are_read_only(self):
        root = Path(self.hermes.state()['root'])
        external = self.skill(root / 'research' / 'food' / 'alpha')
        (external / 'README.md').write_text('read me', encoding='utf-8')
        rows = self.hermes.state()['skills']
        self.assertEqual([row['id'] for row in rows], ['external:research/food/alpha'])
        row = rows[0]
        self.assertTrue(row['installed'])
        self.assertTrue(row['external'])
        self.assertEqual(row['description'], 'first line second line')
        detail = self.hermes.action('detail', {'id': row['id']})['skill']
        self.assertIn('v1', detail['skill_md'])
        self.assertEqual(detail['readme_md'], 'read me')
        self.assertEqual(self.hermes.skill_path(row['id']), external)
        for op in ('disable', 'delete', 'install', 'tags'):
            self.assert_code('external_skill', self.hermes.action(op, {'id': row['id']}))
        self.assertTrue((external / 'SKILL.md').is_file())
        with self.assertRaises(SkillError):
            self.lib.import_path(external)

    def test_preset_enable_disable_preserve_non_member_and_delete_keeps_deployment(self):
        member, outsider = self.import_skill('member'), self.import_skill('outsider')
        self.run_action('install', id=outsider)
        self.run_action('preset_save', name='study', ids=[member])
        outside = self.root / outsider / 'SKILL.md'
        before = outside.stat().st_ino, outside.read_bytes()
        self.run_action('preset_apply', name='study', enabled=True)
        self.assertTrue((self.root / member).is_dir())
        self.assertEqual((outside.stat().st_ino, outside.read_bytes()), before)
        self.run_action('preset_apply', name='study', enabled=False)
        self.assertFalse((self.root / member).exists())
        self.assertEqual((outside.stat().st_ino, outside.read_bytes()), before)
        self.run_action('preset_apply', name='study', enabled=True)
        self.assertEqual(self.run_action('preset_delete', name='study')['state']['presets'], [])
        self.assertTrue((self.root / member).is_dir())
        self.assertEqual((outside.stat().st_ino, outside.read_bytes()), before)

    def test_preset_delete_dispatch_reaches_core(self):
        from skills_service import SkillsService

        member = self.import_skill()
        self.run_action('install', id=member)
        self.run_action('preset_save', name='active', ids=[member])
        result = SkillsService(self.data).dispatch({
            'agent': 'codex', 'op': 'preset_delete', 'payload': {'name': 'active'},
        })
        self.assertTrue(result['ok'])
        self.assertEqual(result['state']['presets'], [])
        self.assertTrue((self.root / member / 'SKILL.md').is_file())

    def test_owned_copy_edits_stop_disable_delete_and_update(self):
        skill_id = self.import_skill()
        self.run_action('install', id=skill_id)
        deployed = self.root / skill_id / 'SKILL.md'
        deployed.write_text('important edits', encoding='utf-8')
        update = self.skill(self.base / 'update' / 'different-name', 'v2')
        for op, extra in (('install', {}), ('disable', {}), ('delete', {}), ('update', {'path': str(update)})):
            self.assert_code('modified', self.lib.action(op, {'id': skill_id, **extra}))
        record = self.lib.state()['skills'][0]
        self.assertTrue(record['modified'])
        self.assertEqual(deployed.read_text(encoding='utf-8'), 'important edits')
        self.assertFalse((self.lib.skill_path(skill_id) / 'SKILL.md').read_text(encoding='utf-8') == 'important edits')

    def test_explicit_update_callback_tags_and_rollback(self):
        skill_id = self.import_skill()
        self.run_action('tags', id=skill_id, tags=['research', 'research'])
        self.run_action('install', id=skill_id)
        update = self.skill(self.base / 'upstream' / 'different-name', 'v2')
        source = {'kind': 'git', 'ref': 'main', 'revision': 'revision-v2'}

        def reject(path, record):
            self.assertTrue((path / 'SKILL.md').is_file())
            self.assertEqual(record['version'], 1)
            raise RuntimeError('old preview')

        with self.assertRaisesRegex(RuntimeError, 'old preview'):
            self.lib.replace_skill(skill_id, update, source, validate_current=reject)
        updated = self.lib.replace_skill(skill_id, update, source)
        self.assertEqual((updated['id'], updated['version'], updated['tags'], updated['source']),
                         (skill_id, 2, ['research'], source))
        self.assertIn('v2', (self.root / skill_id / 'SKILL.md').read_text(encoding='utf-8'))
        self.assert_code('library_changed', self.lib.action('update', {'id': skill_id, 'path': str(update), 'version': 1}))
        self.run_action('rollback', id=skill_id, version='1')
        self.assertIn('v1', (self.root / skill_id / 'SKILL.md').read_text(encoding='utf-8'))
        history = self.run_action('history', id=skill_id)['history']
        self.assertEqual([r['version'] for r in history], [3, 2, 1])
        self.assertTrue(history[0]['current'])
        self.run_action('delete', id=skill_id)
        self.assertEqual(self.lib.state()['skills'], [])
        self.run_action('rollback', id=skill_id, version=2)
        self.assertFalse((self.root / skill_id).exists())
        self.assertIn('v2', (self.lib.skill_path(skill_id) / 'SKILL.md').read_text(encoding='utf-8'))

    def test_batch_failure_rolls_back_files_and_metadata(self):
        first, second = self.import_skill('first'), self.import_skill('second')
        collision = self.root / second
        collision.mkdir(parents=True)
        self.assert_code('target_exists', self.lib.action('install', {'ids': [first, second]}))
        self.assertFalse((self.root / first).exists())
        self.assertTrue(collision.is_dir())
        self.assertFalse(self.lib.state()['skills'][0]['installed'])
        self.assertEqual(list(self.root.glob('.skill-library-*')), [])
        self.run_action('install', id=first)

    def test_replace_failure_restores_previous_copy_and_history(self):
        skill_id = self.import_skill()
        self.run_action('install', id=skill_id)
        live = self.lib.skill_path(skill_id)
        real_rename = skills_core.rename_new

        def fail_rename(source, target):
            if target == live and source.parent.name.startswith('.skill-library-'):
                if 'v2' in (source / 'SKILL.md').read_text(encoding='utf-8'):
                    raise OSError('injected final move failure')
            real_rename(source, target)

        update = self.skill(self.base / 'update' / skill_id, 'v2')
        with patch.object(skills_core, 'rename_new', side_effect=fail_rename):
            self.assert_code('io_error', self.lib.action('update', {'id': skill_id, 'path': str(update)}))
        self.assertIn('v1', (live / 'SKILL.md').read_text(encoding='utf-8'))
        self.assertIn('v1', (self.root / skill_id / 'SKILL.md').read_text(encoding='utf-8'))
        self.assertEqual(len(self.run_action('history', id=skill_id)['history']), 1)

    def test_directory_links_are_unlinked_without_touching_destination(self):
        skill_id = self.import_skill()
        result = self.lib.action('install', {'id': skill_id, 'mode': 'link'})
        if result.get('code') == 'link_unavailable':
            self.assertFalse((self.root / skill_id).exists())
            self.skipTest('Filesystem does not permit directory symlinks')
        self.assertTrue(result['ok'], result)
        target = self.root / skill_id
        self.assertTrue(skills_core.linked(target.lstat()))
        self.assertEqual(target.resolve(), self.lib.skill_path(skill_id))
        self.run_action('disable', id=skill_id)
        self.assertIsNone(skills_core.info(target))
        self.assertTrue((self.lib.skill_path(skill_id) / 'SKILL.md').is_file())

    def test_unsafe_roots_and_directory_links_are_rejected(self):
        for value in (self.root.anchor, str(self.root / '..' / 'escapes'), str(self.data / 'skills-library')):
            self.assertFalse(self.lib.action('root', {'path': value})['ok'])
        for value in ('../alpha', 'CON', 'foo:bar', 'alpha.'):
            self.assert_code('unsafe_path', self.lib.action('delete', {'id': value}))
        outside = self.skill(self.base / 'outside')
        candidate = self.skill(self.base / 'input' / 'alpha')
        try:
            skills_core.link_directory(outside, candidate / 'link')
        except OSError:
            self.skipTest('Filesystem does not permit directory symlinks')
        with self.assertRaisesRegex(SkillError, 'Links, mounts'):
            self.lib.import_path(candidate)
        self.assert_code('unsafe_path', self.lib.action('root', {'path': str(candidate / 'link' / 'new')}))
        self.assertTrue((outside / 'SKILL.md').is_file())

    def test_snapshot_portable_cas_and_existing_deployment_safeguard(self):
        external = self.skill(self.root / 'external-skill')
        old = self.lib.export_portable()
        self.assertEqual(json.loads(old['manifest.json']), {'schema': 1, 'skills': [], 'presets': []})
        alpha = self.skill(self.base / 'private-input' / 'alpha')
        (alpha / 'empty').mkdir()
        skill_id = self.lib.import_path(alpha, {'path': str(alpha), 'token': 'secret-only-local'})['id']
        self.run_action('tags', id=skill_id, tags=['sample'])
        self.run_action('preset_save', name='test', ids=[skill_id])
        snapshot = self.lib.export_portable()
        self.assertNotIn(b'secret-only-local', snapshot['manifest.json'])
        self.assertNotIn(b'private-input', snapshot['manifest.json'])
        self.assertNotIn(b'external-skill', snapshot['manifest.json'])
        self.assert_code('library_changed', self.lib.import_portable(old, expected_current=old))
        self.run_action('install', id=skill_id)
        self.assert_code('deployments_active', self.lib.import_portable(old, expected_current=snapshot))
        different = SkillLibrary(self.base / 'other-data', 'codex')
        other_root = self.base / 'other-root'
        self.assertTrue(different.action('root', {'path': str(other_root)})['ok'])
        self.assertNotEqual(self.lib.library_id, different.library_id)
        self.assertNotEqual(self.lib.library_id, self.hermes.library_id)
        self.assertEqual(self.lib.library_id, SkillLibrary(self.data, 'codex').library_id)
        self.assertTrue(different.import_portable(snapshot)['ok'])
        self.assertEqual(snapshot, different.export_portable())
        self.assertEqual(different.state()['root'], str(other_root))
        self.assertFalse(other_root.exists())
        self.assertTrue((different.skill_path(skill_id) / 'empty').is_dir())
        self.assertFalse(different.state()['skills'][0]['installed'])
        self.run_action('disable', id=skill_id)
        self.assertTrue(self.lib.import_portable(old, expected_current=snapshot)['ok'])
        self.assertTrue((external / 'SKILL.md').is_file())
        self.assertEqual(self.lib.export_portable(), old)

    def test_portable_rejects_path_traversal_before_replacing_existing(self):
        self.import_skill()
        original = self.lib.export_portable()
        changed = {**original, 'skills/alpha/../../escaped': b'bad'}
        self.assert_code('unsafe_path', self.lib.import_portable(changed))
        self.assertEqual(self.lib.export_portable(), original)


if __name__ == '__main__':
    unittest.main()

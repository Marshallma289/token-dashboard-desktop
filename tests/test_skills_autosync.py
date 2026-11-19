from collections import deque
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import tempfile
from threading import Event, Thread
import unittest
from unittest.mock import Mock, patch

import skills_autosync as auto
from skills_backup import handle
from skills_core import SkillLibrary


class FakeClock:
    value = 0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class FakeBackup:
    def __init__(self):
        self.libraries = {'codex': ['connected-codex', 'a', False], 'hermes': [None, None, False]}
        self.observations, self.transfers = [], []
        self.results = deque()
        self.started, self.release = Event(), Event()
        self.block = False

    def observe(self, agent):
        self.observations.append(agent)
        return auto.Observation(*self.libraries[agent])

    def transfer(self, agent, op, observed, cancelled):
        self.transfers.append((agent, op, observed.digest))
        self.started.set()
        if self.block:
            if not self.release.wait(5):
                raise OSError('test did not release transfer')
            if cancelled.is_set():
                return {'ok': False, 'code': 'cancelled'}
        if self.results:
            return self.results.popleft()
        self.libraries[agent][2] = False
        return {'ok': True}


class AutoSyncTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name).resolve()
        self.clock, self.adapter = FakeClock(), FakeBackup()
        self.worker = self.make()

    def make(self, **kwargs):
        manager = auto.AutoSyncManager(self.path, adapter=self.adapter, clock=self.clock, pull_interval=300,
                                       **kwargs)
        self.addCleanup(manager.stop, wait=2)
        return manager

    def action(self, op, agent='codex', **payload):
        return self.worker.action(agent, op, payload)

    def enable(self, agent='codex'):
        result = self.action('autosync_set', agent, enabled=True, confirm=True)
        self.assertTrue(result['ok'], result)
        return result['status']

    def advance(self, seconds):
        self.clock.advance(seconds)
        self.worker.tick()

    def test_default_off_does_not_read_library_or_network_and_requires_per_agent_consent(self):
        self.advance(900)
        self.assertEqual(self.action('autosync_status')['status']['state'], 'disabled')
        self.assertEqual((self.adapter.observations, self.adapter.transfers), ([], []))
        self.assertEqual(self.action('autosync_set', enabled=True)['code'], 'confirmation_required')
        self.assertEqual(self.action('autosync_set', 'hermes', enabled=True, confirm=True)['code'], 'sync_not_connected')
        self.enable()
        self.assertFalse(self.action('autosync_status', 'hermes')['status']['enabled'])
        self.assertTrue(self.make().status('codex')['enabled'])
        files = list((self.path / 'skills-autosync').glob('*.json'))
        self.assertEqual([path.name for path in files], ['codex.json'])

    def test_debounce_is_trailing_and_idle_pulls_are_periodic(self):
        self.enable()
        self.adapter.libraries['codex'][1:] = ['b', True]
        self.advance(20)
        self.assertEqual(self.worker.status('codex')['next_in_seconds'], 120)
        self.advance(115)
        self.assertEqual(self.adapter.transfers, [])
        self.adapter.libraries['codex'][1:] = ['c', True]
        self.advance(1)
        self.advance(119)
        self.assertEqual(self.adapter.transfers, [])
        self.advance(1)
        self.assertEqual(self.adapter.transfers, [('codex', 'sync_push', 'c')])
        self.advance(299)
        self.assertEqual(len(self.adapter.transfers), 1)
        self.advance(1)
        self.assertEqual(self.adapter.transfers[-1], ('codex', 'sync_pull', 'c'))
        self.assertIsNotNone(self.worker.status('codex')['last_success_at'])

    def test_conflict_pauses_persists_and_requires_explicit_resume(self):
        self.adapter.libraries['codex'][2] = True
        self.adapter.results.append({'ok': False, 'code': 'sync_conflict', 'conflict': {'skill_ids': ['one']}})
        self.enable()
        self.advance(120)
        status = self.worker.status('codex')
        self.assertEqual(status['state'], 'paused_conflict')
        self.assertEqual(status['conflict'], {'skill_ids': ['one']})
        self.assertEqual(self.enable()['state'], 'paused_conflict')
        self.worker = self.make()
        self.advance(10000)
        self.assertEqual(self.worker.status('codex')['pause_reason'], 'sync_conflict')
        self.assertEqual(len(self.adapter.transfers), 1)
        self.assertEqual(self.action('autosync_resume')['code'], 'confirmation_required')
        self.adapter.libraries['codex'][2] = False
        self.assertTrue(self.action('autosync_resume', confirm=True)['ok'])
        self.advance(0)
        self.assertEqual([op for _, op, _ in self.adapter.transfers], ['sync_push', 'sync_pull'])

    def test_repository_switch_revokes_previous_authorization_before_network(self):
        self.enable()
        self.adapter.libraries['codex'][0] = 'different-connection'
        self.advance(900)
        self.assertEqual(self.adapter.transfers, [])
        self.assertEqual(self.worker.status('codex')['last_error'], 'connection_changed')
        self.assertFalse(self.make().status('codex')['enabled'])
        self.enable()
        self.advance(300)
        self.assertEqual(len(self.adapter.transfers), 1)

    def test_backoff_and_remote_ahead_does_not_force_push(self):
        self.adapter.libraries['codex'][2] = True
        self.enable()
        self.adapter.results.extend([{'ok': False, 'code': 'git_timeout'}, {'ok': False, 'code': 'remote_ahead'}])
        self.advance(120)
        self.assertEqual(self.worker.status('codex')['state'], 'retry_wait')
        self.advance(119)
        self.assertEqual(len(self.adapter.transfers), 1)
        self.advance(1)
        self.advance(0)
        self.assertEqual([op for _, op, _ in self.adapter.transfers], ['sync_push', 'sync_push', 'sync_pull'])

    def test_close_and_disable_do_not_wait_for_transfer_or_queue_another(self):
        self.adapter.block = True
        self.enable()
        self.clock.advance(300)
        self.worker.start()
        worker = self.worker._worker
        self.worker.start()
        self.assertIs(worker, self.worker._worker)
        self.addCleanup(self.adapter.release.set)
        self.assertTrue(self.adapter.started.wait(2))
        result = self.action('autosync_set', enabled=False)
        self.assertFalse(result['status']['enabled'])
        self.assertEqual(result['status']['state'], 'stopping')
        self.assertFalse(self.worker.stop())
        self.adapter.release.set()
        self.assertTrue(self.worker.stop(wait=2))
        self.advance(10000)
        self.assertEqual(len(self.adapter.transfers), 1)
        self.assertFalse(self.make().status('codex')['enabled'])

    def test_disable_wins_race_with_unfinished_enable_confirmation(self):
        before, release = Event(), Event()
        observe = self.adapter.observe

        def delayed(agent):
            before.set()
            release.wait(3)
            return observe(agent)

        self.adapter.observe = delayed
        results = []
        thread = Thread(target=lambda: results.append(self.action('autosync_set', enabled=True, confirm=True)))
        thread.start()
        try:
            self.assertTrue(before.wait(2))
            self.assertTrue(self.action('autosync_set', enabled=False)['ok'])
        finally:
            release.set()
            thread.join(3)
        self.assertEqual(results[0]['code'], 'autosync_busy')
        self.assertFalse(self.worker.status('codex')['enabled'])

    def test_guard_checks_backup_connection_and_digest_before_git(self):
        # The files exist only in a temporary directory. All Git creation is a test double.
        env = {'CODEX_HOME': str(self.path / 'home-codex'), 'HERMES_HOME': str(self.path / 'home-hermes')}
        with patch.dict(os.environ, env):
            remote = self.path / 'remote'
            remote.mkdir()
            other = self.path / 'other-remote'
            other.mkdir()
            lib = SkillLibrary(self.path / 'data', 'codex')
            self.assertTrue(handle(lib, 'sync_connect', {'remote_url': str(remote), 'confirm': True})['ok'])
            adapter = auto.BackupAdapter(lib.data_dir)
            previous = adapter.observe('codex')
            src = self.path / 'alpha'
            src.mkdir()
            (src / 'SKILL.md').write_text('temp skill', encoding='utf-8')
            lib.import_path(src)
            current = adapter.observe('codex')
            self.assertNotEqual(previous.digest, current.digest)
            self.assertTrue(current.dirty)
            with patch.object(auto.backup.shutil, 'which', return_value='fake'), patch.object(auto, '_CancellableGit') as git:
                self.assertEqual(adapter.transfer('codex', 'sync_push', previous, Event())['code'], 'library_changed')
                self.assertTrue(handle(lib, 'sync_connect', {'remote_url': str(other), 'confirm': True})['ok'])
                self.assertEqual(adapter.transfer('codex', 'sync_push', current, Event())['code'], 'connection_changed')
                cancelled = Event()
                cancelled.set()
                self.assertEqual(adapter.transfer('codex', 'sync_push', current, cancelled)['code'], 'cancelled')
                git.assert_not_called()

    def test_invalid_config_fails_closed_and_singleton_is_per_directory(self):
        self.assertIs(auto.get_autosync(self.path), auto.get_autosync(self.path / '.'))
        prefs = self.path / 'skills-autosync'
        prefs.mkdir()
        (prefs / 'codex.json').write_text(json.dumps({'connection': True, 'pause': None}), encoding='utf-8')
        manager = self.make()
        manager.tick()
        self.assertEqual(manager.status('codex')['last_error'], 'invalid_autosync_config')
        self.assertEqual(self.adapter.transfers, [])

    def test_backup_adapter_success_updates_baseline_with_a_git_double(self):
        env = {'CODEX_HOME': str(self.path / 'home-codex'), 'HERMES_HOME': str(self.path / 'home-hermes')}
        with patch.dict(os.environ, env):
            remote = self.path / 'remote'
            remote.mkdir()
            lib = SkillLibrary(self.path / 'state', 'codex')
            skill = self.path / 'alpha'
            skill.mkdir()
            (skill / 'SKILL.md').write_text('temp skill', encoding='utf-8')
            lib.import_path(skill)
            self.assertTrue(handle(lib, 'sync_connect', {'remote_url': str(remote), 'confirm': True})['ok'])
            adapter = auto.BackupAdapter(lib.data_dir)
            observation = adapter.observe('codex')
            self.assertTrue(observation.dirty)
            with patch.object(auto.backup.shutil, 'which', return_value='fake'), patch.object(auto, '_CancellableGit') as git:
                git.return_value.fetch.return_value = (None, None, None)
                git.return_value.push.return_value = 'a' * 40
                result = adapter.transfer('codex', 'sync_push', observation, Event())
                self.assertTrue(result['ok'], result)
                git.return_value.push.assert_called_once()
            self.assertFalse(adapter.observe('codex').dirty)
            self.assertTrue(lib.skill_path('alpha').is_dir())

    def test_cancelled_git_does_not_start_the_next_command(self):
        cancelled = Event()
        git = auto._CancellableGit.__new__(auto._CancellableGit)
        git._cancelled = cancelled
        with patch.object(auto.backup._Git, 'run') as run:
            git.run('ls-remote')
            cancelled.set()
            with self.assertRaises(auto.backup._Error) as error:
                git.run('fetch')
            self.assertEqual(error.exception.code, 'cancelled')
            self.assertEqual(run.call_count, 1)

    def test_desktop_host_uses_shared_manager_and_stops_without_draining(self):
        import build_desktop
        import desktop
        import skills_service

        self.assertIn('skills_autosync.py', build_desktop.RUNTIME_FILES)
        skills_service.check_dependencies()
        self.adapter.block = True
        self.addCleanup(self.adapter.release.set)
        data = self.path / 'desktop'
        with patch.object(desktop, 'local_data_dir', return_value=data), \
                patch.object(desktop, 'DashboardService'), \
                patch.object(auto, 'get_autosync', return_value=self.worker):
            runtime = desktop.DesktopRuntime(database=Mock(), providers_path=self.path / 'providers.json')
            port = runtime.server.server_port
            origin = f'http://127.0.0.1:{port}'

            def call(agent, op, **payload):
                connection = HTTPConnection('127.0.0.1', port, timeout=3)
                try:
                    body = json.dumps({'agent': agent, 'op': op, 'payload': payload})
                    connection.request('POST', '/api/skills', body, {'Content-Type': 'application/json',
                                        'Origin': origin, 'X-Dashboard-Request': '1'})
                    response = connection.getresponse()
                    return response.status, json.loads(response.read())
                finally:
                    connection.close()

            try:
                runtime.start()
                self.assertIs(runtime.server.skills_service.autosync, self.worker)
                self.assertTrue(self.worker._worker.is_alive())
                self.assertEqual(call('codex', 'autosync_status')[1]['result']['status']['state'], 'disabled')
                self.assertNotEqual(call('codex', 'autosync_set', enabled=True)[0], 200)
                self.assertTrue(call('codex', 'autosync_set', enabled=True, confirm=True)[1]['ok'])
                self.assertFalse(call('hermes', 'autosync_status')[1]['result']['status']['enabled'])
                self.clock.advance(300)
                self.worker._wake.set()
                self.assertTrue(self.adapter.started.wait(3))
                runtime.stop()  # The fake transfer remains blocked until explicitly released.
                self.assertTrue(self.worker._worker.is_alive())
                self.assertTrue(self.worker._stop.is_set())
                self.assertFalse(runtime.thread.is_alive())
            finally:
                self.adapter.release.set()
                runtime.stop()
                self.worker.stop(wait=2)


if __name__ == '__main__':
    unittest.main()

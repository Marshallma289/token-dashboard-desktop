"""Updater checks use synthetic packages and isolated, disposable installations."""
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, call, patch
from urllib.error import HTTPError, URLError
import warnings
import zipfile

import updater
from update_helpers import MACOS_HELPER, WINDOWS_HELPER

ROOT = Path(__file__).resolve().parents[1]


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='updater checks ', dir=ROOT.parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def archive(self, key='windows-x64', entries=None, records=None):
        prefix = 'CodexTokenDesktop/' if key == 'windows-x64' else 'CodexTokenDesktop.app/'
        entries = entries if entries is not None else [(prefix + 'program', b'program')]
        records = records if records is not None else [{'path': 'program', 'sha256': hashlib.sha256(b'program').hexdigest()}]
        manifest = {'files': records}
        archive = self.root / 'package.zip'
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with zipfile.ZipFile(archive, 'w') as output:
                for name, content in entries:
                    output.writestr(name, content)
                if key == 'windows-x64':
                    output.writestr(prefix + 'build-manifest.json', json.dumps(manifest))
        return archive, manifest

    def instance(self):
        with patch.object(updater, 'resource_dir', return_value=ROOT):
            instance = updater.Updater(self.root / 'data', lambda: None)
        instance._status['state'] = 'idle'
        instance._key = 'windows-x64'
        instance._info = {'version': '1.2.0', 'build_number': 4, 'source_commit': 'a' * 40}
        return instance

    def publication(self, **changes):
        package = {'archive': 'windows.zip', 'manifest': 'manifest.json', 'sha256': 'a' * 64,
                   'manifest_sha256': 'b' * 64, 'size': 12}
        update = {'schema': 1, 'version': '1.2.0', 'build_number': 5, 'source_commit': 'b' * 40,
                  'source_digest': 'c' * 64, 'packages': {'windows-x64': package}}
        update.update(changes)
        assets = [{'name': name, 'browser_download_url': f'https://github.com/{updater.REPOSITORY}/releases/download/test/{name}'}
                  for name in ('update.json', 'windows.zip', 'manifest.json')]
        return [json.dumps({'assets': assets}).encode(), json.dumps(update).encode()]

    def test_windows_and_mac_valid_packages(self):
        for key in ('windows-x64', 'macos-arm64'):
            with self.subTest(key=key):
                archive, manifest = self.archive(key)
                updater.validate_archive(archive, manifest, key)

    def test_unknown_archive_platform_is_rejected(self):
        archive, manifest = self.archive('macos-arm64')
        with self.assertRaises(ValueError):
            updater.validate_archive(archive, manifest, 'linux-x64')

    def test_unsafe_paths(self):
        for key in ('windows-x64', 'macos-arm64'):
            prefix = 'CodexTokenDesktop/' if key == 'windows-x64' else 'CodexTokenDesktop.app/'
            for path in ('../escape', '/absolute', prefix + '../escape', prefix + 'a\\b', prefix + 'C:escape',
                         prefix + './program', prefix + 'nested//program'):
                with self.subTest(key=key, path=path):
                    archive, manifest = self.archive(key, [(path, b'program')])
                    with self.assertRaises(ValueError):
                        updater.validate_archive(archive, manifest, key)

    def test_archive_file_and_directory_conflicts(self):
        for key in ('windows-x64', 'macos-arm64'):
            prefix = 'CodexTokenDesktop/' if key == 'windows-x64' else 'CodexTokenDesktop.app/'
            records = [{'path': path, 'sha256': hashlib.sha256(b'program').hexdigest()}
                       for path in ('program', 'program/child')]
            archive, manifest = self.archive(key, [(prefix + record['path'], b'program') for record in records], records)
            with self.assertRaisesRegex(ValueError, '冲突'):
                updater.validate_archive(archive, manifest, key)
            archive, manifest = self.archive(key, [(prefix + 'program/', b''), (prefix + 'program', b'program')])
            with self.assertRaisesRegex(ValueError, '重复'):
                updater.validate_archive(archive, manifest, key)

    def test_windows_reserved_and_trailing_characters(self):
        for relative in ('file.', 'file ', 'CON', 'con.txt', 'NUL.dat', 'LPT9', 'COM1.log', 'nested./program'):
            with self.subTest(relative=relative):
                records = [{'path': relative, 'sha256': hashlib.sha256(b'program').hexdigest()}]
                archive, manifest = self.archive(entries=[('CodexTokenDesktop/' + relative, b'program')], records=records)
                with self.assertRaisesRegex(ValueError, 'Windows 文件名'):
                    updater.validate_archive(archive, manifest, 'windows-x64')

    def test_duplicate_archive_and_manifest_paths(self):
        for key in ('windows-x64', 'macos-arm64'):
            prefix = 'CodexTokenDesktop/' if key == 'windows-x64' else 'CodexTokenDesktop.app/'
            archive, manifest = self.archive(key, [(prefix + 'program', b'program')] * 2)
            with self.assertRaisesRegex(ValueError, '重复'):
                updater.validate_archive(archive, manifest, key)
            archive, manifest = self.archive(key)
            manifest['files'] *= 2
            with self.assertRaisesRegex(ValueError, '重复'):
                updater.validate_archive(archive, manifest, key)
        archive, manifest = self.archive(entries=[('CodexTokenDesktop/program', b'program'), ('CodexTokenDesktop/PROGRAM', b'program')])
        with self.assertRaisesRegex(ValueError, '重复'):
            updater.validate_archive(archive, manifest, 'windows-x64')

    def test_bad_hash_missing_and_undeclared_files(self):
        for key in ('windows-x64', 'macos-arm64'):
            prefix = 'CodexTokenDesktop/' if key == 'windows-x64' else 'CodexTokenDesktop.app/'
            for entries, records in [([(prefix + 'program', b'corrupt')], None), ([], None),
                                     ([(prefix + 'other', b'program')], None)]:
                with self.subTest(key=key, entries=entries):
                    archive, manifest = self.archive(key, entries, records)
                    with self.assertRaises(ValueError):
                        updater.validate_archive(archive, manifest, key)

    def test_mac_symlink_stays_inside_bundle(self):
        for target, succeeds in [('program', True), ('../../../outside', False), ('/outside', False)]:
            archive = self.root / 'links.zip'
            link = zipfile.ZipInfo('CodexTokenDesktop.app/link')
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            manifest = {'files': [{'path': 'link', 'type': 'symlink', 'target': target}]}
            with zipfile.ZipFile(archive, 'w') as output:
                output.writestr(link, target)
            if succeeds:
                updater.validate_archive(archive, manifest, 'macos-arm64')
            else:
                with self.assertRaises(ValueError):
                    updater.validate_archive(archive, manifest, 'macos-arm64')

    def test_mac_symlink_chains_escape_and_cycles(self):
        cases = [({'link': '.'}, True),
                 ({'link': '.', 'escape': 'link/../outside'}, False),
                 ({'first': 'second', 'second': 'first'}, False),
                 ({'self': 'self'}, False)]
        for links, succeeds in cases:
            with self.subTest(links=links):
                archive = self.root / 'chain.zip'
                manifest = {'files': [{'path': name, 'type': 'symlink', 'target': target} for name, target in links.items()]}
                with zipfile.ZipFile(archive, 'w') as output:
                    for name, target in links.items():
                        entry = zipfile.ZipInfo('CodexTokenDesktop.app/' + name)
                        entry.create_system = 3
                        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
                        output.writestr(entry, target)
                if succeeds:
                    updater.validate_archive(archive, manifest, 'macos-arm64')
                else:
                    with self.assertRaises(ValueError):
                        updater.validate_archive(archive, manifest, 'macos-arm64')

    def test_same_version_new_build_is_available(self):
        instance = self.instance()
        with patch.object(updater, 'fetch_bytes', side_effect=self.publication()):
            instance._check()
        self.assertEqual(instance.status()['state'], 'available')
        self.assertIn('构建 5', instance.status()['latest_version'])

    def test_old_build_same_commit_and_older_version_are_current(self):
        for changes in ({'build_number': 4}, {'source_commit': 'a' * 40}, {'version': '1.1.0', 'build_number': 99}):
            instance = self.instance()
            with patch.object(updater, 'fetch_bytes', side_effect=self.publication(**changes)):
                instance._check()
            self.assertEqual(instance.status()['state'], 'current')

    def test_no_release_and_network_errors(self):
        for error, state in [(HTTPError(updater.API, 404, 'missing', {}, None), 'current'),
                             (HTTPError(updater.API, 503, 'offline', {}, None), 'error'),
                             (URLError('offline'), 'error')]:
            if isinstance(error, HTTPError):
                self.addCleanup(error.close)
            instance = self.instance()
            with patch.object(updater, 'fetch_bytes', side_effect=error):
                instance._check()
            self.assertEqual(instance.status()['state'], state)

    def test_malformed_publication_types_become_error(self):
        valid_release, valid_update = (json.loads(payload) for payload in self.publication())
        cases = [(value, valid_update) for value in (None, [], 'release', 7)]
        cases += [({'assets': value}, valid_update) for value in (None, {}, ['bad asset'])]
        cases += [(valid_release, value) for value in (None, [], 'update', 7)]
        for value in (None, [], 'packages', 7):
            cases.append((valid_release, {**valid_update, 'packages': value}))
        for value in (None, [], 'selected', 7):
            cases.append((valid_release, {**valid_update, 'packages': {'windows-x64': value}}))
        for release, update in cases:
            with self.subTest(release=release, update=update):
                instance = self.instance()
                with patch.object(updater, 'fetch_bytes', side_effect=[json.dumps(release).encode(), json.dumps(update).encode()]):
                    instance._check()
                self.assertEqual(instance.status()['state'], 'error')
                self.assertIsNone(instance._selected)

    def test_installation_preflight_rejection_keeps_window_open(self):
        instance = self.instance()
        instance._selected = ({}, {})
        # AppTranslocation is prohibited before any file or network operation.
        unsafe = self.root / 'AppTranslocation' / 'application.app'
        with patch.object(updater, 'installation_dir', return_value=unsafe), \
                patch.object(updater, 'fetch_bytes') as fetch, \
                patch.object(instance, '_close_window') as close:
            instance._download()
            fetch.assert_not_called()
            close.assert_not_called()
        self.assertEqual(instance.status()['state'], 'error')
        self.assertIsNone(instance._job_path)
        self.assertIsNone(instance._lock_file)
        self.assertFalse(unsafe.parent.exists())

    def test_external_environment_cleans_bundle_paths_and_restores_dll_search(self):
        import ctypes
        bundle = self.root / 'bundle'
        system = str(self.root / 'system tools')
        environment = {'PATH': os.pathsep.join((str(bundle), str(bundle / 'lib'), system)),
                       'DYLD_LIBRARY_PATH': os.pathsep.join((str(bundle / 'libraries'), system)),
                       'DYLD_FRAMEWORK_PATH': str(bundle / 'frameworks'),
                       '_PYI_APPLICATION_HOME_DIR': str(bundle), '_PYI_PARENT_PROCESS_LEVEL': '1',
                       'KEEP_SETTING': 'retained'}
        for raises in (False, True):
            with self.subTest(raises=raises), patch.dict(os.environ, environment, clear=True), \
                    patch.object(updater.sys, 'platform', 'win32'), \
                    patch.object(updater.sys, 'frozen', True, create=True), \
                    patch.object(updater, 'resource_dir', return_value=bundle), \
                    patch.object(ctypes, 'windll', Mock(), create=True) as dll:
                try:
                    with updater.external_environment() as cleaned:
                        self.assertEqual(cleaned['PATH'], system)
                        self.assertEqual(cleaned['DYLD_LIBRARY_PATH'], system)
                        self.assertEqual(cleaned['DYLD_FRAMEWORK_PATH'], '')
                        self.assertEqual(cleaned['KEEP_SETTING'], 'retained')
                        self.assertEqual(cleaned['PYINSTALLER_RESET_ENVIRONMENT'], '1')
                        self.assertFalse(any(key.startswith('_PYI_') for key in cleaned))
                        dll.kernel32.SetDllDirectoryW.assert_called_once_with(None)
                        self.assertEqual(dict(os.environ), environment)
                        if raises:
                            raise RuntimeError('simulated subprocess launch failure')
                except RuntimeError:
                    self.assertTrue(raises)
                self.assertEqual(dll.kernel32.SetDllDirectoryW.call_args_list, [call(None), call(str(bundle))])

    def test_external_environment_source_run_preserves_path(self):
        with patch.dict(os.environ, {'PATH': 'original path', '_PYI_TEST': 'remove'}, clear=True), \
                patch.object(updater.sys, 'frozen', False, create=True):
            with updater.external_environment() as cleaned:
                self.assertEqual(cleaned['PATH'], 'original path')
                self.assertNotIn('_PYI_TEST', cleaned)
                self.assertEqual(cleaned['PYINSTALLER_RESET_ENVIRONMENT'], '1')

    def test_installer_handoff_and_early_exit_restarts_old_application(self):
        for platform in ('win32', 'darwin'):
            for early_exit in (False, True):
                with self.subTest(platform=platform, early_exit=early_exit):
                    instance = self.instance()
                    identifier = ('d' if early_exit else 'e') * 32
                    case = self.root / (platform + str(early_exit))
                    case.mkdir()
                    target = case / ('application.app' if platform == 'darwin' else 'application')
                    target.mkdir()
                    (target / 'original').write_text('old program')
                    stage = case / ('.codex-token-update-' + identifier)
                    stage.mkdir()
                    candidate = stage / 'candidate' / ('CodexTokenDesktop.app' if platform == 'darwin' else 'CodexTokenDesktop')
                    job = {'id': identifier, 'platform': platform, 'target': str(target), 'candidate': str(candidate),
                           'backup': str(target) + '.rollback-' + identifier, 'parent_pid': os.getpid()}
                    path = stage / 'job.json'
                    updater.write_json(path, job)
                    instance._job_path = path
                    instance._lock_file = case / '.application.update.lock'
                    updater.write_json(instance._lock_file, {'pid': os.getpid()})
                    helper = Mock(pid=123456)
                    helper.wait.side_effect = None if early_exit else subprocess.TimeoutExpired('helper', 1)
                    helper.wait.return_value = 1 if early_exit else None
                    with patch.object(updater.sys, 'platform', platform), \
                            patch.object(updater.sys, 'frozen', False, create=True), \
                            patch.object(updater, 'installation_dir', return_value=target), \
                            patch.dict(os.environ, {'SystemRoot': str(self.root), '_PYI_TEST': 'remove'}), \
                            patch.object(subprocess, 'CREATE_NO_WINDOW', 0x08000000, create=True), \
                            patch.object(updater.subprocess, 'Popen', side_effect=[helper, Mock(pid=654321)]) as launch:
                        instance.launch_installer()
                    helper.wait.assert_called_once_with(timeout=1)
                    self.assertTrue((stage / 'installer.log').is_file())
                    first = launch.call_args_list[0]
                    self.assertEqual(first.kwargs['cwd'], stage)
                    self.assertNotIn('_PYI_TEST', first.kwargs['env'])
                    self.assertEqual(first.kwargs['env']['PYINSTALLER_RESET_ENVIRONMENT'], '1')
                    self.assertIs(first.kwargs['stdout'], first.kwargs['stderr'])
                    if platform == 'win32':
                        self.assertEqual(first.kwargs['creationflags'], 0x08000000)
                    else:
                        self.assertTrue(first.kwargs['start_new_session'])
                    self.assertEqual((target / 'original').read_text(), 'old program')
                    if early_exit:
                        self.assertEqual(launch.call_count, 2)
                        result = updater.read_json(stage / 'result.json')
                        self.assertEqual(result['state'], 'error')
                        self.assertIn('等待程序退出前终止', result['message'])
                        self.assertFalse(instance._lock_file.exists())
                        expected = target / ('CodexTokenDesktop.exe' if platform == 'win32' else 'Contents/MacOS/CodexTokenDesktop')
                        self.assertEqual(launch.call_args_list[1].args[0], [str(expected)])
                        self.assertNotIn('_PYI_TEST', launch.call_args_list[1].kwargs['env'])
                    else:
                        self.assertEqual(launch.call_count, 1)
                        self.assertFalse((stage / 'result.json').exists())
                        self.assertEqual(updater.read_json(instance._lock_file), {'pid': helper.pid, 'job': str(path)})

    def test_unsupported_platform_cannot_start(self):
        with patch.object(updater, 'resource_dir', return_value=ROOT), patch.object(updater.sys, 'platform', 'linux'), \
                patch.object(updater.sys, 'frozen', True, create=True):
            instance = updater.Updater(self.root / 'data', lambda: None)
            with patch.object(updater.threading, 'Thread') as thread:
                self.assertEqual(instance.check()['state'], 'unsupported')
                self.assertEqual(instance.start()['state'], 'unsupported')
                thread.assert_not_called()

    def test_repeated_clicks_create_only_one_worker(self):
        instance = self.instance()
        with patch.object(updater.threading, 'Thread') as thread:
            instance.check()
            instance.check()
            self.assertEqual(thread.call_count, 1)
            thread.reset_mock()
            instance._status['state'] = 'available'
            instance._selected = ({}, {})
            instance.start()
            instance.start()
            self.assertEqual(thread.call_count, 1)

    def test_manifest_platform_mismatch_leaves_installation_untouched(self):
        instance = self.instance()
        target = self.root / 'installation'
        target.mkdir()
        (target / 'old').write_text('original')
        payload = b'synthetic archive'
        manifest = {'version': '1.2.0', 'source_commit': 'b' * 40, 'source_digest': 'c' * 64,
                    'architecture': 'x64', 'platform': 'wrong-platform'}
        manifest_bytes = json.dumps(manifest).encode()
        instance._selected = (manifest, {'archive_url': 'archive', 'manifest_url': 'manifest', 'size': len(payload),
                                       'sha256': hashlib.sha256(payload).hexdigest(),
                                       'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest()})
        def fetch(url, limit, **kwargs):
            if 'target' in kwargs:
                kwargs['target'].write_bytes(payload)
                return b''
            return manifest_bytes
        with patch.object(updater, 'installation_dir', return_value=target), patch.object(updater, 'fetch_bytes', side_effect=fetch):
            instance._download()
        self.assertEqual(instance.status()['state'], 'error')
        self.assertIn('发布版本不一致', instance.status()['message'])
        self.assertEqual((target / 'old').read_text(), 'original')
        self.assertFalse(target.with_name('.installation.update.lock').exists())


class NativeHelperTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='helper checks with spaces ', dir=ROOT.parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def make_job(self, platform):
        identifier = 'c' * 32
        name = 'CodexTokenDesktop.app' if platform == 'darwin' else 'CodexTokenDesktop'
        target = self.root / ('installed application.app' if platform == 'darwin' else 'installed application')
        stage = self.root / ('.codex-token-update-' + identifier)
        candidate = stage / 'candidate' / name
        target.mkdir()
        candidate.mkdir(parents=True)
        (target / 'identity').write_text('old')
        (candidate / 'identity').write_text('new')
        process = subprocess.Popen([sys.executable, '-c', 'pass'])
        process.wait(timeout=10)
        job = {'id': identifier, 'platform': platform, 'target': str(target), 'candidate': str(candidate),
               'backup': str(target) + '.rollback-' + identifier, 'parent_pid': process.pid}
        path = stage / 'job.json'
        updater.write_json(path, job)
        return path, job

    def assert_success(self, path, job):
        result = updater.read_json(path.parent / 'result.json')
        self.assertEqual(result['state'], 'success')
        self.assertEqual(result['backup'], job['backup'])
        self.assertEqual((Path(job['target']) / 'identity').read_text(), 'new')
        self.assertEqual((Path(job['backup']) / 'identity').read_text(), 'old')
        self.assertFalse(Path(job['candidate']).exists())
        ack = updater.read_json(path.parent / 'ack.json')
        self.assertEqual(ack['id'], job['id'])
        self.assertIsInstance(ack['pid'], int)
        self.assertTrue(updater.process_alive(ack['pid']))
        os.kill(ack['pid'], signal.SIGTERM)
        deadline = time.monotonic() + 10
        while updater.process_alive(ack['pid']) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(updater.process_alive(ack['pid']))

    def assert_rollback(self, path, job):
        result = updater.read_json(path.parent / 'result.json')
        self.assertEqual(result['state'], 'error')
        self.assertTrue(result['message'])
        self.assertEqual((Path(job['target']) / 'identity').read_text(), 'old')
        self.assertEqual((path.parent / 'failed' / 'identity').read_text(), 'new')
        self.assertFalse(Path(job['backup']).exists())

    @unittest.skipUnless(sys.platform in ('win32', 'darwin'), 'native desktop helper')
    def test_helper_rejects_outside_candidate_and_stale_ack(self):
        for invalid in ('outside candidate', 'stale ack', 'existing backup', 'wrong platform'):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory(prefix='reject ', dir=self.root) as directory:
                previous = self.root
                self.root = Path(directory).resolve()
                try:
                    path, job = self.make_job(sys.platform)
                    for base in (Path(job['target']), Path(job['candidate'])):
                        relative = 'CodexTokenDesktop.exe' if sys.platform == 'win32' else 'Contents/MacOS/CodexTokenDesktop'
                        executable = base / relative
                        executable.parent.mkdir(parents=True, exist_ok=True)
                        executable.write_bytes(b'never launched')
                        executable.chmod(0o755)
                    if invalid == 'outside candidate':
                        outside = self.root / 'outside candidate'
                        outside.mkdir()
                        (outside / 'untouched').write_text('safe')
                        job['candidate'] = str(outside)
                        updater.write_json(path, job)
                    elif invalid == 'stale ack':
                        updater.write_json(path.parent / 'ack.json', {'id': job['id'], 'pid': os.getpid()})
                    elif invalid == 'existing backup':
                        Path(job['backup']).mkdir()
                        (Path(job['backup']) / 'untouched').write_text('safe')
                    else:
                        job['platform'] = 'linux'
                        updater.write_json(path, job)
                    if sys.platform == 'win32':
                        script = path.parent / 'install.ps1'
                        script.write_text(WINDOWS_HELPER, encoding='utf-8-sig')
                        shell = Path(os.environ['WINDIR']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
                        command = [str(shell), '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script), '-JobPath', str(path)]
                    else:
                        script = path.parent / 'install.sh'
                        script.write_text(MACOS_HELPER)
                        command = ['/bin/sh', str(script), str(path)]
                    completed = subprocess.run(command, capture_output=True, timeout=15)
                    self.assertEqual(completed.returncode, 1)
                    self.assertEqual((Path(job['target']) / 'identity').read_text(), 'old')
                    self.assertFalse((path.parent / 'failed').exists())
                    if invalid == 'outside candidate':
                        self.assertEqual((outside / 'untouched').read_text(), 'safe')
                    elif invalid == 'existing backup':
                        self.assertEqual((Path(job['backup']) / 'untouched').read_text(), 'safe')
                    if invalid in ('stale ack', 'existing backup'):
                        self.assertEqual(updater.read_json(path.parent / 'result.json')['state'], 'error')
                finally:
                    self.root = previous

    @unittest.skipUnless(sys.platform == 'win32', 'native Windows helper')
    def test_windows_success_ack_and_bad_executable_rollback(self):
        compiler = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        if not compiler.is_file():
            self.skipTest('.NET Framework C# compiler unavailable')
        source = self.root / 'Simulator.cs'
        source.write_text(r'''using System; using System.IO; using System.Diagnostics; using System.Threading;
class Simulator { static void Main(string[] args) { if(args.Length != 2) return;
string job = args[1]; string id = Directory.GetParent(job).Name.Substring(".codex-token-update-".Length);
File.WriteAllText(Path.Combine(Path.GetDirectoryName(job), "ack.json"), "{\"id\":\"" + id + "\",\"pid\":" + Process.GetCurrentProcess().Id + "}"); Thread.Sleep(120000); }}''')
        exe = self.root / 'simulator.exe'
        subprocess.run([str(compiler), '/nologo', '/target:exe', '/out:' + str(exe), str(source)], check=True, capture_output=True)
        for success in (True, False):
            with self.subTest(success=success), tempfile.TemporaryDirectory(prefix='case ', dir=self.root) as directory:
                previous = self.root
                self.root = Path(directory)
                try:
                    path, job = self.make_job('win32')
                    import shutil
                    shutil.copy2(exe, Path(job['target']) / 'CodexTokenDesktop.exe')
                    candidate_exe = Path(job['candidate']) / 'CodexTokenDesktop.exe'
                    if success:
                        shutil.copy2(exe, candidate_exe)
                    else:
                        candidate_exe.write_bytes(b'not a Windows executable')
                    helper = path.parent / 'install.ps1'
                    helper.write_text(WINDOWS_HELPER, encoding='utf-8-sig')
                    shell = Path(os.environ['WINDIR']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
                    completed = subprocess.run([str(shell), '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(helper), '-JobPath', str(path)], capture_output=True, timeout=30)
                    self.assertEqual(completed.returncode, 0 if success else 1, completed.stderr.decode(errors='replace'))
                    (self.assert_success if success else self.assert_rollback)(path, job)
                    # Windows Start-Process returns before the short rollback
                    # simulator has finished; let its image handle close.
                    time.sleep(1)
                finally:
                    self.root = previous

    @unittest.skipUnless(sys.platform == 'darwin', 'native macOS helper')
    def test_mac_success_ack_and_early_exit_rollback(self):
        for success in (True, False):
            with self.subTest(success=success), tempfile.TemporaryDirectory(prefix='case ', dir=self.root) as directory:
                previous = self.root
                self.root = Path(directory).resolve()
                try:
                    path, job = self.make_job('darwin')
                    for directory_path, candidate in [(Path(job['target']), False), (Path(job['candidate']), True)]:
                        executable = directory_path / 'Contents/MacOS/CodexTokenDesktop'
                        executable.parent.mkdir(parents=True)
                        code = '#!' + sys.executable + '\nimport json, os, pathlib, sys, time\n'
                        if candidate and success:
                            code += 'job = pathlib.Path(sys.argv[2])\ndata = json.loads(job.read_text())\n(job.parent / "ack.json").write_text(json.dumps({"id": data["id"], "pid": os.getpid()}))\ntime.sleep(120)\n'
                        executable.write_text(code)
                        executable.chmod(0o755)
                    helper = path.parent / 'install.sh'
                    helper.write_text(MACOS_HELPER)
                    completed = subprocess.run(['/bin/sh', str(helper), str(path)], capture_output=True, timeout=30)
                    self.assertEqual(completed.returncode, 0 if success else 1, completed.stderr.decode(errors='replace'))
                    (self.assert_success if success else self.assert_rollback)(path, job)
                finally:
                    self.root = previous


if __name__ == '__main__':
    unittest.main()

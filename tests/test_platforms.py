import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import desktop


class DesktopPlatformTests(unittest.TestCase):
    def test_windows_keeps_existing_data_location(self):
        with patch.object(sys, 'platform', 'win32'), patch.dict(desktop.os.environ, {'LOCALAPPDATA': 'local-user-data'}):
            self.assertEqual(desktop.local_data_dir(), Path('local-user-data') / 'CodexTokenDashboard')

    def test_mac_uses_application_support(self):
        with patch.object(sys, 'platform', 'darwin'), patch.object(Path, 'home', return_value=Path('mac-user')):
            self.assertEqual(desktop.local_data_dir(), Path('mac-user/Library/Application Support/CodexTokenDashboard'))

    def test_provider_configuration_uses_writable_mac_directory_and_windows_portable_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data, application = root / 'data', root / 'application'
            data.mkdir()
            application.mkdir()
            (data / 'providers.json').write_text('{}', encoding='utf-8')
            (application / 'providers.json').write_text('{}', encoding='utf-8')
            for platform, expected in [('darwin', data), ('win32', application)]:
                with self.subTest(platform=platform), patch.object(sys, 'platform', platform), patch.object(desktop, 'local_data_dir', return_value=data), patch.object(desktop, 'application_dir', return_value=application), patch.object(desktop, 'DashboardDB') as database, patch.object(desktop, 'create_server'):
                    desktop.DesktopRuntime()
                    self.assertEqual(database.call_args.kwargs['providers_path'], expected / 'providers.json')
                    self.assertEqual(database.call_args.args[0], data / 'usage.sqlite3')

    def test_webview2_restart_marker_is_not_used_on_mac(self):
        with patch.object(sys, 'platform', 'darwin'), patch.object(desktop, 'local_data_dir') as directory:
            desktop._wait_for_webview_cleanup()
            desktop._record_clean_exit()
            directory.assert_not_called()

    def test_main_selects_native_renderer_without_changing_window_or_bridge(self):
        for platform, renderer in [('win32', 'edgechromium'), ('darwin', None)]:
            webview = types.ModuleType('webview')
            webview.create_window = Mock(return_value=Mock())
            webview.start = Mock()
            with self.subTest(platform=platform), patch.object(sys, 'platform', platform), patch.dict(sys.modules, {'webview': webview}), patch.object(desktop, 'DesktopRuntime') as runtime, patch.object(desktop, '_wait_for_webview_cleanup'), patch.object(desktop, '_record_clean_exit'):
                runtime.return_value.start.return_value = 'http://127.0.0.1:12345/?desktop=1'
                self.assertEqual(desktop.main(), 0)
                self.assertEqual(webview.start.call_args.kwargs['gui'], renderer)
                self.assertTrue(webview.start.call_args.kwargs['private_mode'])
                self.assertEqual(webview.create_window.call_args.kwargs['width'], 1440)
                self.assertIsInstance(webview.create_window.call_args.kwargs['js_api'], desktop.DesktopBridge)
                runtime.return_value.stop.assert_called_once()

    def test_mac_error_message_is_passed_as_data(self):
        message = 'Failure with "quotes" and a newline\nDetails'
        with patch.object(sys, 'platform', 'darwin'), patch.object(desktop.os, 'name', 'posix'), patch.object(desktop.subprocess, 'run') as run:
            desktop._show_startup_error(message)
        args = run.call_args.args[0]
        self.assertEqual(args[:2], ['osascript', '-e'])
        self.assertEqual(args[-1], message)
        self.assertNotIn(message, args[2])


if __name__ == '__main__':
    unittest.main()

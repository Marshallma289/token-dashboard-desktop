"""First-run data source diagnostics checks using isolated local paths."""
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import json

from backend import DashboardDB, DashboardService, create_server
from tests.test_hermes import make_hermes
from tests.test_review_regressions import session, turn, usage, write_lines


class SourceDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.codex_home = self.root / 'codex'
        self.sessions = self.codex_home / 'sessions'
        self.archive = self.codex_home / 'archived_sessions'
        self.hermes = self.root / 'hermes' / 'state.db'
        environment = patch.dict(os.environ, {'CODEX_HOME': str(self.codex_home)})
        environment.start()
        self.addCleanup(environment.stop)

    def service(self, **kwargs):
        db = DashboardDB(self.root / 'usage.db', timezone_name='UTC', **kwargs)
        self.addCleanup(db.close)
        return DashboardService(db)

    def diagnostics(self, service, host='127.0.0.1', headers=None):
        server = create_server(service, host, 0, skills_data_dir=self.root / 'state')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = Request(f'http://127.0.0.1:{server.server_port}/api/diagnostics', headers=headers or {})
            with urlopen(request, timeout=4) as response:
                payload = json.load(response)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=4)
        return {source['kind']: source for source in payload['sources']}

    def test_fresh_workspace_has_no_false_archive_warning_or_source_writes(self):
        self.sessions.mkdir(parents=True)
        service = self.service(hermes_db=self.hermes, hermes_explicit=False)
        service.refresh()
        self.assertEqual(service.health()['scan']['state'], 'ok')
        sources = self.diagnostics(service)
        self.assertEqual(sources['codex']['status'], 'empty')
        self.assertEqual(sources['codex']['available_records'], 0)
        self.assertEqual([p['status'] for p in sources['codex']['paths']], ['readable', 'optional_missing'])
        self.assertEqual(sources['hermes']['status'], 'not_configured')
        self.assertEqual(sources['hermes']['paths'][0]['path'], str(self.hermes))
        self.assertFalse(self.archive.exists())
        self.assertFalse(self.hermes.parent.exists())

    def test_never_used_default_install_is_not_configured(self):
        service = self.service()
        service.refresh()
        self.assertEqual(service.health()['scan']['state'], 'ok')
        sources = self.diagnostics(service)
        self.assertEqual(sources['codex']['status'], 'not_configured')

    def test_requested_missing_or_invalid_paths_are_actual_errors(self):
        wrong = self.root / 'not-a-directory'
        wrong.write_text('private data', encoding='utf-8')
        service = self.service(roots=[wrong, self.root / 'misspelled'], hermes_db=self.hermes)
        service.refresh()
        self.assertEqual(service.health()['scan']['state'], 'partial')
        sources = self.diagnostics(service)
        self.assertEqual(sources['codex']['status'], 'error')
        self.assertEqual([p['status'] for p in sources['codex']['paths']], ['invalid', 'missing'])
        self.assertEqual(sources['hermes']['status'], 'error')
        self.assertEqual(sources['hermes']['paths'][0]['status'], 'missing')

    def test_loaded_and_retained_counts_after_a_source_fails(self):
        self.sessions.mkdir(parents=True)
        write_lines(self.sessions / 'sample.jsonl', [session(), turn('fixture'), usage('thread-a', 'r1', 50)])
        make_hermes(self.hermes)
        service = self.service(hermes_db=self.hermes)
        service.refresh()
        sources = self.diagnostics(service)
        for kind in ('codex', 'hermes'):
            self.assertEqual(sources[kind]['status'], 'loaded')
            self.assertEqual(sources[kind]['available_records'], 1)
        self.assertEqual(sources['codex']['paths'][0]['files_seen'], 1)
        self.assertEqual(sources['hermes']['paths'][0]['records_seen'], 1)
        service.refresh()
        self.assertEqual(self.diagnostics(service)['hermes']['paths'][0]['records_seen'], 1)
        self.hermes.write_bytes(b'not SQLite')
        service.refresh(force=True)
        sources = self.diagnostics(service)
        self.assertEqual(service.health()['scan']['state'], 'partial')
        self.assertEqual(sources['hermes']['status'], 'error')
        self.assertEqual(sources['hermes']['available_records'], 1)
        self.assertEqual(sources['hermes']['paths'][0]['status'], 'invalid')

    def test_lan_listener_does_not_expose_local_paths(self):
        service = self.service()
        with self.assertRaises(HTTPError) as error:
            self.diagnostics(service, '0.0.0.0')
        with error.exception as response:
            self.assertEqual(response.code, 403)
            self.assertNotIn(str(self.root), response.read().decode())

    def test_cross_origin_request_does_not_expose_local_paths(self):
        service = self.service()
        with self.assertRaises(HTTPError) as error:
            self.diagnostics(service, headers={'Origin': 'https://foreign.example', 'Sec-Fetch-Site': 'cross-site'})
        with error.exception as response:
            self.assertEqual(response.code, 403)
            self.assertNotIn(str(self.root), response.read().decode())

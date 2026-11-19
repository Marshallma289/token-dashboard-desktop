"""Public HTTP, private library and real deployment roots must agree."""
from contextlib import closing
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

from backend import DashboardDB, DashboardService, create_server
from skills_core import SkillLibrary


class SkillsWorkflowTests(unittest.TestCase):
    def test_same_name_in_two_workspaces_backup_and_disable(self):
        with tempfile.TemporaryDirectory() as temp:
            # macOS TMPDIR may start at /var, a symlink to /private/var.
            base = Path(temp).resolve(strict=True)
            homes = {agent: base / agent for agent in ('codex', 'hermes')}
            env = {f'{agent.upper()}_HOME': str(home) for agent, home in homes.items()}
            db = DashboardDB(base / 'usage.sqlite3', roots=[base / 'sessions'])
            service = DashboardService(db)
            with patch.dict(os.environ, env), create_server(service, port=0, skills_data_dir=base/'app') as http:
                origin = f'http://127.0.0.1:{http.server_port}'
                worker = threading.Thread(target=http.serve_forever, daemon=True)
                worker.start()

                def call(agent, op, **payload):
                    req = Request(origin+'/api/skills', method='POST', headers={
                        'Content-Type': 'application/json', 'Origin': origin, 'X-Dashboard-Request': '1',
                    }, data=json.dumps(dict(agent=agent, op=op, payload=payload)).encode())
                    with closing(urlopen(req, timeout=10)) as response:
                        data = json.load(response)
                    self.assertTrue(data['ok'], data)
                    result = data['result']
                    if isinstance(result, dict):
                        self.assertIsNot(result.get('ok'), False, result)
                    return result

                def deployed(agent):
                    return homes[agent] / 'skills' / 'paired-skill' / 'SKILL.md'

                try:
                    for agent in homes:
                        src = base/'fixtures'/agent/'paired-skill'
                        src.mkdir(parents=True)
                        (src/'SKILL.md').write_text(f'---\nname: paired-skill\ndescription: {agent}\n---\n{agent}\n', encoding='utf-8')
                        SkillLibrary(base/'app',agent).import_path(src)
                        self.assertEqual(len(call(agent,'state')['skills']),1)
                        call(agent, 'install', id='paired-skill', mode='copy')
                        self.assertEqual(deployed(agent).read_bytes(),(src/'SKILL.md').read_bytes())
                    call('codex','backup_create')
                    self.assertEqual(len(call('codex','backup_list')['backups']),1)
                    self.assertEqual(call('hermes','backup_list')['backups'],[])
                    call('codex','disable', id='paired-skill')
                    self.assertFalse(deployed('codex').exists())
                    self.assertTrue(deployed('hermes').is_file())
                    self.assertTrue(call('hermes','state')['skills'][0]['installed'])
                finally:
                    http.shutdown()
                    worker.join(5)
                    db.close()


if __name__ == '__main__':
    unittest.main()

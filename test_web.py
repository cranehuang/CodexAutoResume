import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest

import guardian
import web


class WebConsoleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        status = {
            'updated_at': guardian.utcnow(),
            'mode': 'send',
            'interval': 120,
            'socket': '/tmp/x.sock',
            'socket_ok': True,
            'monitor_all': False,
            'scan': {'threads': 3, 'candidates': 1, 'unmonitored': 1, 'errors': 0,
                     'duplicates': 0, 'duration_ms': 900},
            'quota': {'ordinaryUsageAllowed': False,
                      'primary': {'usedPercent': 100, 'resetsAt': None},
                      'secondary': None},
            'eligible': [{'thread_id': 't-1', 'failed_turn_id': 'f-1', 'cwd': '/repo/a',
                          'status': 'idle', 'project': '/repo/a', 'project_label': ''}],
            'unmonitored': [],
            'submitted': [],
            'waiting': 'quota_blocked',
            'last_error': None,
        }
        (self.root / guardian.STATUS_NAME).write_text(json.dumps(status))

        conn = guardian.db_open(self.root / 'attempts.sqlite3')
        with conn:
            conn.execute('INSERT INTO attempts (thread_id, failed_turn_id, claimed_at, state, created_turn_id, '
                         'title, cwd, project, project_label) VALUES (?,?,?,?,?,?,?,?,?)',
                         ('t-1', 'f-1', guardian.utcnow(), 'submitted', 'n-1', '示例会话',
                          '/repo/a', '/repo/a', '项目 A'))
        conn.close()

        self.console = web.Console(self.root, Path(__file__).resolve().parent / 'webui',
                                   '/nonexistent.sock', live=False)
        self.httpd = web.make_server(self.console, '127.0.0.1', 0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)
        self.tmp.cleanup()

    def raw(self, method, path, body=None, ctype='application/json'):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        headers = {}
        if body is not None:
            body = json.dumps(body)
            headers['Content-Type'] = ctype
        conn.request(method, path, body=body, headers=headers)
        res = conn.getresponse()
        data = res.read()
        conn.close()
        return res.status, res.getheader('Content-Type'), data

    def call(self, method, path, body=None, ctype='application/json'):
        code, _, data = self.raw(method, path, body, ctype)
        return code, json.loads(data or b'{}')

    def test_overview(self):
        code, data = self.call('GET', '/api/overview')
        self.assertEqual(code, 200)
        self.assertFalse(data['service']['running'])
        self.assertEqual(data['status']['scan']['threads'], 3)
        self.assertEqual(data['attempts']['counts']['submitted'], 1)
        self.assertEqual(data['attempts']['recent'][0]['thread_id'], 't-1')
        self.assertEqual(data['attempts']['recent'][0]['title'], '示例会话')
        self.assertEqual(data['attempts']['recent'][0]['project_label'], '项目 A')
        self.assertEqual(data['attempts']['recent'][0]['cwd'], '/repo/a')
        self.assertEqual(data['config']['projects'], [])

    def test_static_index_and_traversal(self):
        code, ctype, data = self.raw('GET', '/')
        self.assertEqual(code, 200)
        self.assertIn('text/html', ctype)
        self.assertIn(b'Codex Auto Resume', data)
        code, _, _ = self.raw('GET', '/../guardian.py')
        self.assertEqual(code, 403)

    def test_config_put_roundtrip(self):
        project = self.root / 'proj'
        project.mkdir()
        code, data = self.call('PUT', '/api/config', {'projects': [
            {'path': str(project), 'label': 'demo', 'enabled': False},
            {'path': str(project) + '/', 'label': 'duplicate'},
        ]})
        self.assertEqual(code, 200)
        self.assertEqual(len(data['projects']), 1)
        self.assertEqual(data['projects'][0]['label'], 'demo')
        self.assertFalse(data['projects'][0]['enabled'])

        saved = json.loads((self.root / guardian.CONFIG_NAME).read_text())
        self.assertEqual(saved['projects'][0]['path'], str(project))
        self.assertEqual(os.stat(self.root / guardian.CONFIG_NAME).st_mode & 0o777, 0o600)
        self.assertEqual(guardian.load_config(self.root)['projects'][0]['path'], str(project))

    def test_config_rejects_relative_and_missing_paths(self):
        code, data = self.call('PUT', '/api/config', {'projects': [{'path': 'relative/dir'}]})
        self.assertEqual(code, 400)
        self.assertIn('absolute', data['error'])

        code, data = self.call('PUT', '/api/config', {'projects': [{'path': '/definitely/not/here'}]})
        self.assertEqual(code, 400)
        self.assertIn('does not exist', data['error'])

    def test_config_requires_json_content_type(self):
        code, data = self.call('PUT', '/api/config', {'projects': []}, ctype='text/plain')
        self.assertEqual(code, 400)
        self.assertIn('application/json', data['error'])

    def test_quota_live_disabled(self):
        code, data = self.call('GET', '/api/quota')
        self.assertEqual(code, 200)
        self.assertIn('disabled', data['error'])


if __name__ == '__main__':
    unittest.main()

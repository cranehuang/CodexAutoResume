#!/usr/bin/env python3
"""Stdlib-only web console for codex-auto-resume.

Shows the last scan snapshot, live quota, submission history, and edits the
project whitelist that guardian.py reads on every cycle.

Binds 127.0.0.1 by default. Reach it through an SSH tunnel; never expose it
publicly: the API can change which repositories get auto-resumed.
"""
import argparse
import datetime as dt
import fcntl
import json
import mimetypes
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import guardian

MAX_BODY = 64 * 1024
MAX_PROJECTS = 200


class Console:
    """Data + config access shared by all request handler threads."""

    def __init__(self, root, webui, socket, live=True, ttl=10):
        self.root = Path(root).expanduser()
        self.webui = Path(webui).expanduser().resolve()
        self.socket = socket
        self.live = live
        self.ttl = max(0, int(ttl))
        self._lock = threading.Lock()
        self._quota_cache = {'at': 0.0, 'data': None}

    def service_running(self):
        """The daemon holds an exclusive flock on <state>/lock for its lifetime."""
        try:
            fd = os.open(str(self.root / 'lock'), os.O_CREAT | os.O_RDWR, 0o600)
        except OSError:
            return None
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            except OSError:
                return None
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)

    def status(self):
        path = self.root / guardian.STATUS_NAME
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            return None, 'no scan yet'
        except Exception as exc:
            return None, f'{type(exc).__name__}: {exc}'
        if not isinstance(data, dict):
            return None, f'{guardian.STATUS_NAME} is not a JSON object'
        return data, None

    def attempts(self, limit=50):
        try:
            limit = max(1, min(int(limit), 500))
        except (TypeError, ValueError):
            limit = 50
        result = {'counts': {'submitted': 0, 'unknown': 0, 'submit_unknown': 0},
                  'total': 0, 'last_24h': 0, 'recent': []}
        db = self.root / 'attempts.sqlite3'
        if not db.exists():
            return result
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)).isoformat(timespec='seconds')
        conn = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=5)
        try:
            for state, n in conn.execute('SELECT state, COUNT(*) FROM attempts GROUP BY state'):
                result['counts'][state] = n
            result['total'] = conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]
            result['last_24h'] = conn.execute(
                'SELECT COUNT(*) FROM attempts WHERE claimed_at >= ?', (cutoff,)).fetchone()[0]
            rows = conn.execute(
                'SELECT thread_id, failed_turn_id, claimed_at, state, created_turn_id, details, title, '
                'cwd, project, project_label '
                'FROM attempts ORDER BY claimed_at DESC LIMIT ?', (limit,)).fetchall()
            result['recent'] = [{'thread_id': r[0], 'failed_turn_id': r[1], 'claimed_at': r[2],
                                 'state': r[3], 'created_turn_id': r[4], 'details': r[5],
                                 'title': r[6] or '', 'cwd': r[7] or '',
                                 'project': r[8] or '', 'project_label': r[9] or ''}
                                for r in rows]
        finally:
            conn.close()
        return result

    def overview(self):
        status, status_error = self.status()
        age = None
        if status and isinstance(status.get('updated_at'), str):
            try:
                age = max(0, int((dt.datetime.now(dt.timezone.utc)
                                  - dt.datetime.fromisoformat(status['updated_at'])).total_seconds()))
            except ValueError:
                age = None
        return {'now': guardian.utcnow(),
                'service': {'running': self.service_running(),
                            'mode': (status or {}).get('mode'),
                            'interval': (status or {}).get('interval')},
                'status': status,
                'status_age_sec': age,
                'status_error': status_error,
                'config': guardian.load_config(self.root),
                'attempts': self.attempts(50),
                'live_quota': self.live}

    def quota(self, force=False):
        if not self.live:
            return {'available': None, 'error': 'live quota probe disabled', 'fetched_at': guardian.utcnow()}
        with self._lock:
            fresh = time.monotonic() - self._quota_cache['at'] < self.ttl
            if not force and fresh and self._quota_cache['data'] is not None:
                return self._quota_cache['data']
            server = None
            try:
                server = guardian.Server(self.socket)
                ok, res = guardian.available(server)
                data = guardian.quota_dict(res)
                data.update({'available': ok, 'error': None, 'fetched_at': guardian.utcnow()})
            except Exception as exc:
                data = {'available': None, 'error': f'{type(exc).__name__}: {exc}',
                        'fetched_at': guardian.utcnow()}
            finally:
                if server is not None:
                    server.close()
            self._quota_cache = {'at': time.monotonic(), 'data': data}
            return data

    def update_config(self, payload):
        if not isinstance(payload, dict) or not isinstance(payload.get('projects'), list):
            raise ValueError('projects must be a list')
        projects, seen = [], set()
        for item in payload['projects']:
            if not isinstance(item, dict):
                raise ValueError('each project must be an object')
            raw = item.get('path')
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError('project path is required')
            path = os.path.expanduser(raw.strip())
            if not os.path.isabs(path):
                raise ValueError(f'project path must be absolute: {raw}')
            if not os.path.isdir(path):
                raise ValueError(f'project directory does not exist: {path}')
            real = os.path.realpath(path)
            if real in seen:
                continue
            seen.add(real)
            projects.append({'path': real,
                             'label': str(item.get('label') or '').strip()[:80],
                             'enabled': item.get('enabled', True) is not False})
            if len(projects) > MAX_PROJECTS:
                raise ValueError(f'too many projects (max {MAX_PROJECTS})')
        return guardian.save_config(self.root, {'projects': projects})


class Handler(BaseHTTPRequestHandler):
    console = None
    server_version = 'codex-auto-resume-web/1.0'
    sys_version = ''

    def log_request(self, code='-', size='-'):
        if self.path.startswith('/api/'):
            super().log_request(code, size)

    def _send(self, code, body, ctype='application/json; charset=utf-8', cache='no-store'):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode('utf-8')
        elif isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', cache)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, obj)

    def _error(self, code, message):
        self._json({'error': message}, code)

    def _static(self, path):
        rel = 'index.html' if path in ('', '/', '/index.html') else path.lstrip('/')
        base = self.console.webui
        target = (base / rel).resolve()
        if target != base and base not in target.parents:
            self._error(403, 'forbidden')
            return
        if not target.is_file():
            self._error(404, 'not found')
            return
        ctype = mimetypes.guess_type(str(target))[0] or 'application/octet-stream'
        if ctype.startswith('text/') or ctype == 'application/javascript':
            ctype += '; charset=utf-8'
        self._send(200, target.read_bytes(), ctype, cache='no-cache')

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == '/api/overview':
                self._json(self.console.overview())
            elif path == '/api/config':
                self._json(self.console.overview()['config'])
            elif path == '/api/attempts':
                self._json(self.console.attempts(parse_qs(parsed.query).get('limit', ['50'])[0]))
            elif path == '/api/quota':
                force = parse_qs(parsed.query).get('force', ['0'])[0] in ('1', 'true')
                self._json(self.console.quota(force=force))
            elif path == '/api/health':
                self._json({'ok': True, 'now': guardian.utcnow()})
            elif path.startswith('/api/'):
                self._error(404, 'unknown endpoint')
            else:
                self._static(path)
        except Exception as exc:
            self._error(500, f'{type(exc).__name__}: {exc}')

    def _read_json(self):
        ctype = (self.headers.get('Content-Type') or '').split(';')[0].strip().lower()
        if ctype != 'application/json':
            raise ValueError('Content-Type must be application/json')
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            length = 0
        if length <= 0:
            raise ValueError('empty request body')
        if length > MAX_BODY:
            raise ValueError('request body too large')
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ValueError(f'invalid JSON: {exc}')

    def do_PUT(self):
        if urlparse(self.path).path != '/api/config':
            self._error(404, 'unknown endpoint')
            return
        try:
            payload = self._read_json()
            config = self.console.update_config(payload)
        except ValueError as exc:
            self._error(400, str(exc))
            return
        except Exception as exc:
            self._error(500, f'{type(exc).__name__}: {exc}')
            return
        self._json(config)


def make_server(console, host='127.0.0.1', port=8787):
    handler = type('BoundHandler', (Handler,), {'console': console})
    return ThreadingHTTPServer((host, port), handler)


def main():
    p = argparse.ArgumentParser(description='Codex auto resume web console (metrics + project whitelist)')
    p.add_argument('--state-dir', default=str(guardian.STATE_DIR))
    p.add_argument('--socket', default=guardian.DEFAULT_SOCKET)
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int, default=8787)
    p.add_argument('--webui', default=str(Path(__file__).resolve().parent / 'webui'))
    p.add_argument('--no-live', action='store_true', help='serve only the status snapshot, no live socket probe')
    p.add_argument('--cache-ttl', type=int, default=10, help='seconds to cache the live quota; default 10')
    args = p.parse_args()

    root = Path(args.state_dir).expanduser()
    if not (root / guardian.STATUS_NAME).exists():
        print(f'warning: no {guardian.STATUS_NAME} in {root}; is the guardian running?', file=sys.stderr)
    console = Console(root, args.webui, args.socket, live=not args.no_live, ttl=args.cache_ttl)
    httpd = make_server(console, args.host, args.port)
    print(f'codex-auto-resume console: http://{args.host}:{args.port}/  (state: {root})', flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == '__main__':
    main()

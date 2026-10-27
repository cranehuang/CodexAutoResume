#!/usr/bin/env python3
"""Codex App Server: dynamic, first-start, all-eligible usage-limit recovery.

Uses an existing shared App Server Unix WebSocket socket. No new Codex process.
State: SQLite, at-most-once submission attempt per (thread, failed turn).
Defaults to a dry run; pass --send to enable writes.
"""
import argparse
import datetime as dt
import fcntl
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
import time

from probe_transport import Client, TransportError

HOME = Path.home()
STATE_DIR = Path(os.environ.get('CODEX_GUARDIAN_HOME', str(HOME / '.local/state/codex-auto-resume-all')))
DEFAULT_SOCKET = str(Path(os.environ.get('CODEX_HOME', str(HOME / '.codex'))) / 'app-server-control/app-server-control.sock')
CONFIG_NAME = 'config.json'
STATUS_NAME = 'status.json'
CONTINUE_TEXT = ('继续刚才因为 Codex 使用额度耗尽而中断的任务。请先检查本会话已完成的工作与当前代码状态，'
                 '避免重复执行有副作用的操作。仅继续尚未完成的目标，完成后运行合适的测试并汇报。'
                 '遇到权限审批、风险操作或不确定性时请停止并向我说明。')
log = logging.getLogger('guardian')


def utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


def db_open(path):
    conn = sqlite3.connect(str(path), timeout=15)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=15000')
    conn.execute('''CREATE TABLE IF NOT EXISTS attempts (
        thread_id TEXT NOT NULL, failed_turn_id TEXT NOT NULL,
        claimed_at TEXT NOT NULL, state TEXT NOT NULL,
        created_turn_id TEXT, details TEXT,
        PRIMARY KEY (thread_id, failed_turn_id)
    )''')
    # Older databases predate the snapshot columns; add them without losing history.
    columns = {row[1] for row in conn.execute('PRAGMA table_info(attempts)')}
    for name in ('title', 'cwd', 'project', 'project_label'):
        if name not in columns:
            conn.execute(f'ALTER TABLE attempts ADD COLUMN {name} TEXT')
    conn.commit()
    return conn


def get_attempt(conn, tid, turn):
    return conn.execute('SELECT state FROM attempts WHERE thread_id=? AND failed_turn_id=?', (tid, turn)).fetchone()


def claim(conn, tid, turn, title=None, cwd=None, project=None, project_label=None):
    try:
        with conn:
            conn.execute('INSERT INTO attempts (thread_id, failed_turn_id, claimed_at, state, title, cwd, '
                         'project, project_label) VALUES (?,?,?,?,?,?,?,?)',
                         (tid, turn, utcnow(), 'submit_unknown', title, cwd, project, project_label))
        return True
    except sqlite3.IntegrityError:
        return False


def set_state(conn, tid, failed_turn, state, new_turn=None, details=None):
    with conn:
        conn.execute('UPDATE attempts SET state=?, created_turn_id=?, details=? WHERE thread_id=? AND failed_turn_id=?',
                     (state, new_turn, (details or '')[:500], tid, failed_turn))


class Server:
    CLIENT_INFO = {'name':'codex_auto_resume_all',
                   'title':'Codex automatic all-thread recovery', 'version':'1.0.0'}

    def __init__(self, sock):
        self.sock = sock
        self.client = None
        self.n = 0
        self._connect()

    def _connect(self):
        client = Client(self.sock, timeout=14)
        self.client = client
        try:
            self.call('initialize', {'clientInfo': self.CLIENT_INFO})
            client.initialized()
        except Exception:
            try:
                client.close()
            except Exception:
                pass
            self.client = None
            raise

    def reconnect(self):
        """Replace a poisoned connection; callers keep the same Server object."""
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass
        self.client = None
        self._connect()

    def call(self, name, params=None):
        if self.client is None:
            raise TransportError('Not connected')
        self.n += 1
        return self.client.rpc(self.n, name, params)

    def close(self):
        if self.client is not None:
            self.client.close()
            self.client = None


def all_threads(server):
    """Never silently truncate pagination: failing a page aborts the scan."""
    cursor, seen_cursors, seen_threads = None, set(), set()
    while True:
        params = {'limit':100, 'sortKey':'recency_at'}
        if cursor:
            params['cursor'] = cursor
        response = server.call('thread/list', params)
        if not isinstance(response, dict) or not isinstance(response.get('data'), list):
            raise RuntimeError('Unexpected thread/list response; stopping scan')
        for t in response['data']:
            if isinstance(t, dict) and t.get('id') and t['id'] not in seen_threads:
                seen_threads.add(t['id'])
                yield t
        new = response.get('nextCursor')
        if not new:
            return
        if new in seen_cursors:
            raise RuntimeError('Pagination cursor repeated; stopping scan')
        seen_cursors.add(new)
        cursor = new


def latest_turn(server, tid):
    response = server.call('thread/turns/list', {'threadId':tid, 'limit':1,
                      'sortDirection':'desc', 'itemsView':'notLoaded'})
    data = response.get('data') if isinstance(response, dict) else None
    if not isinstance(data, list):
        raise RuntimeError('Unexpected thread/turns/list response')
    return data[0] if data else None


def is_quota_failed(turn):
    if not isinstance(turn, dict) or turn.get('status') != 'failed' or not turn.get('id'):
        return False
    error = turn.get('error') or {}
    return isinstance(error, dict) and error.get('codexErrorInfo') == 'usageLimitExceeded'


def available(server):
    res = server.call('account/rateLimits/read')
    if not isinstance(res, dict):
        raise RuntimeError('Unexpected account/rateLimits/read response')
    return res.get('ordinaryUsageAllowed') is True, res


def summary_quota(res):
    lim = res.get('rateLimits') or {}
    p, s = lim.get('primary') or {}, lim.get('secondary') or {}
    def fmt(w):
        at = w.get('resetsAt')
        stamp = dt.datetime.fromtimestamp(at, dt.timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M %Z') if isinstance(at, (int,float)) else '?'
        return f'{w.get("usedPercent", "?")}% until {stamp}'
    return f'ordinaryUsageAllowed={res.get("ordinaryUsageAllowed")} primary={fmt(p)} secondary={fmt(s)}'


def quota_dict(res):
    """Structured quota for status.json / the web console (no account details)."""
    lim = res.get('rateLimits') or {}
    def window(w):
        if not isinstance(w, dict):
            return None
        at = w.get('resetsAt')
        reset = None
        if isinstance(at, (int, float)):
            reset = dt.datetime.fromtimestamp(at, dt.timezone.utc).astimezone().isoformat(timespec='seconds')
        return {'usedPercent': w.get('usedPercent'),
                'windowDurationMins': w.get('windowDurationMins'),
                'resetsAt': reset}
    return {'ordinaryUsageAllowed': res.get('ordinaryUsageAllowed') is True,
            'rateLimitReachedType': lim.get('rateLimitReachedType'),
            'primary': window(lim.get('primary')),
            'secondary': window(lim.get('secondary'))}


def norm_path(path):
    try:
        return os.path.realpath(os.path.expanduser(str(path))).rstrip(os.sep) or os.sep
    except Exception:
        return str(path)


def default_config():
    return {'version': 1, 'projects': []}


def clean_projects(raw):
    out = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            path = item.get('path')
            if not isinstance(path, str) or not path.strip():
                continue
            out.append({'path': norm_path(path),
                        'label': str(item.get('label') or '').strip()[:80],
                        'enabled': item.get('enabled', True) is not False})
    return out


def load_config(root):
    """Missing/invalid config means monitor-all, matching the original behaviour."""
    path = Path(root) / CONFIG_NAME
    try:
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            raise ValueError('config root must be a JSON object')
    except FileNotFoundError:
        return default_config()
    except Exception as exc:
        log.warning('CONFIG unreadable, monitoring all threads: %s', exc)
        return default_config()
    cfg = default_config()
    cfg.update({k: v for k, v in raw.items() if k != 'projects'})
    cfg['projects'] = clean_projects(raw.get('projects'))
    return cfg


def save_config(root, cfg):
    """Atomic write (0600) so a scanning guardian never reads a partial file."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = {'version': 1, 'projects': clean_projects((cfg or {}).get('projects'))}
    tmp = root / (CONFIG_NAME + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    os.chmod(tmp, 0o600)
    os.replace(tmp, root / CONFIG_NAME)
    return data


def match_project(cwd, cfg):
    """Return (monitored, project). With no enabled project every thread is monitored."""
    projects = [p for p in (cfg or {}).get('projects', []) if p.get('enabled', True)]
    if not projects:
        return True, None
    if not cwd:
        return False, None
    target = norm_path(cwd)
    best, best_len = None, -1
    for p in projects:
        root = norm_path(p['path'])
        if target == root or target.startswith(root + os.sep):
            if len(root) > best_len:
                best, best_len = p, len(root)
    return best is not None, best


def infer_project(cwd):
    """Best-effort project root: nearest git repo below home, else the directory itself."""
    if not cwd:
        return None
    try:
        path = Path(norm_path(cwd))
        home = Path(norm_path(str(HOME)))
    except Exception:
        return None
    for parent in (path, *path.parents):
        # Never mistake a dotfiles repo at ~ for the project.
        if parent != home and (parent / '.git').exists():
            return str(parent)
        if parent == home:
            break
    return str(path)


def project_of(cwd, cfg):
    """(monitored, project entry, project path) with inference for unconfigured dirs."""
    tracked, project = match_project(cwd, cfg)
    path = project['path'] if project else infer_project(cwd)
    return tracked, project, path


def write_status(path, stats):
    path = Path(path)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(stats, ensure_ascii=False, indent=2) + '\n')
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def record_failure(status_path, exc, args=None):
    """Keep the last good scan data, but mark the socket/scan as failed."""
    try:
        stats = json.loads(Path(status_path).read_text())
        if not isinstance(stats, dict):
            stats = {}
    except Exception:
        stats = {}
    stats.update({'updated_at': utcnow(), 'socket_ok': False, 'waiting': 'scan_failed',
                  'last_error': f'{type(exc).__name__}: {exc}'})
    if args is not None:
        stats.setdefault('mode', 'send' if args.send else 'dry-run')
        stats.setdefault('interval', args.interval)
        stats.setdefault('socket', args.socket)
    try:
        write_status(status_path, stats)
    except Exception as write_exc:
        log.error('STATUS write failed: %s', write_exc)


def thread_status(t):
    s = t.get('status')
    return s.get('type') if isinstance(s, dict) else None


def run_scan(server, conn, send, config=None, status_path=None, interval=None, socket=None):
    """One full scan. A pagination/transport error aborts the whole cycle.

    config/status_path are optional so the README's simple probe usage and the
    tests keep working exactly as before.
    """
    cfg = config if config is not None else default_config()
    active = [p for p in cfg.get('projects', []) if p.get('enabled', True)]
    stats = {
        'updated_at': utcnow(),
        'mode': 'send' if send else 'dry-run',
        'interval': interval,
        'socket': socket,
        'socket_ok': True,
        'monitor_all': not active,
        'scan': {'threads': 0, 'candidates': 0, 'unmonitored': 0,
                 'errors': 0, 'duplicates': 0, 'reconnects': 0, 'duration_ms': None},
        'quota': None,
        'eligible': [],
        'unmonitored': [],
        'submitted': [],
        'waiting': None,
        'last_error': None,
    }
    started = time.monotonic()
    try:
        _run_scan(server, conn, send, cfg, stats)
    except Exception as exc:
        stats['last_error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        stats['scan']['duration_ms'] = int((time.monotonic() - started) * 1000)
        stats['updated_at'] = utcnow()
        if status_path:
            try:
                write_status(status_path, stats)
            except Exception as exc:
                log.error('STATUS write failed: %s', exc)


def _run_scan(server, conn, send, cfg, stats):
    eligible, unmonitored = [], []
    total = errors = duplicates = 0
    # Backfill snapshot fields for attempts recorded before those columns existed.
    pending = {row[0] for row in conn.execute(
        "SELECT DISTINCT thread_id FROM attempts WHERE title IS NULL OR title='' "
        "OR cwd IS NULL OR cwd='' OR project IS NULL OR project=''")}
    found = {}
    # First collect all candidates (first boot and every scan); do not use 'updatedAt > installation_time'.
    for t in all_threads(server):
        total += 1
        tid, status = t['id'], thread_status(t)
        if tid in pending:
            title = t.get('name') or t.get('title') or ''
            cwd = t.get('cwd')
            _, project, project_path = project_of(cwd, cfg)
            if title or cwd:
                found[tid] = (title, cwd, project_path,
                              (project.get('label') or '') if project else '')
        if status not in ('idle', 'systemError', 'notLoaded'):
            continue
        try:
            last = latest_turn(server, tid)
        except Exception as exc:
            log.warning('SKIP %s: cannot read last turn: %s', tid, exc)
            errors += 1
            continue
        if not is_quota_failed(last):
            continue
        failed_turn = last['id']
        if get_attempt(conn, tid, failed_turn):
            duplicates += 1
            log.info('SKIP duplicate %s / %s', tid, failed_turn)
            continue
        cwd = t.get('cwd')
        tracked, project, project_path = project_of(cwd, cfg)
        entry = {'thread_id': tid,
                 'failed_turn_id': failed_turn,
                 'cwd': cwd,
                 'title': t.get('name') or t.get('title') or '',
                 'status': status,
                 'project': project_path,
                 'project_label': (project.get('label') or '') if project else ''}
        (eligible if tracked else unmonitored).append(entry)
    if found:
        with conn:
            conn.executemany(
                "UPDATE attempts SET "
                "title=COALESCE(NULLIF(title,''), ?), "
                "cwd=COALESCE(NULLIF(cwd,''), ?), "
                "project=COALESCE(NULLIF(project,''), ?), "
                "project_label=COALESCE(NULLIF(project_label,''), ?) "
                "WHERE thread_id=?",
                [(title, cwd, project, label, tid)
                 for tid, (title, cwd, project, label) in found.items()])
    stats['scan'].update(threads=total, candidates=len(eligible), unmonitored=len(unmonitored),
                         errors=errors, duplicates=duplicates)
    stats['eligible'] = eligible
    stats['unmonitored'] = unmonitored
    log.info('SCAN threads=%d candidates=%d unmonitored=%d duplicates=%d errors=%d',
             total, len(eligible), len(unmonitored), duplicates, errors)

    quota_ok = False
    try:
        quota_ok, limits = available(server)
        stats['quota'] = quota_dict(limits)
        log.info('QUOTA %s', summary_quota(limits))
    except Exception as exc:
        stats['last_error'] = f'quota: {exc}'
        log.error('Cannot confirm quota: %s', exc)

    if not eligible:
        stats['waiting'] = 'idle'
        return
    if stats['quota'] is None:
        stats['waiting'] = 'quota_unknown'
        log.info('WAIT quota blocked/unknown; %d candidates deferred', len(eligible))
        return
    if not quota_ok:
        stats['waiting'] = 'quota_blocked'
        log.info('WAIT quota blocked/unknown; %d candidates deferred', len(eligible))
        return
    if not send:
        stats['waiting'] = 'dry_run'
        log.info('DRY-RUN quota available; %d candidates ready (no writes)', len(eligible))
        for e in eligible:
            log.info('DRY-RUN eligible thread=%s failed_turn=%s status=%s cwd=%s',
                     e['thread_id'], e['failed_turn_id'], e['status'], e['cwd'])
        return

    # User requested ALL, not one-per-scan. Dispatch each independently; no hard-coded count cap.
    # Distinct threads may share a working directory; the user explicitly requested all.
    # IMPORTANT: parallel edits on one directory can still conflict (see README).
    stats['waiting'] = 'sent'
    submitted = stats['submitted']
    for e in eligible:
        tid, failed = e['thread_id'], e['failed_turn_id']
        reconnect = False
        try:
            ok, lim = available(server)
            stats['quota'] = quota_dict(lim)
            if not ok:
                stats['waiting'] = 'quota_blocked'
                log.info('QUOTA exhausted again, remaining candidates wait for next scan')
                break
            info = server.call('thread/read', {'threadId':tid, 'includeTurns':False}).get('thread') or {}
            if thread_status(info) not in ('idle','systemError','notLoaded'):
                log.info('SKIP %s now active', tid)
                continue
            now_last = latest_turn(server, tid)
            if not is_quota_failed(now_last) or now_last['id'] != failed:
                log.info('SKIP %s changed since scan', tid)
                continue
            if thread_status(info) == 'notLoaded':
                # Keep the original thread ID; no fork. Loading is a stateful operation.
                server.call('thread/resume', {'threadId':tid})
                now_last = latest_turn(server, tid)
                if not is_quota_failed(now_last) or now_last['id'] != failed:
                    log.info('SKIP %s changed after thread/resume', tid)
                    continue
            # Persist an at-most-once claim BEFORE sending. On timeouts, never re-send blindly.
            if not claim(conn, tid, failed, e.get('title'), e.get('cwd'),
                         e.get('project'), e.get('project_label')):
                continue
            try:
                result = server.call('turn/start', {
                    'threadId':tid,
                    'input':[{'type':'text','text':CONTINUE_TEXT}],
                })
                new_turn = ((result or {}).get('turn') or {}).get('id')
                if not new_turn:
                    # Ambiguous response: request MAY have succeeded.
                    log.error('AMBIGUOUS %s: turn/start returned no turn id', tid)
                    set_state(conn, tid, failed, 'unknown', details='turn/start response missing turn.id')
                    stats['last_error'] = f'ambiguous turn/start for {tid}'
                else:
                    set_state(conn, tid, failed, 'submitted', new_turn=new_turn)
                    submitted.append({'thread_id': tid, 'failed_turn_id': failed, 'new_turn_id': new_turn})
                    log.info('SUBMITTED thread=%s old=%s new=%s', tid, failed, new_turn)
            except TransportError as exc:
                # Claim already exists: record unknown, never retry, but drop the connection.
                set_state(conn, tid, failed, 'unknown', details=f'transport: {exc}')
                log.error('UNKNOWN submission %s (transport, no automatic retry): %s', tid, exc)
                stats['last_error'] = f'unknown submission for {tid}: {exc}'
                reconnect = True
            except Exception as exc:
                set_state(conn, tid, failed, 'unknown', details=str(exc))
                log.error('UNKNOWN submission %s (no automatic retry): %s', tid, exc)
                stats['last_error'] = f'unknown submission for {tid}: {exc}'
        except TransportError as exc:
            # Nothing claimed for this candidate: safe to reconnect and continue with the next one.
            errors += 1
            stats['scan']['errors'] = errors
            reconnect = True
            log.error('TRANSPORT candidate %s failed: %s', tid, exc)
        except Exception as exc:
            errors += 1
            stats['scan']['errors'] = errors
            log.error('SKIP candidate %s: %s', tid, exc)
        if reconnect:
            stats['scan']['reconnects'] += 1
            try:
                server.reconnect()
                log.info('RECONNECTED after transport error; continuing with next candidate')
            except Exception as exc:
                log.error('RECONNECT failed, deferring remaining candidates: %s', exc)
                stats['waiting'] = 'transport_error'
                stats['last_error'] = f'reconnect failed: {exc}'
                break
    log.info('SCAN COMPLETE submitted=%d reconnects=%d',
             len(submitted), stats['scan']['reconnects'])


def main():
    p = argparse.ArgumentParser(description='Dynamic all-thread Codex quota recovery on existing App Server')
    p.add_argument('--socket', default=DEFAULT_SOCKET)
    p.add_argument('--state-dir', default=str(STATE_DIR))
    p.add_argument('--daemon', action='store_true', help='scan immediately then repeat')
    p.add_argument('--interval', type=int, default=120, help='seconds between scans; default 120')
    p.add_argument('--send', action='store_true', help='ENABLE actual turn/start messages; omit for dry run')
    p.add_argument('--status', action='store_true', help='show local submission records')
    args = p.parse_args()
    if args.interval < 15:
        p.error('--interval must be at least 15 seconds')
    root = Path(args.state_dir).expanduser()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                        stream=sys.stdout)
    conn = db_open(root / 'attempts.sqlite3')
    if args.status:
        for row in conn.execute('SELECT thread_id,failed_turn_id,claimed_at,state,created_turn_id FROM attempts ORDER BY claimed_at DESC LIMIT 100'):
            print(' | '.join(str(x) for x in row))
        return
    lock_fd = os.open(str(root / 'lock'), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('Another guardian instance is running; refusing duplicate instance')
    log.info('START mode=%s first scan immediately, all pages, all eligible threads',
             'SEND' if args.send else 'DRY-RUN')
    status_path = root / STATUS_NAME
    while True:
        server = None
        try:
            server = Server(args.socket)
            run_scan(server, conn, args.send, config=load_config(root), status_path=status_path,
                     interval=args.interval, socket=args.socket)
        except Exception as exc:
            log.error('Scan failed; no writes this cycle: %s', exc)
            record_failure(status_path, exc, args)
        finally:
            if server is not None:
                server.close()
        if not args.daemon:
            break
        time.sleep(args.interval)

if __name__ == '__main__':
    main()

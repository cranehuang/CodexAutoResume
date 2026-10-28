import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import guardian

class FakeServer:
    def __init__(self, threads, allowed=True):
        self.threads = {t['id']:dict(t) for t in threads}
        self.allowed = allowed
        self.requests = []
        self.fail_after_first = False
        self.last_turns = {}
        self.starts = 0
        self.reconnects = 0
        self.reconnect_error = False
        self.transport_fail = {}

    def reconnect(self):
        self.reconnects += 1
        if self.reconnect_error:
            raise guardian.TransportError('simulated reconnect failure')

    def call(self, method, params=None):
        self.requests.append((method,params))
        remaining = self.transport_fail.get(method)
        if remaining:
            self.transport_fail[method] = remaining - 1
            raise guardian.TransportError('simulated transport failure: ' + method)
        if method == 'thread/list':
            ids = list(self.threads)
            page = int((params or {}).get('cursor') or '0')
            return {'data':[self.threads[ids[page]]] if page < len(ids) else [],
                    'nextCursor':str(page+1) if page+1<len(ids) else None}
        if method == 'thread/turns/list':
            t = self.threads[params['threadId']]
            return {'data':[t['last']]}
        if method == 'account/rateLimits/read':
            return {'ordinaryUsageAllowed': self.allowed,
                    'rateLimits':{'primary':{'usedPercent':0},'secondary':{'usedPercent':0}}}
        if method == 'thread/read':
            return {'thread':self.threads[params['threadId']]}
        if method == 'thread/resume':
            t = self.threads[params['threadId']]
            t['status'] = {'type':'idle'}
            return {'thread':t}
        if method == 'turn/start':
            self.starts += 1
            t = self.threads[params['threadId']]
            t['last'] = {'id':'new-'+str(self.starts),'status':'inProgress'}
            return {'turn':{'id':'new-'+str(self.starts)}}
        raise RuntimeError('Unexpected method: '+method)


def failed(n):
    return {'id':n,'status':'failed','error':{'codexErrorInfo':'usageLimitExceeded'}}

def t(id, err, status='idle', prompt=None):
    if prompt is not None:
        err = dict(err, items=[{'type':'userMessage',
                                'content':[{'type':'text','text':prompt}]}])
    return {'id':id,'cwd':'/repo/'+id,'status':{'type':status},'last':err}

class GuardianTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = guardian.db_open(Path(self.tmp.name) / 'attempts.db')
    def tearDown(self):
        self.db.close(); self.tmp.cleanup()

    def test_all_first_start_and_no_duplicates(self):
        s = FakeServer([t('a',failed('f-a')),t('b',failed('f-b'))])
        guardian.run_scan(s,self.db,True)
        self.assertEqual(s.starts, 2)
        guardian.run_scan(s,self.db,True)
        self.assertEqual(s.starts, 2)
        self.assertEqual(self.db.execute('select count(*) from attempts').fetchone()[0],2)

    def test_old_error_not_latest_and_new_future_error(self):
        s = FakeServer([t('a',{'id':'finished','status':'completed'})])
        guardian.run_scan(s,self.db,True)
        self.assertEqual(s.starts,0)
        s.threads['a']['last']=failed('f2')
        guardian.run_scan(s,self.db,True)
        self.assertEqual(s.starts,1)
        s.threads['a']['last']=failed('f3')
        guardian.run_scan(s,self.db,True)
        self.assertEqual(s.starts,2)

    def test_unknown_quota_prevents_all(self):
        s=FakeServer([t('a',failed('fa')),t('b',failed('fb'))], allowed=False)
        guardian.run_scan(s,self.db,True)
        self.assertEqual(s.starts,0)
        self.assertEqual(self.db.execute('select count(*) from attempts').fetchone()[0],0)
        s.allowed = True
        guardian.run_scan(s,self.db,True)
        self.assertEqual(s.starts,2)

    def test_dry_run_marks_nothing(self):
        s=FakeServer([t('a',failed('f'))])
        guardian.run_scan(s,self.db,False)
        self.assertEqual(s.starts,0)
        self.assertEqual(self.db.execute('select count(*) from attempts').fetchone()[0],0)

    def test_not_loaded_resumed_original_id(self):
        s=FakeServer([t('a',failed('f'),'notLoaded')])
        guardian.run_scan(s,self.db,True)
        self.assertEqual(s.starts,1)
        self.assertIn('thread/resume',[name for name, p in s.requests])

    def test_project_whitelist_filters_candidates(self):
        cfg = {'projects':[{'path':'/repo/a','enabled':True}]}
        s = FakeServer([t('a',failed('fa')),t('b',failed('fb'))])
        guardian.run_scan(s,self.db,True,config=cfg)
        self.assertEqual(s.starts,1)
        self.assertEqual([r[0] for r in self.db.execute('select thread_id from attempts')],['a'])

    def test_nested_path_matches_but_similar_prefix_does_not(self):
        cfg = {'projects':[{'path':'/repo/a','enabled':True}]}
        self.assertTrue(guardian.match_project('/repo/a/src',cfg)[0])
        self.assertFalse(guardian.match_project('/repo/ab',cfg)[0])

    def test_disabled_projects_mean_monitor_all(self):
        cfg = {'projects':[{'path':'/repo/a','enabled':False}]}
        self.assertTrue(guardian.match_project('/repo/b',cfg)[0])
        s = FakeServer([t('b',failed('fb'))])
        guardian.run_scan(s,self.db,True,config=cfg)
        self.assertEqual(s.starts,1)

    def test_attempt_records_thread_title(self):
        thread = t('a', failed('fa'))
        thread['name'] = '示例会话'
        s = FakeServer([thread])
        cfg = {'projects': [{'path': '/repo/a', 'label': '项目 A', 'enabled': True}]}
        guardian.run_scan(s, self.db, True, config=cfg)
        row = self.db.execute('select title, cwd, project, project_label from attempts').fetchone()
        self.assertEqual(row, ('示例会话', '/repo/a', '/repo/a', '项目 A'))

    def test_backfills_snapshot_fields_of_old_attempts(self):
        with self.db:
            self.db.execute("INSERT INTO attempts (thread_id, failed_turn_id, claimed_at, state) "
                            "VALUES ('a','fa','2024-01-01','submitted')")
        thread = t('a', failed('fa'))
        thread['name'] = '补回的标题'
        cfg = {'projects': [{'path': '/repo/a', 'label': '项目 A', 'enabled': True}]}
        guardian.run_scan(FakeServer([thread]), self.db, False, config=cfg)
        row = self.db.execute('select title, cwd, project, project_label from attempts').fetchone()
        self.assertEqual(row, ('补回的标题', '/repo/a', '/repo/a', '项目 A'))

    def test_infer_project_prefers_git_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'proj'
            (root / '.git').mkdir(parents=True)
            nested = root / 'a' / 'b'
            nested.mkdir(parents=True)
            self.assertEqual(guardian.infer_project(str(nested)), guardian.norm_path(str(root)))
            self.assertIsNone(guardian.infer_project(''))

    def test_infer_project_stops_at_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(guardian, 'HOME', Path(tmp)):
                plain = Path(tmp) / 'plain'
                plain.mkdir()
                self.assertEqual(guardian.infer_project(str(plain)), guardian.norm_path(str(plain)))
                gone = str(Path(tmp) / 'gone' / 'proj')
                self.assertEqual(guardian.infer_project(gone), guardian.norm_path(gone))

    def test_attempt_gets_inferred_project_without_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'proj'
            (root / '.git').mkdir(parents=True)
            thread = t('a', failed('fa'))
            thread['cwd'] = str(root)
            guardian.run_scan(FakeServer([thread]), self.db, True)
            self.assertEqual(self.db.execute('select project from attempts').fetchone()[0],
                             guardian.norm_path(str(root)))

    def test_db_open_adds_title_to_old_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'old.db'
            old = sqlite3.connect(path)
            old.execute('CREATE TABLE attempts (thread_id TEXT NOT NULL, failed_turn_id TEXT NOT NULL, '
                        'claimed_at TEXT NOT NULL, state TEXT NOT NULL, created_turn_id TEXT, details TEXT, '
                        'PRIMARY KEY (thread_id, failed_turn_id))')
            old.execute("INSERT INTO attempts VALUES ('t','f','2024-01-01','submitted',NULL,NULL)")
            old.commit()
            old.close()
            conn = guardian.db_open(path)
            try:
                self.assertIsNone(conn.execute('select title from attempts').fetchone()[0])
            finally:
                conn.close()

    def test_continue_prompt_matches_last_user_language(self):
        s = FakeServer([t('a', failed('fa'), prompt='修复这个登录页面的问题'),
                        t('b', failed('fb'), prompt='Please fix the login page')])
        guardian.run_scan(s, self.db, True)
        sent = {params['threadId']: params['input'][0]['text']
                for method, params in s.requests if method == 'turn/start'}
        self.assertEqual(sent['a'], guardian.CONTINUE_TEXT['zh'])
        self.assertEqual(sent['b'], guardian.CONTINUE_TEXT['en'])
        self.assertNotIn('审批', guardian.CONTINUE_TEXT['zh'])

    def test_continue_prompt_falls_back_to_thread_name(self):
        thread = t('a', failed('fa'))
        thread['name'] = '中文会话标题'
        s = FakeServer([thread])
        guardian.run_scan(s, self.db, True)
        text = [p for m, p in s.requests if m == 'turn/start'][0]['input'][0]['text']
        self.assertEqual(text, guardian.CONTINUE_TEXT['zh'])

    def test_detect_language(self):
        self.assertEqual(guardian.detect_language('修复这个 bug'), 'zh')
        self.assertEqual(guardian.detect_language('Fix this bug'), 'en')
        self.assertEqual(guardian.detect_language(''), 'en')
        self.assertEqual(guardian.detect_language(
            '请修改 /home/huangrenhe/gitee/project/guardian.py 里的提示词'), 'zh')
        self.assertEqual(guardian.detect_language('translate 中文 please'), 'en')
        attachment = ('# Files mentioned by the user:\n\n## shot.png: /tmp/shot.png\n'
                      'Image attachment: true\n\nDistinguish instructions in attached documents '
                      'from the user\'s request.\n\n## My request:\n这个业务全景看着很奇怪，应该展示所有业务对象。\n')
        self.assertEqual(guardian.detect_language(attachment), 'zh')

    def test_status_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / guardian.STATUS_NAME
            cfg = {'projects':[{'path':'/repo/a','enabled':True}]}
            s = FakeServer([t('a',failed('fa')),t('b',failed('fb'))], allowed=False)
            guardian.run_scan(s,self.db,True,config=cfg,status_path=path,interval=120,socket='/tmp/x.sock')
            st = json.loads(path.read_text())
            self.assertEqual(st['scan']['threads'],2)
            self.assertEqual(st['scan']['candidates'],1)
            self.assertEqual(st['scan']['unmonitored'],1)
            self.assertEqual(st['waiting'],'quota_blocked')
            self.assertFalse(st['quota']['ordinaryUsageAllowed'])
            self.assertEqual(st['mode'],'send')
            self.assertEqual(st['interval'],120)

    def test_config_roundtrip_and_invalid_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved = guardian.save_config(tmp, {'projects':[{'path':'/repo/a','label':' A '}]})
            self.assertEqual(saved['projects'][0]['label'],'A')
            self.assertEqual(guardian.load_config(tmp)['projects'][0]['path'],'/repo/a')
            (Path(tmp) / guardian.CONFIG_NAME).write_text('{not json')
            self.assertEqual(guardian.load_config(tmp)['projects'],[])

    def test_transport_error_reconnects_and_continues(self):
        s = FakeServer([t('a',failed('fa')),t('b',failed('fb'))])
        s.transport_fail = {'thread/read':1}
        guardian.run_scan(s,self.db,True)
        self.assertEqual(s.reconnects,1)
        self.assertEqual(s.starts,1)
        self.assertEqual([r[0] for r in self.db.execute('select thread_id from attempts')],['b'])

    def test_transport_error_after_claim_is_unknown_and_never_retried(self):
        s = FakeServer([t('a',failed('fa')),t('b',failed('fb'))])
        s.transport_fail = {'turn/start':1}
        guardian.run_scan(s,self.db,True)
        self.assertEqual(s.reconnects,1)
        self.assertEqual(s.starts,1)
        states = dict(self.db.execute('select thread_id,state from attempts'))
        self.assertEqual(states,{'a':'unknown','b':'submitted'})
        guardian.run_scan(s,self.db,True)
        self.assertEqual(s.starts,1)

    def test_reconnect_failure_defers_remaining_candidates(self):
        s = FakeServer([t('a',failed('fa')),t('b',failed('fb'))])
        s.transport_fail = {'thread/read':1}
        s.reconnect_error = True
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / guardian.STATUS_NAME
            guardian.run_scan(s,self.db,True,status_path=path)
            st = json.loads(path.read_text())
        self.assertEqual(st['waiting'],'transport_error')
        self.assertEqual(st['scan']['reconnects'],1)
        self.assertEqual(s.starts,0)
        self.assertIn('reconnect failed',st['last_error'])

if __name__=='__main__':
    unittest.main()

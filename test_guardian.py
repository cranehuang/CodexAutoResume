import json
import tempfile
import unittest
from pathlib import Path
import guardian

class FakeServer:
    def __init__(self, threads, allowed=True):
        self.threads = {t['id']:dict(t) for t in threads}
        self.allowed = allowed
        self.requests = []
        self.fail_after_first = False
        self.last_turns = {}
        self.starts = 0

    def call(self, method, params=None):
        self.requests.append((method,params))
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

def t(id, err, status='idle'):
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

if __name__=='__main__':
    unittest.main()

import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from backend import DashboardDB,DashboardService
from tests.test_review_regressions import session,turn,usage,write_lines
class UpgradeTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'sessions';self.root.mkdir();self.path=self.root/'a.jsonl'
  write_lines(self.path,[session(),turn('test'),usage('thread-a','r1',100),usage('thread-a','r2',200)])
  self.db=DashboardDB(Path(self.tmp.name)/'cache.db',roots=[self.root],timezone_name='UTC');self.db.scan()
 def tearDown(self):
  self.db.close();self.tmp.cleanup()
 def test_incomplete_enumeration_preserves_cache(self):
  def walk(*args,**kwargs):
   kwargs['onerror'](PermissionError('blocked'));return iter([])
  with patch('backend.os.walk',walk): result=self.db.scan()
  self.assertEqual(result.unavailable_roots,1);self.assertEqual(self.db.dashboard(0)['summary']['total_tokens'],300)
 def test_partial_read_preserves_and_retries(self):
  original=self.db.parser._iter_json
  def broken(path):
   for n,event in original(path):
    if n>3:
     self.db.parser._read_failed=True;self.db.parser._parse_errors+=1;return
    yield n,event
  with self.path.open('a') as f:f.write('\n')
  service=DashboardService(self.db)
  with patch.object(self.db.parser,'_iter_json',broken): service.refresh()
  self.assertEqual(self.db.dashboard(0)['summary']['total_tokens'],300)
  self.assertEqual(service.health()['scan']['state'],'partial')
  self.assertEqual(service.refresh().files_scanned,1)
  self.assertEqual(service.health()['status'],'ok')
 def test_duplicate_snapshot_is_coherent(self):
  a=usage('thread-a','same',150);a['payload']['usage']={'input_tokens':100,'output_tokens':50,'total_tokens':150}
  b=usage('thread-a','same',150,'2026-09-18T16:31:00Z');b['payload']['usage']={'input_tokens':90,'output_tokens':60,'total_tokens':150}
  write_lines(self.path,[session(),turn('test'),a,b]);self.db.scan();s=self.db.dashboard(0)['summary']
  self.assertEqual((s['input_tokens'],s['output_tokens'],s['total_tokens']),(90,60,150))
 def test_query_cache_invalidates_after_scan(self):
  with patch.object(self.db,'_fetch_rows',wraps=self.db._fetch_rows) as fetch:
   self.db.dashboard(0);self.db.dashboard(0);self.assertEqual(fetch.call_count,1)
   write_lines(self.path,[session(),turn('test'),usage('thread-a','new',900)]);self.db.scan()
   self.assertEqual(self.db.dashboard(0)['summary']['total_tokens'],900);self.assertEqual(fetch.call_count,2)
 def test_official_label_is_utf8(self):
  self.assertEqual(self.db.catalog.label('openai'),'OpenAI 官方')
if __name__=='__main__':unittest.main()

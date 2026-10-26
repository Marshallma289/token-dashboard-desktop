"""A cumulative interval ending today is not today's usage."""
import datetime as dt
from contextlib import closing
from pathlib import Path
import tempfile
import unittest

from backend import DashboardDB
from test_hermes import make_hermes


class HermesDayAttributionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        source=self.root/'hermes/state.db'
        make_hermes(source,first_seen=dt.datetime(2026,10,24,6,tzinfo=dt.timezone.utc).timestamp(),
                    last_seen=dt.datetime(2026,10,26,6,tzinfo=dt.timezone.utc).timestamp(),
                    input_tokens=1000,cache_read_tokens=100000000,cache_write_tokens=0,output_tokens=200)
        self.db=DashboardDB(self.root/'usage.db',roots=[self.root/'empty'],hermes_db=source,
            timezone_name='Asia/Shanghai',clock=lambda:dt.datetime(2026,10,26,8,tzinfo=dt.timezone.utc))
        self.addCleanup(self.db.close)
        self.db.scan()

    def test_today_does_not_count_entire_cross_day_interval(self):
        data=self.db.dashboard(1,source_kind='hermes')
        self.assertEqual(data['summary']['total_tokens'],0)
        self.assertTrue(data['time_attribution']['today_incomplete'])
        self.assertTrue(data['time_attribution']['summary_incomplete'])
        self.assertEqual(data['time_attribution']['unallocated_records'],1)
        self.assertEqual(self.db.requests(days=1,source_kind='hermes')['total'],0)

    def test_all_time_keeps_total_but_daily_charts_do_not_assign_it_to_today(self):
        data=self.db.dashboard(0,source_kind='hermes')
        self.assertEqual(data['summary']['total_tokens'],100001200)
        self.assertEqual(data['daily_activity'],[])
        self.assertEqual(data['hourly_activity'],[])
        self.assertEqual(data['daily_model_usage'][0]['date'],'')
        self.assertEqual(data['daily_model_usage'][0]['date_attribution'],'undated_interval')
        self.assertFalse(data['time_attribution']['summary_incomplete'])

    def test_range_must_cover_whole_interval_and_history_is_preserved(self):
        included=self.db.dashboard(0,start_date='2026-10-24',end_date='2026-10-26',source_kind='hermes')
        self.assertEqual(included['summary']['total_tokens'],100001200)
        partial=self.db.dashboard(0,start_date='2026-10-25',end_date='2026-10-26',source_kind='hermes')
        self.assertEqual(partial['summary']['total_tokens'],0)
        self.assertEqual(self.db.requests(days=0,source_kind='hermes')['data'][0]['date_attribution'],'undated_interval')
        self.assertEqual(self.db.counts()['usage_records'],1)

    def test_single_local_day_still_has_exact_daily_usage(self):
        make_hermes(self.root/'hermes/profiles/one/state.db',
            first_seen=dt.datetime(2026,10,25,17,tzinfo=dt.timezone.utc).timestamp(),
            last_seen=dt.datetime(2026,10,26,6,tzinfo=dt.timezone.utc).timestamp(),
            input_tokens=10,cache_read_tokens=20,cache_write_tokens=0,output_tokens=5)
        self.db.scan()
        data=self.db.dashboard(1,source_kind='hermes')
        self.assertEqual(data['summary']['total_tokens'],35)
        self.assertEqual(data['daily_activity'][0]['date'],'2026-10-26')
        self.assertEqual(data['daily_activity'][0]['total_tokens'],35)

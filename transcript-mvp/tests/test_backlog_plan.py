import tempfile
import unittest
from datetime import datetime, UTC, timedelta
from pathlib import Path
from unittest.mock import patch

from kairos_transcripts.common import Problem, save_json
from kairos_transcripts.monitor import Monitor


class BacklogPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        save_json(self.root/'config/monitor.json', {
            'enabled': True, 'initial_since': '2026-10-19T00:00:00+00:00',
            'repository': 'owner/repo', 'state_branch': 'state',
            'max_videos_per_run': 2, 'max_transcript_characters_per_run': 100,
            'warning_pending_count': 10, 'use_youtube_data_api': False,
            'use_gemini': False, 'use_paid_api': False})
        save_json(self.root/'config/channels.json', {'channels': []})
        self.app = Monitor(self.root)

    def tearDown(self):
        self.app.close()
        self.tmp.cleanup()

    def add(self, count):
        for i in range(count):
            self.app.add({'video_id':f'v{i:010d}', 'title':'test', 'published_at':'2026-10-20T00:00:00+00:00'})
        self.app.db.commit()

    def starts(self, future=False):
        start = datetime.now(UTC) + timedelta(days=1 if future else -2)
        return [(start+timedelta(hours=5*i)).isoformat() for i in range(3)]

    def test_snapshot_partition_and_idempotency_preserve_ids_and_status(self):
        self.add(127)
        self.app.add({'video_id':'alreadySent'},'SENT'); self.app.db.commit()
        plan = self.app.create_backlog_plan('request',self.starts(True))
        self.assertEqual([w['target'] for w in plan['waves']], [42,42,43])
        self.assertEqual(plan['remaining'],127)
        self.assertEqual(self.app.db.execute('SELECT count(DISTINCT video_id) FROM backlog_items').fetchone()[0],127)
        self.add(128)
        self.assertEqual(self.app.create_backlog_plan('another',self.starts())['plan_id'],'request')
        self.assertEqual(self.app.backlog_status()['remaining'],127)
        self.assertEqual(self.app.db.execute("SELECT status FROM videos WHERE video_id='alreadySent'").fetchone()[0],'SENT')

    def test_future_wave_cannot_be_collected_or_selected(self):
        self.add(3); self.app.create_backlog_plan('request',self.starts(True))
        self.assertEqual(self.app.queue(10),[])
        with patch('kairos_transcripts.monitor.collect_batch') as network:
            with self.assertRaisesRegex(Problem,'BACKLOG_WAVE_NOT_DUE'):
                self.app.collect('v0000000000')
            network.assert_not_called()
        self.app.db.execute("UPDATE videos SET status='READY' WHERE video_id='v0000000000'")
        self.app.db.commit()
        with self.assertRaisesRegex(Problem,'BACKLOG_WAVE_NOT_DUE'):
            self.app.dispatch('v0000000000')

    def test_due_wave_and_carryover_precede_new_videos(self):
        self.add(6)
        starts=self.starts()
        starts[2]=(datetime.now(UTC)+timedelta(days=1)).isoformat()
        self.app.create_backlog_plan('request', starts)
        self.app.add({'video_id':'newOutside1'}); self.app.db.commit()
        self.assertEqual([v['video_id'] for v in self.app.queue(20)], [f'v{i:010d}' for i in range(4)])
        self.app.db.execute("UPDATE videos SET status='SENT' WHERE video_id='v0000000000'")
        self.app.db.commit()
        self.assertEqual(self.app.queue(20)[0]['video_id'],'v0000000001')
        self.assertEqual(self.app.backlog_status()['waves'][0]['remaining'],1)

    def test_backlog_budget_applies_only_to_snapshot_and_expands_collection(self):
        self.add(3); self.app.create_backlog_plan('request',self.starts())
        self.app.add({'video_id':'newOutside1'}); self.app.db.commit()
        self.assertEqual(self.app.collection_limits('newOutside1'),(2,100))
        for i in range(3):
            with patch('kairos_transcripts.monitor.time.time',return_value=1000+i*20):
                with patch('kairos_transcripts.monitor.collect_batch',return_value=[{'status':'OK','characters':60}]):
                    self.assertEqual(self.app.collect(f'v{i:010d}')['status'],'OK')

    def test_finished_plan_restores_regular_queue(self):
        self.add(3); self.app.create_backlog_plan('request',self.starts())
        self.app.db.execute("UPDATE videos SET status='SENT' WHERE video_id IN ('v0000000000','v0000000001')")
        self.app.db.execute("UPDATE videos SET status='FILTERED' WHERE video_id='v0000000002'")
        self.app.add({'video_id':'newOutside1'}); self.app.db.commit()
        self.assertEqual(self.app.backlog_status()['remaining'],0)
        self.assertEqual(self.app.queue()[0]['video_id'],'newOutside1')

    def test_three_batches_must_be_spaced(self):
        self.add(3)
        with self.assertRaisesRegex(Problem,'THREE_SPACED_WAVES_REQUIRED'):
            self.app.create_backlog_plan('request',['2026-10-21T00:00:00Z']*3)


if __name__ == '__main__':
    unittest.main()

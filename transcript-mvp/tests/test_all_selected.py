import json
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import test_monitor as fixture
from test_pipeline import summary, transcript

from kairos_transcripts.common import Problem, save_json
from kairos_transcripts.monitor import boundary
from kairos_transcripts.summary import delivery_bundle
from kairos_transcripts.telegram import validate_bundle


class AllSelectedTests(unittest.TestCase):
    setUp = fixture.MonitorTests.setUp
    tearDown = fixture.MonitorTests.tearDown
    item = fixture.MonitorTests.item

    def test_all_selected_keeps_low_simple_and_content_duplicate_but_validates_source(self):
        for change in [{'importance': 1}, {'simple_content': True}, {'duplicate_of': 'n6nO1MkYId0'}]:
            value = dict(summary(), **change)
            bundle = delivery_bundle(value, transcript(), 'all_selected')
            self.assertEqual(validate_bundle(bundle)['delivery_policy'], 'all_selected')
            with self.assertRaisesRegex(Problem, 'FILTERED'):
                delivery_bundle(value, transcript())
        value = dict(summary(), importance=1)
        value['points'][0]['evidence'][0]['quote'] = 'nonexistent source'
        with self.assertRaisesRegex(Problem, 'EVIDENCE_QUOTE_MISMATCH'):
            delivery_bundle(value, transcript(), 'all_selected')

    def test_new_uploads_are_not_held_behind_future_backlog_wave(self):
        self.app.policy['delivery_policy'] = 'all_selected'
        self.app.add(self.item())
        self.app.db.execute('INSERT INTO backlog_items VALUES (?,?,?,?)',
                            (fixture.VID,'plan',1,(datetime.now(UTC)+timedelta(hours=5)).isoformat()))
        self.app.add(self.item('n6nO1MkYId0'))
        self.app.db.commit()
        self.assertEqual([x['video_id'] for x in self.app.queue()], ['n6nO1MkYId0'])
        self.assertEqual(self.app.collection_limits('n6nO1MkYId0'), (None, None))
        with self.assertRaisesRegex(Problem, 'BACKLOG_WAVE_NOT_DUE'):
            self.app.collection_limits(fixture.VID)

    def test_review_low_importance_still_requires_evidence_and_prepares_delivery(self):
        self.app.policy['delivery_policy'] = 'all_selected'
        self.app.add(self.item())
        self.app.db.commit()
        save_json(self.root/'transcripts'/f'{fixture.VID}.json', transcript())
        draft = self.root/'summaries'/'draft.json'
        save_json(draft, dict(summary(), importance=2))
        self.assertEqual(self.app.review(fixture.VID, draft)['status'], 'READY')
        bundle = json.loads((self.root/'summaries'/f'{fixture.VID}.delivery.json').read_text(encoding='utf-8'))
        self.assertEqual(validate_bundle(bundle)['importance'], 2)

    def test_restore_only_policy_filters_never_removed_or_already_sent(self):
        self.app.policy['delivery_policy'] = 'all_selected'
        for vid,status in [(fixture.VID,'FILTERED'),('n6nO1MkYId0','FILTERED'),('QslXeEZ0tPI','SENT')]:
            self.app.add(self.item(vid), status)
            save_json(self.root/'summaries'/f'{vid}.summary.json', dict(summary(), importance=2))
        self.app.db.execute("UPDATE videos SET reason='CHANNEL_REMOVED_BY_USER' WHERE video_id='n6nO1MkYId0'")
        self.app.db.commit()
        self.assertEqual(self.app.restore_policy_filtered()['restored'], [fixture.VID])
        self.assertEqual(self.app.restore_policy_filtered()['count'], 0)
        statuses = {r[0]:r[1] for r in self.app.db.execute('SELECT video_id,status FROM videos')}
        self.assertEqual(statuses['n6nO1MkYId0'], 'FILTERED')
        self.assertEqual(statuses['QslXeEZ0tPI'], 'SENT')

    def test_no_arbitrary_video_or_text_cutoff_but_usage_ledger_is_preserved(self):
        self.app.policy['delivery_policy'] = 'all_selected'
        self.app.add(self.item())
        slot = boundary(datetime.now(UTC)).isoformat()
        self.app.db.execute('INSERT INTO run_budget VALUES (?,?,?)',
                            (slot,json.dumps([f'v{i:010d}' for i in range(8)]),999999))
        self.app.db.commit()
        save_json(self.root/'transcripts'/f'{fixture.VID}.json', transcript())
        with patch('kairos_transcripts.monitor.collect_batch', return_value=[{'status':'CACHED','characters':999999}]):
            self.assertEqual(self.app.collect(fixture.VID)['status'], 'CACHED')
        budget = self.app.db.execute('SELECT * FROM run_budget WHERE slot=?',(slot,)).fetchone()
        self.assertEqual(len(json.loads(budget['video_ids'])), 9)
        self.assertEqual(budget['characters'], 1999998)

    def test_all_forty_videos_remain_available_across_eleven_item_pages(self):
        self.app.policy['delivery_policy'] = 'all_selected'
        for i in range(40):
            self.app.add(self.item(f'v{i:010d}'))
        self.app.db.commit()
        seen = []
        while rows := self.app.queue(11):
            seen.extend(r['video_id'] for r in rows)
            for r in rows:
                self.app.db.execute("UPDATE videos SET status='SENT' WHERE video_id=?",(r['video_id'],))
            self.app.db.commit()
        self.assertEqual(len(set(seen)), 40)
        self.assertEqual(len(seen), 40)

    def test_window_progress_reconciles_sent_and_waiting(self):
        for i in range(40):
            self.app.add(self.item(f'v{i:010d}'), 'SENT' if i<11 else 'PENDING')
        self.app.db.commit()
        p=self.app.progress(fixture.START.isoformat(),(fixture.START+timedelta(hours=5)).isoformat())
        self.assertEqual((p['known_matching_videos'],p['sent'],p['waiting']), (40,11,29))
        self.assertEqual(p['known_matching_videos'], p['sent']+p['waiting']+p['filtered']+p['delivery_attention'])

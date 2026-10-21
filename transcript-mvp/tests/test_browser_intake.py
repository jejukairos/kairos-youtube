import copy
import unittest

import test_monitor as fixture
from kairos_transcripts.browser_intake import save_browser_capture, text_checksum
from kairos_transcripts.common import Problem, read_json


class BrowserIntakeTests(unittest.TestCase):
    setUp = fixture.MonitorTests.setUp
    tearDown = fixture.MonitorTests.tearDown
    item = fixture.MonitorTests.item

    def payload(self):
        rows = [{'time':'0:00','text':'첫째 문장'}, {'time':'0:08','text':'둘째 문장'},
                {'time':'0:18','text':'마지막 문장'}]
        raw = '\n'.join(f"[{r['time']}] {r['text']}" for r in rows)
        return dict(video_id=fixture.VID, channel_id=fixture.CID, channel='Test', title='Test', rows=rows,
                    dom_count=3, duration_seconds=20, text_checksum=text_checksum(raw))

    def test_complete_capture_saved_without_network_or_status_promotion(self):
        self.app.add(self.item()); self.app.db.commit()
        result = save_browser_capture(self.root, self.payload())
        self.assertEqual(result['status'], 'SAVED')
        self.assertEqual(result['segments'], 3)
        item = read_json(self.root/'transcripts'/f'{fixture.VID}.json')
        self.assertEqual(item['segments'][-1]['start'], 18)
        self.assertEqual(item['captions_language'], 'und')
        self.assertEqual(self.app.db.execute('SELECT status FROM videos').fetchone()[0], 'PENDING')

    def test_count_checksum_and_end_incompleteness_rejected(self):
        self.app.add(self.item()); self.app.db.commit()
        for change, code in [({'dom_count':4}, 'INCOMPLETE_BROWSER_CAPTURE'),
                             ({'text_checksum':'00000000'}, 'CHECKSUM_MISMATCH'),
                             ({'duration_seconds':400}, 'END_NOT_CONFIRMED')]:
            value = dict(self.payload(), **change)
            with self.assertRaisesRegex(Problem, code):
                save_browser_capture(self.root, value)
        self.assertFalse((self.root/'transcripts'/f'{fixture.VID}.json').exists())

    def test_sent_capture_cannot_overwrite_source_or_revive_delivery(self):
        self.app.add(self.item(), 'SENT'); self.app.db.commit()
        with self.assertRaisesRegex(Problem, 'NOT_AVAILABLE'):
            save_browser_capture(self.root, self.payload())

    def test_changed_existing_source_requires_review(self):
        self.app.add(self.item()); self.app.db.commit()
        save_browser_capture(self.root, self.payload())
        value = copy.deepcopy(self.payload());value['rows'][1]['text']='변경된 원문'
        value['text_checksum']=text_checksum('\n'.join(f"[{r['time']}] {r['text']}" for r in value['rows']))
        with self.assertRaisesRegex(Problem, 'SOURCE_CHANGED|TRANSCRIPT_CHANGED'):
            save_browser_capture(self.root, value)

    def test_unicode_checksum_matches_javascript_utf16(self):
        self.assertEqual(text_checksum('hello'), '4f9f2cab')
        self.assertNotEqual(text_checksum('📊'), text_checksum('📈'))

    def test_caption_language_is_not_assumed_from_korean_video_title(self):
        self.app.add(self.item()); self.app.db.commit()
        value = self.payload();value['language']='en'
        save_browser_capture(self.root, value)
        self.assertEqual(read_json(self.root/'transcripts'/f'{fixture.VID}.json')['captions_language'], 'en')

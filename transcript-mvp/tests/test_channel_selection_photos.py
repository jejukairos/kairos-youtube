import re
import unittest
from datetime import timedelta
from unittest.mock import Mock, patch

import test_monitor as fixture
from test_pipeline import summary, transcript

from kairos_transcripts.common import Problem, digest, save_json
from kairos_transcripts.monitor import Monitor
from kairos_transcripts.summary import delivery_bundle
from kairos_transcripts.telegram import Telegram, notice_bundle, photo_card, units, validate_bundle


class SelectionTests(unittest.TestCase):
    setUp = fixture.MonitorTests.setUp
    tearDown = fixture.MonitorTests.tearDown
    item = fixture.MonitorTests.item

    def reload(self, config):
        self.app.close()
        save_json(self.root / 'config/channels.json', config)
        self.app = Monitor(self.root)

    def test_removed_pending_is_excluded_but_sent_history_retained(self):
        self.app.add(self.item())
        self.app.add(self.item('n6nO1MkYId0'), 'SENT')
        self.app.db.commit()
        self.reload({'channels': [], 'excluded_channel_ids': [fixture.CID]})
        self.assertEqual(self.app.queue(), [])
        rows = {r['video_id']: dict(r) for r in self.app.db.execute('SELECT * FROM videos')}
        self.assertEqual(rows[fixture.VID]['status'], 'FILTERED')
        self.assertEqual(rows[fixture.VID]['reason'], 'CHANNEL_REMOVED_BY_USER')
        self.assertEqual(rows['n6nO1MkYId0']['status'], 'SENT')
        with self.assertRaises(Problem):
            self.app.collect(fixture.VID)

    def test_series_filter_applies_to_new_and_already_queued_videos(self):
        self.app.add(self.item())
        self.app.db.commit()
        pattern = r'^\s*한국경제신문\s*30\s*분\s*만에\s*읽기(?:\s|[|｜:：]|$)'
        self.reload({'channels': [{'channel_id': fixture.CID, 'name': 'HK', 'title_regex': pattern}]})
        for title in ['한국경제신문 30분 만에 읽기 | 20261021🌞#모닝루틴',
                      '한국경제신문30분만에읽기 | 20261020']:
            self.assertTrue(re.search(pattern, title))
        excluded = self.item('n6nO1MkYId0')
        excluded['title'] = '한국경제신문 기자의 증시 전망 라이브'
        self.app.add(excluded)
        self.assertIsNone(self.app.db.execute('SELECT * FROM videos WHERE video_id=?',
                                            (excluded['video_id'],)).fetchone())
        included = self.item('QslXeEZ0tPI')
        included['title'] = '한국경제신문 30분 만에 읽기 | 20261021🌞#모닝루틴'
        self.app.add(included)
        self.app.db.commit()
        self.assertEqual([v['video_id'] for v in self.app.queue()], [included['video_id']])

    def test_per_channel_start_does_not_import_past_uploads(self):
        new_start = fixture.START + timedelta(days=1)
        self.reload({'channels': [{'channel_id': fixture.CID, 'name': 'New',
                                  'initial_since': new_start.isoformat()}]})
        end = new_start + timedelta(hours=1)
        old = self.item()
        new = self.item('n6nO1MkYId0', new_start)
        self.app.discover(end, lambda c: {'channel_id': fixture.CID, 'videos': [old, new]})
        self.assertEqual([v['video_id'] for v in self.app.queue()], ['n6nO1MkYId0'])

    def test_dispatch_blocks_removed_channel_even_when_manually_set_ready(self):
        self.app.add(self.item(), 'READY')
        self.app.excluded_channels.add(fixture.CID)
        with patch('kairos_transcripts.monitor.subprocess.run') as external:
            with self.assertRaisesRegex(Problem, 'CHANNEL_REMOVED_BY_USER'):
                self.app.dispatch(fixture.VID)
            external.assert_not_called()


class PhotoTests(unittest.TestCase):
    def test_photo_caption_bound_and_same_video_url_required(self):
        card = photo_card('L2GG_DeUDa0', '📺' * 500, '긴 제목📊' * 500)
        self.assertLessEqual(units(card['text']), 1024)
        self.assertIn('https://www.youtube.com/watch?v=L2GG_DeUDa0', card['text'])
        bundle = delivery_bundle(summary(), transcript())
        validate_bundle(bundle)
        for photo in ['https://private.example/image.jpg',
                      'https://i.ytimg.com/vi/n6nO1MkYId0/hqdefault.jpg']:
            bundle['parts'][0]['photo'] = photo
            bundle['bundle_hash'] = digest({k: v for k, v in bundle.items() if k != 'bundle_hash'})
            with self.assertRaisesRegex(Problem, 'INVALID_DELIVERY_PHOTO'):
                validate_bundle(bundle)

    def test_photo_transport_uses_sendphoto_and_keeps_text_transport(self):
        session = Mock()
        session.post.return_value = Mock(status_code=200)
        session.post.return_value.json.return_value = {'ok': True, 'result': {'message_id': 1}}
        client = Telegram('123:' + 'x' * 25, '12345', session)
        part = photo_card('L2GG_DeUDa0', '채널', '영상')
        self.assertEqual(client.send(part)['status'], 'SENT')
        self.assertTrue(session.post.call_args.args[0].endswith('/sendPhoto'))
        payload = session.post.call_args.kwargs['json']
        self.assertEqual(payload['caption'], part['text'])
        self.assertNotIn('allow_paid_broadcast', payload)
        client.send({'text': '요약', 'preview': False})
        self.assertTrue(session.post.call_args.args[0].endswith('/sendMessage'))

    def test_notice_photo_requires_short_caption_with_source_link(self):
        bundle = notice_bundle('test:photo', 'https://www.youtube.com/watch?v=L2GG_DeUDa0',
                               'L2GG_DeUDa0')
        self.assertIn('photo', bundle['parts'][0])
        for text in ['주소 없는 사진', 'x' * 1025]:
            with self.assertRaisesRegex(Problem, 'INVALID_DELIVERY_PHOTO'):
                notice_bundle('test:photo', text, 'L2GG_DeUDa0')

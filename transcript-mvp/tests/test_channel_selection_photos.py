import re
import unittest
from datetime import timedelta
from unittest.mock import Mock, patch

import test_monitor as fixture
from test_pipeline import summary, transcript

from kairos_transcripts.common import Problem, digest, now, save_json
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

    def test_person_match_in_description_survives_reload(self):
        config = {'channels': [{'channel_id': fixture.CID, 'name': 'Test',
                               'content_regex': r'윤\s*지\s*호|윤쎈'}]}
        self.reload(config)
        item = self.item()
        item['description'] = '출연: 윤지호 경제평론가'
        self.app.add(item)
        self.app.db.commit()
        self.reload(config)
        self.assertEqual([v['video_id'] for v in self.app.queue()], [fixture.VID])
        other = self.item('n6nO1MkYId0')
        other['description'] = '다른 출연자의 방송'
        self.app.add(other)
        self.assertIsNone(self.app.db.execute('SELECT * FROM videos WHERE video_id=?',
                                            (other['video_id'],)).fetchone())
        alias = self.item('QslXeEZ0tPI')
        alias['title'] = '윤쎈의 경제 전망'
        self.app.add(alias)
        self.assertIsNotNone(self.app.db.execute('SELECT * FROM videos WHERE video_id=?',
                                               (alias['video_id'],)).fetchone())

    def test_public_feed_preserves_description_for_person_filter(self):
        xml = f'''<feed xmlns="http://www.w3.org/2005/Atom"
          xmlns:yt="http://www.youtube.com/xml/schemas/2015"
          xmlns:media="http://search.yahoo.com/mrss/">
          <yt:channelId>{fixture.CID}</yt:channelId>
          <link rel="alternate" href="https://www.youtube.com/channel/{fixture.CID}"/>
          <entry><yt:channelId>{fixture.CID}</yt:channelId><yt:videoId>{fixture.VID}</yt:videoId>
          <title>투자 전망</title><published>{fixture.START.isoformat()}</published>
          <media:group><media:description>출연: 윤지호</media:description></media:group>
          </entry></feed>'''
        from kairos_transcripts.monitor import parse_feed
        item = parse_feed(xml, {'channel_id': fixture.CID, 'name': 'Test'})[0]
        self.assertEqual(item['description'], '출연: 윤지호')

    def test_existing_database_migrates_description_without_losing_history(self):
        self.app.add(self.item(), 'SENT')
        self.app.db.execute('ALTER TABLE videos DROP COLUMN description')
        self.app.db.commit()
        self.reload({'channels': [{'channel_id': fixture.CID, 'name': 'Test'}]})
        row = self.app.db.execute('SELECT * FROM videos WHERE video_id=?', (fixture.VID,)).fetchone()
        self.assertEqual(row['status'], 'SENT')
        self.assertEqual(row['description'], '')

    def test_dispatch_blocks_removed_channel_even_when_manually_set_ready(self):
        self.app.add(self.item(), 'READY')
        self.app.excluded_channels.add(fixture.CID)
        with patch('kairos_transcripts.monitor.subprocess.run') as external:
            with self.assertRaisesRegex(Problem, 'CHANNEL_REMOVED_BY_USER'):
                self.app.dispatch(fixture.VID)
            external.assert_not_called()

    def test_dispatch_requires_current_slot_check_of_every_registered_channel(self):
        self.app.policy['require_channel_audit_before_dispatch'] = True
        self.app.add(self.item(), 'READY')
        with patch('kairos_transcripts.monitor.subprocess.run') as external:
            with self.assertRaisesRegex(Problem, 'CHANNEL_AUDIT_REQUIRED_BEFORE_DISPATCH'):
                self.app.dispatch(fixture.VID)
            external.assert_not_called()

    def test_lookup_failure_is_reported_as_unconfirmed_even_after_check(self):
        self.app.db.execute('INSERT INTO channels(channel_id,cursor,last_checked,last_error) VALUES (?,?,?,?)',
                            (fixture.CID, fixture.START.isoformat(), now(), 'PUBLIC_FEED_FAILED'))
        audit = self.app.audit()
        self.assertEqual(audit['channels_checked'], 1)
        self.assertFalse(audit['coverage_confirmed'])
        self.assertEqual(audit['issues'][0]['reason'], 'PUBLIC_FEED_FAILED')

    def test_checked_channels_allow_validated_summary_dispatch(self):
        self.app.policy['require_channel_audit_before_dispatch'] = True
        self.app.add(self.item(), 'READY')
        self.app.db.execute('INSERT INTO channels(channel_id,cursor,last_checked) VALUES (?,?,?)',
                            (fixture.CID, fixture.START.isoformat(), now()))
        self.app.db.commit()
        save_json(self.root / 'summaries' / f'{fixture.VID}.delivery.json',
                  delivery_bundle(summary(), transcript()))
        with patch('kairos_transcripts.monitor.subprocess.run', return_value=Mock(returncode=0)) as external:
            self.assertEqual(self.app.dispatch(fixture.VID)['status'], 'DISPATCHED')
            external.assert_called_once()


class PhotoTests(unittest.TestCase):
    def test_new_summary_uses_youtube_preview_card_followed_by_summary(self):
        bundle = delivery_bundle(summary(), transcript())
        validate_bundle(bundle)
        first = bundle['parts'][0]
        self.assertTrue(first['preview'])
        self.assertEqual(first['preview_url'], bundle['url'])
        self.assertNotIn('photo', first)
        self.assertTrue(all(not p['preview'] for p in bundle['parts'][1:]))

    def test_preview_transport_requests_large_media_below_channel_title_link(self):
        session = Mock()
        session.post.return_value = Mock(status_code=200)
        session.post.return_value.json.return_value = {'ok': True, 'result': {'message_id': 1}}
        client = Telegram('123:' + 'x' * 25, '12345', session)
        part = delivery_bundle(summary(), transcript())['parts'][0]
        self.assertEqual(client.send(part)['status'], 'SENT')
        self.assertTrue(session.post.call_args.args[0].endswith('/sendMessage'))
        self.assertEqual(session.post.call_args.kwargs['json']['link_preview_options'],
                         {'is_disabled': False, 'url': part['preview_url'],
                          'prefer_large_media': True, 'show_above_text': False})

    def test_preview_url_cannot_point_to_another_video_or_host(self):
        for url in ['https://private.example/image', 'https://www.youtube.com/watch?v=n6nO1MkYId0']:
            bundle = delivery_bundle(summary(), transcript())
            bundle['parts'][0]['preview_url'] = url
            bundle['bundle_hash'] = digest({k: v for k, v in bundle.items() if k != 'bundle_hash'})
            with self.assertRaisesRegex(Problem, 'INVALID_DELIVERY_PREVIEW'):
                validate_bundle(bundle)

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

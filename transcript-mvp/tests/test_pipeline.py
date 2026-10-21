import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from kairos_transcripts.cli import main
from kairos_transcripts.collector import (
    choose_track,
    classify_failure,
    collect_batch,
    collect_values,
    extract_id,
    fetch_ytdlp,
    normalize_transcript,
    parse_subtitle,
    save_transcript,
)
from kairos_transcripts.common import Problem, read_json
from kairos_transcripts.summary import (
    delivery_bundle,
    generate_local,
    render_summary,
    template,
    validate_summary,
)
from kairos_transcripts.telegram import Outbox, Telegram, load_env, split_message, units

VID = 'L2GG_DeUDa0'


def transcript():
    return normalize_transcript({'video_id': VID, 'title': '검증용 가상 영상',
        'channel': '가상 채널', 'segments': [
            {'start': 0, 'duration': 10, 'text': '매출은 100억 원이라고 발표자가 말했다.'},
            {'start': 100, 'duration': 10, 'text': '다음 분기 성장률 20%를 전망했다.'},
            {'start': 200, 'duration': 10, 'text': '수요가 감소하면 전망이 틀릴 위험이 있다.'}]})


def summary(item=None):
    item = item or transcript()
    value = template(item)
    value['importance_reason'] = '숫자와 전망 전제조건이 있는 테스트 자료'
    value['points'] = [
        {'kind': 'number', 'text': '발표자는 매출 100억 원을 언급했다.',
         'evidence': [{'segment': 0, 'quote': '매출은 100억 원'}]},
        {'kind': 'outlook', 'text': '발표자는 다음 분기 성장률 20%를 전망했다.',
         'evidence': [{'segment': 1, 'quote': '성장률 20%'}]},
        {'kind': 'risk', 'text': '수요 감소 시 전망이 틀릴 수 있다는 위험이다.',
         'evidence': [{'segment': 2, 'quote': '전망이 틀릴 위험'}]}]
    return value


class CollectorTests(unittest.TestCase):
    def test_url_normalization_and_dedup(self):
        urls = [f'https://youtu.be/{VID}?si=ignored', f'https://youtube.com/watch?v={VID}',
                f'https://www.youtube.com/shorts/{VID}', VID, '# comment', 'not-a-url',
                f'https://youtube.com.attacker.test/watch?v={VID}']
        videos, invalid = collect_values(urls)
        self.assertEqual(len(videos), 1)
        self.assertEqual(len(invalid), 2)
        self.assertIsNone(extract_id(f'https://secret@youtube.com/watch?v={VID}'))

    def test_manual_priority_before_language_then_language_order(self):
        auto_ko = SimpleNamespace(language_code='ko', is_generated=True)
        manual_en = SimpleNamespace(language_code='en', is_generated=False)
        manual_ko = SimpleNamespace(language_code='ko-KR', is_generated=False)
        self.assertIs(choose_track([auto_ko, manual_en]), manual_en)
        self.assertIs(choose_track([manual_en, manual_ko]), manual_ko)

    def test_error_classification(self):
        for name, expected in [('IpBlocked', 'IP_BLOCKED'), ('RequestBlocked', 'IP_BLOCKED'),
                               ('AgeRestricted', 'AGE_RESTRICTED'), ('VideoUnavailable', 'UNAVAILABLE'),
                               ('NoTranscriptFound', 'NO_CAPTIONS')]:
            self.assertEqual(classify_failure(type(name, (Exception,), {})()), expected)

    def test_batch_continues_and_cache_never_calls_provider(self):
        with tempfile.TemporaryDirectory() as folder:
            fake = Mock()
            fake.list.side_effect = [type('IpBlocked', (Exception,), {})(),
                [SimpleNamespace(language_code='ko', is_generated=True,
                  fetch=lambda: [SimpleNamespace(start=0, duration=1, text='실제 가상 자막')])]]
            ids = [VID, 'n6nO1MkYId0']
            with redirect_stdout(io.StringIO()):
                results = collect_batch(ids, folder, api=fake, metadata=False, delay=0)
                cached = collect_batch([ids[1]], folder, api=Mock(), metadata=False, delay=0)
            self.assertEqual([r['status'] for r in results], ['IP_BLOCKED', 'OK'])
            self.assertEqual(cached[0]['status'], 'CACHED')
            txt = (Path(folder) / f'{ids[1]}.txt').read_text(encoding='utf-8')
            self.assertIn('\n[00:00:00]', txt)
            self.assertNotIn('\\n', txt)

    def test_blocked_input_does_not_attempt_fallback(self):
        fake = Mock()
        fake.list.side_effect = type('IpBlocked', (Exception,), {})()
        with tempfile.TemporaryDirectory() as folder, patch('kairos_transcripts.collector.fetch_ytdlp') as fallback:
            with redirect_stdout(io.StringIO()):
                collect_batch([VID], folder, api=fake, fallback=True, delay=0)
            fallback.assert_not_called()

    def test_empty_nan_and_negative_captions_rejected(self):
        for segments in [[], [{'start': -1, 'duration': 1, 'text': 'bad'}],
                         [{'start': float('nan'), 'duration': 1, 'text': 'bad'}]]:
            with self.assertRaises(Problem):
                normalize_transcript({'video_id': VID, 'segments': segments})

    def test_subtitles_json3_vtt_srt(self):
        value = parse_subtitle(json.dumps({'events': [{'tStartMs': 1000, 'dDurationMs': 2000,
                              'segs': [{'utf8': '금리'}, {'utf8': ' 변화'}]}]}), 'json3')
        self.assertEqual(value[0]['text'], '금리 변화')
        self.assertEqual(value[0]['duration'], 2)
        for text in ['WEBVTT\n\n00:01.000 --> 00:03.000\n<b>자막</b>\n\n',
                     '1\n00:00:01,000 --> 00:00:03,000\n자막\n\n']:
            parsed = parse_subtitle(text, 'vtt')
            self.assertEqual(parsed, [{'start': 1.0, 'duration': 2.0, 'text': '자막'}])

    def test_ytdlp_never_downloads_media_and_no_cookie_config(self):
        info = {'id': VID, 'title': 'Test', 'subtitles': {'ko': [{'ext': 'json3',
                'url': 'https://www.youtube.com/api/timedtext?test=1'}]}}
        response = Mock(text=json.dumps({'events': [{'tStartMs': 0, 'dDurationMs': 1000,
                                                   'segs': [{'utf8': 'test'}]}]}))
        with patch('yt_dlp.YoutubeDL') as downloader, patch('kairos_transcripts.collector.TimedSession') as session:
            downloader.return_value.__enter__.return_value.extract_info.return_value = info
            session.return_value.get.return_value = response
            result = fetch_ytdlp(VID)
            options = downloader.call_args.args[0]
            self.assertTrue(options['skip_download'])
            self.assertNotIn('cookiefile', options)
            downloader.return_value.__enter__.return_value.extract_info.assert_called_once_with(
                f'https://www.youtube.com/watch?v={VID}', download=False)
            self.assertEqual(result['captions_generated'], False)

    def test_ytdlp_preserves_original_language_instead_of_auto_translation(self):
        info = {'subtitles': {}, 'automatic_captions': {
            'ko': [{'ext': 'json3', 'url': 'https://www.youtube.com/api/timedtext?lang=en&tlang=ko'}],
            'en': [{'ext': 'json3', 'url': 'https://www.youtube.com/api/timedtext?lang=en'}]}}
        response = Mock(text=json.dumps({'events': [{'tStartMs': 0, 'dDurationMs': 1000,
                                                    'segs': [{'utf8': 'original English'}]}]}))
        with patch('yt_dlp.YoutubeDL') as downloader, patch('kairos_transcripts.collector.TimedSession') as session:
            downloader.return_value.__enter__.return_value.extract_info.return_value = info
            session.return_value.get.return_value = response
            result = fetch_ytdlp(VID)
            self.assertEqual(result['captions_language'], 'en')
            self.assertNotIn('tlang=', session.return_value.get.call_args.args[0])


class SummaryTests(unittest.TestCase):
    def test_actual_quotes_and_source_binding(self):
        item, value = transcript(), summary()
        checked = validate_summary(value, item)
        self.assertFalse(checked['external_fact_check'])
        rendered = render_summary(value, item)
        self.assertIn('▶️ 발표자는 다음 분기 성장률 20%를 전망했다.', rendered)
        self.assertIn(item['url'], rendered)
        self.assertNotIn('00:01:40', rendered)
        self.assertNotIn('근거', rendered)
        self.assertNotIn('[전망]', rendered)
        self.assertNotIn('★', rendered)
        self.assertNotIn('외부 사실 검증 없음', rendered)
        self.assertEqual(checked['points'][1]['evidence'][0]['segment'], 1)
        item['segments'][0]['text'] += ' 수정'
        with self.assertRaisesRegex(Problem, 'SOURCE_MISMATCH'):
            validate_summary(value, item)

    def test_invented_number_or_quote_rejected(self):
        for kind in ['number', 'quote']:
            value = summary()
            if kind == 'number':
                value['points'][0]['text'] = '매출 999억 원을 언급했다.'
            else:
                value['points'][0]['evidence'][0]['quote'] = '존재하지 않는 발언'
            with self.assertRaises(Problem):
                validate_summary(value, transcript())

    def test_unverified_fact_kind_rejected(self):
        value = summary()
        value['points'][0]['kind'] = 'verified_fact'
        with self.assertRaisesRegex(Problem, 'INVALID_POINT_KIND'):
            validate_summary(value, transcript())

    def test_three_distinct_evidence_segments_required(self):
        value = summary()
        for point in value['points']:
            point['text'] = '논점은 설명된 위험에 있다. ' + point['kind']
            point['evidence'] = [{'segment': 2, 'quote': '위험이 있다'}]
        with self.assertRaisesRegex(Problem, 'INSUFFICIENT_EVIDENCE'):
            validate_summary(value, transcript())

    def test_filter_duplicate_simple_and_low_importance(self):
        for change in [{'importance': 2}, {'simple_content': True}, {'duplicate_of': 'n6nO1MkYId0'}]:
            value = dict(summary(), **change)
            with self.assertRaisesRegex(Problem, 'FILTERED'):
                delivery_bundle(value, transcript())

    def test_deep_summary_requires_detail(self):
        value = dict(summary(), importance=5)
        with self.assertRaisesRegex(Problem, 'DEEP_SUMMARY_REQUIRED'):
            validate_summary(value, transcript())

    def test_no_model_skip_preserves_original_and_never_calls_network(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            save_transcript(root, transcript())
            source = root / f'{VID}.json'
            original = source.read_bytes()
            with redirect_stdout(io.StringIO()), patch('requests.Session.post') as post:
                code = main(['summarize', str(source), '--output-dir', str(root / 'summary')])
            self.assertEqual(code, 0)
            post.assert_not_called()
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(read_json(root / 'summary' / f'{VID}.status.json')['status'], 'SKIPPED')

    def test_cloud_model_and_absent_local_model_are_blocked(self):
        with self.assertRaisesRegex(Problem, 'LOCAL_MODEL_REQUIRED'):
            generate_local(transcript(), 'model:cloud')
        with (patch('requests.Session.get', side_effect=requests.ConnectionError('private details')),
              self.assertRaisesRegex(Problem, 'LOCAL_MODEL_UNAVAILABLE')):
            generate_local(transcript(), 'already-installed-model')


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.outbox = Outbox(Path(self.tmp.name) / 'state.sqlite3')
        self.bundle = delivery_bundle(summary(), transcript())

    def tearDown(self):
        self.outbox.close()
        self.tmp.cleanup()

    def client(self, results):
        client = Mock(target_hash='nonsecret-target-digest')
        client.send.side_effect = results
        return client

    def test_emoji_utf16_and_no_loss(self):
        text = '내용📊' * 5000
        parts = split_message(text)
        self.assertEqual(''.join(parts), text)
        self.assertTrue(all(units(p) <= 4096 for p in parts))

    def test_sent_video_does_not_resend(self):
        client = self.client([{'status': 'SENT', 'message_id': 1}, {'status': 'SENT', 'message_id': 2}])
        self.assertEqual(self.outbox.deliver(self.bundle, client)['status'], 'SENT')
        self.assertEqual(self.outbox.deliver(self.bundle, client)['status'], 'ALREADY_SENT')
        self.assertEqual(client.send.call_count, 2)

    def test_rate_limit_preserves_sent_parts_and_resumes(self):
        client = self.client([{'status': 'SENT', 'message_id': 1},
                              {'status': 'RETRY', 'retry_at': 9999999999}])
        self.assertEqual(self.outbox.deliver(self.bundle, client)['status'], 'RETRY')
        self.assertEqual(self.outbox.deliver(self.bundle, client)['status'], 'WAITING_RETRY')
        with patch('kairos_transcripts.telegram.time.time', return_value=99999999999):
            client.send.side_effect = [{'status': 'SENT', 'message_id': 2}]
            self.assertEqual(self.outbox.deliver(self.bundle, client)['status'], 'SENT')
        self.assertEqual(client.send.call_count, 3)

    def test_timeout_and_crash_never_duplicate(self):
        client = self.client([RuntimeError('secret in raw error')])
        self.assertEqual(self.outbox.deliver(self.bundle, client)['status'], 'UNCERTAIN')
        self.assertEqual(self.outbox.deliver(self.bundle, client)['status'], 'UNCERTAIN')
        self.assertEqual(client.send.call_count, 1)

    def test_modified_bundle_rejected_before_network(self):
        client = self.client([])
        self.bundle['parts'][0]['text'] = 'changed'
        with self.assertRaisesRegex(Problem, 'BUNDLE_CHANGED'):
            self.outbox.deliver(self.bundle, client)
        client.send.assert_not_called()

    def test_checkpoint_failure_prevents_external_send(self):
        self.outbox.checkpoint = Mock(side_effect=Problem('CHECKPOINT_FAILED'))
        client = self.client([])
        with self.assertRaisesRegex(Problem, 'CHECKPOINT_FAILED'):
            self.outbox.deliver(self.bundle, client)
        client.send.assert_not_called()

    def test_second_sender_cannot_claim_a_part_already_in_flight(self):
        other = Outbox(Path(self.tmp.name) / 'state.sqlite3')
        rival = self.client([])
        observed = []
        def send(part):
            observed.append(other.deliver(self.bundle, rival)['status'])
            return {'status': 'SENT', 'message_id': len(observed)}
        first = self.client([])
        first.send.side_effect = send
        try:
            self.assertEqual(self.outbox.deliver(self.bundle, first)['status'], 'SENT')
        finally:
            other.close()
        self.assertEqual(observed, ['UNCERTAIN', 'UNCERTAIN'])
        rival.send.assert_not_called()

    def test_transport_classifies_429_and_never_redirects(self):
        session = Mock()
        session.post.return_value = Mock(status_code=429)
        session.post.return_value.json.return_value = {'ok': False, 'error_code': 429,
                                                       'parameters': {'retry_after': 30}}
        client = Telegram(token='123:' + 'x' * 25, chat_id='12345', session=session)
        result = client.send({'text': '검증용', 'preview': False})
        self.assertEqual(result['status'], 'RETRY')
        self.assertFalse(session.post.call_args.kwargs['allow_redirects'])
        self.assertEqual(session.post.call_args.kwargs['json']['chat_id'], '12345')

    def test_env_loading_is_literal_and_not_executable(self):
        with patch.dict(os.environ, {}, clear=True):
            path = Path(self.tmp.name) / '.env'
            path.write_text('TELEGRAM_BOT_TOKEN="$(do-not-execute)"\nOTHER_SECRET=ignored\n', encoding='utf-8')
            load_env(path)
            self.assertEqual(os.environ['TELEGRAM_BOT_TOKEN'], '$(do-not-execute)')
            self.assertNotIn('OTHER_SECRET', os.environ)


if __name__ == '__main__':
    unittest.main()

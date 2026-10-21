import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from test_pipeline import summary, transcript

from kairos_transcripts.common import Problem, save_json
from kairos_transcripts.monitor import Monitor, boundary, parse_feed
from kairos_transcripts.summary import delivery_bundle
from kairos_transcripts.telegram import notice_bundle, validate_bundle

CID = 'UCaWi2foADm_lKAKnmeQwLSA'
VID = 'L2GG_DeUDa0'
START = datetime(2026,10,20,2,30,tzinfo=UTC)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        save_json(self.root / 'config/monitor.json', {
            'enabled':True,'initial_since':START.isoformat(), 'repository':'owner/repo',
            'state_branch':'state', 'max_videos_per_run':2,'max_transcript_characters_per_run':100,
            'warning_pending_count':2,'use_youtube_data_api':False,'use_gemini':False,'use_paid_api':False})
        save_json(self.root / 'config/channels.json', {'channels':[{'channel_id':CID,'name':'Test','priority':50}]})
        self.app = Monitor(self.root)
        self.app.db.execute("INSERT INTO meta VALUES ('legacy_imported','test')")
        self.app.db.commit()

    def tearDown(self):
        self.app.close()
        self.tmp.cleanup()

    def item(self, video_id=VID, published=START):
        return {'video_id':video_id,'published_at':published.isoformat(),'title':'Test',
                'channel_id':CID,'channel':'Test','priority':50}

    def test_kst_schedule_boundaries(self):
        for utc, expected in [('2026-10-21T01:59:59+00:00','2026-10-20T12:00:00+00:00'),
                              ('2026-10-21T02:00:00+00:00','2026-10-21T02:00:00+00:00'),
                              ('2026-10-21T06:59:59+00:00','2026-10-21T02:00:00+00:00'),
                              ('2026-10-21T07:00:00+00:00','2026-10-21T07:00:00+00:00'),
                              ('2026-10-21T11:59:59+00:00','2026-10-21T07:00:00+00:00'),
                              ('2026-10-21T12:00:00+00:00','2026-10-21T12:00:00+00:00'),
                              ('2026-10-21T14:00:00+00:00','2026-10-21T12:00:00+00:00')]:
            self.assertEqual(boundary(datetime.fromisoformat(utc)).isoformat(), expected)

    def test_closed_open_window_dedup_and_late_index_recovery(self):
        end = START + timedelta(hours=4)
        entries = [self.item(), self.item('n6nO1MkYId0',end),self.item('QslXeEZ0tPI',START-timedelta(seconds=1))]
        fetch = lambda c: {'channel_id':CID,'videos':entries}
        first=self.app.discover(end,fetch)
        self.assertEqual(first['new_queue_items'],1)
        self.assertEqual(self.app.discover(end,fetch)['new_queue_items'],0)
        entries.append(self.item('TORQkD6cwUw',START+timedelta(seconds=1)))
        self.assertEqual(self.app.discover(end+timedelta(hours=1),fetch)['new_queue_items'],2)

    def test_failed_channel_does_not_advance_cursor(self):
        self.app.discover(START+timedelta(hours=1),lambda c:{'channel_id':CID,'videos':[self.item()]})
        result=self.app.discover(START+timedelta(hours=2),lambda c:{'channel_id':CID,'error':'FAILED'})
        cursor=self.app.db.execute('SELECT cursor FROM channels').fetchone()[0]
        self.assertEqual(cursor,(START+timedelta(hours=1)).isoformat())
        self.assertEqual(len(result['errors']),1)

    def test_bounded_rss_gap_is_explicit(self):
        entries=[self.item(f'v{i:010d}',START+timedelta(minutes=i+1)) for i in range(15)]
        result=self.app.discover(START+timedelta(hours=1),lambda c:{'channel_id':CID,'videos':entries})
        self.assertEqual(result['coverage_gaps'],[CID])
        self.assertEqual(self.app.db.execute('SELECT cursor FROM channels').fetchone()[0], START.isoformat())

    def test_free_recovery_requires_older_boundary_and_keeps_failures_visible(self):
        self.app.db.execute('INSERT INTO channels(channel_id,cursor,gap) VALUES (?,?,1)',(CID,START.isoformat()))
        entries=[{'id':f'v{i:010d}'} for i in range(11)]
        def metadata(vid):
            return self.item(vid,START+timedelta(minutes=1) if vid==entries[0]['id'] else START-timedelta(days=1))
        good=self.app.recover_gap(CID,entries,metadata)
        self.assertEqual(good['status'],'RECOVERED')
        self.assertEqual(good['new_queue_items'],1)
        bad=self.app.recover_gap(CID,[{'id':'n6nO1MkYId0'}],lambda vid:{'video_id':vid,'error':'BLOCKED'})
        self.assertEqual(bad['status'],'RECOVERY_INCOMPLETE')

    def test_run_character_limit_preserves_caption_and_does_not_mark_complete(self):
        self.app.add(self.item()); self.app.db.commit()
        with (patch('kairos_transcripts.monitor.collect_batch',return_value=[{'status':'OK','characters':101}]),
              self.assertRaisesRegex(Problem,'RUN_TEXT_BUDGET')):
            self.app.collect(VID)
        self.assertEqual(self.app.db.execute('SELECT status FROM videos').fetchone()[0],'PENDING')

    def test_sent_video_is_not_recollected(self):
        self.app.add(self.item(),'SENT'); self.app.db.commit()
        with patch('kairos_transcripts.monitor.collect_batch') as collect:
            with self.assertRaisesRegex(Problem,'NOT_AVAILABLE'):
                self.app.collect(VID)
            collect.assert_not_called()

    def test_caption_block_pauses_other_uncached_videos_and_expires(self):
        self.app.add(self.item()); self.app.add(self.item('n6nO1MkYId0')); self.app.db.commit()
        with patch('kairos_transcripts.monitor.time.time', return_value=1000):
            with patch('kairos_transcripts.monitor.collect_batch', return_value=[{'status':'IP_BLOCKED'}]) as collect:
                self.assertEqual(self.app.collect(VID)['status'], 'IP_BLOCKED')
                with self.assertRaisesRegex(Problem, 'CAPTION_SOURCE_COOLDOWN'):
                    self.app.collect('n6nO1MkYId0')
                self.assertEqual(collect.call_count, 1)
            self.assertIn('caption_source_resume_at_utc', self.app.stats())
        with patch('kairos_transcripts.monitor.time.time', return_value=23000):
            with patch('kairos_transcripts.monitor.collect_batch', return_value=[{'status':'OK','characters':1}]) as collect:
                self.assertEqual(self.app.collect('n6nO1MkYId0')['status'], 'OK')
                collect.assert_called_once()

    def test_caption_pause_keeps_cached_material_available(self):
        self.app.add(self.item()); self.app.db.commit()
        save_json(self.root / 'transcripts' / f'{VID}.json', transcript())
        self.app.db.execute("INSERT INTO meta VALUES ('caption_source_retry_at','9999999999')")
        self.app.db.commit()
        with patch('kairos_transcripts.monitor.collect_batch', return_value=[{'status':'CACHED','characters':1}]) as collect:
            self.assertEqual(self.app.collect(VID)['status'], 'CACHED')
            collect.assert_called_once()

    def test_caption_requests_are_spaced_across_monitor_instances(self):
        self.app.add(self.item()); self.app.add(self.item('n6nO1MkYId0')); self.app.db.commit()
        other = Monitor(self.root)
        try:
            with patch('kairos_transcripts.monitor.time.time', return_value=1000):
                with patch('kairos_transcripts.monitor.collect_batch', return_value=[{'status':'OK','characters':1}]) as collect:
                    self.app.collect(VID)
                    with self.assertRaisesRegex(Problem, 'CAPTION_REQUEST_SPACING'):
                        other.collect('n6nO1MkYId0')
                    self.assertEqual(collect.call_count, 1)
                    self.assertEqual(other.db.execute('SELECT attempts FROM videos WHERE video_id=?',('n6nO1MkYId0',)).fetchone()[0], 0)
            with patch('kairos_transcripts.monitor.time.time', return_value=1020):
                with patch('kairos_transcripts.monitor.collect_batch', return_value=[{'status':'OK','characters':1}]) as collect:
                    other.collect('n6nO1MkYId0')
                    collect.assert_called_once()
        finally:
            other.close()

    def test_sent_video_cannot_be_revived_by_review(self):
        self.app.add(self.item(),'SENT'); self.app.db.commit()
        with self.assertRaisesRegex(Problem,'NOT_AVAILABLE_FOR_REVIEW'):
            self.app.review(VID,self.root/'missing-draft.json')

    def test_completed_remote_run_with_sending_checkpoint_is_uncertain(self):
        import sqlite3
        remote_path=self.root/'remote.sqlite3'
        remote=sqlite3.connect(remote_path)
        remote.execute('CREATE TABLE deliveries(video_id TEXT,parts TEXT)')
        remote.execute('INSERT INTO deliveries VALUES (?,?)',(VID,json.dumps([{'status':'SENDING'}])))
        remote.commit();remote.close()
        self.app.add(self.item(),'DISPATCHED')
        self.app.db.execute('UPDATE videos SET request_id=? WHERE video_id=?',('request-unique',VID))
        self.app.db.commit()
        with (patch('kairos_transcripts.monitor.remote_file',return_value=remote_path.read_bytes()),
              patch('kairos_transcripts.monitor.gh_json',return_value={'workflow_runs':[
                  {'id':1,'display_title':'Transcript delivery request-unique','status':'completed'}]})):
            self.app.sync()
        self.assertEqual(self.app.db.execute('SELECT status FROM videos').fetchone()[0],'DELIVERY_UNCERTAIN')

    def test_request_tracking_is_saved_before_dispatch_and_uncertain_is_not_repeated(self):
        self.app.add(self.item(),'READY'); self.app.db.commit()
        bundle=delivery_bundle(summary(),transcript())
        save_json(self.root/'summaries'/f'{VID}.delivery.json',bundle)
        with patch('kairos_transcripts.monitor.subprocess.run',return_value=Mock(returncode=1)):
            with self.assertRaisesRegex(Problem,'DISPATCH_UNCERTAIN'):
                self.app.dispatch(VID)
            with self.assertRaisesRegex(Problem,'NOT_READY'):
                self.app.dispatch(VID)

    def test_feed_channel_integrity(self):
        channel={'channel_id':CID,'name':'Test'}
        xml=f'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
          <yt:channelId>{CID[2:]}</yt:channelId><link rel="alternate" href="https://www.youtube.com/channel/{CID}"/>
          <entry><yt:channelId>{CID}</yt:channelId><yt:videoId>{VID}</yt:videoId><title>Test</title>
          <published>2026-10-20T02:30:00Z</published></entry></feed>'''
        self.assertEqual(parse_feed(xml,channel)[0]['video_id'],VID)
        with self.assertRaisesRegex(Problem,'MISMATCH'):
            parse_feed(xml.replace(CID[2:],'incorrect'),channel)

    def test_notice_is_deduplicated_without_pretending_it_is_a_video(self):
        value=notice_bundle('20261021:load','자막 대기 영상이 많습니다.')
        self.assertEqual(validate_bundle(value)['kind'],'notice')
        with patch('kairos_transcripts.monitor.subprocess.run',return_value=Mock(returncode=0)) as run:
            self.assertEqual(self.app.notice('20261021:load','자막 대기 영상이 많습니다.')['status'],'NOTICE_REQUESTED')
            self.assertEqual(self.app.notice('20261021:load','자막 대기 영상이 많습니다.')['status'],'NOTICE_ALREADY_REQUESTED')
            self.assertEqual(run.call_count,1)


if __name__=='__main__':
    unittest.main()

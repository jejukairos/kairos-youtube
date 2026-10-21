"""Public feeds and a persistent queue; never calls an AI or Google Data API."""
import base64
import json
import re
import sqlite3
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import requests
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from .collector import QuietLogger, TimedSession, canonical, collect_batch
from .common import Problem, now, read_json, save_json, save_text
from .summary import (
    delivery_bundle,
    duplicate_transcript,
    render_summary,
    validate_summary,
)
from .telegram import notice_bundle, validate_bundle

NS = {'a': 'http://www.w3.org/2005/Atom', 'yt': 'http://www.youtube.com/xml/schemas/2015',
      'media': 'http://search.yahoo.com/mrss/'}
CID = re.compile(r'^UC[A-Za-z0-9_-]{22}$')
PENDING = ('PENDING', 'CAPTIONS_READY', 'CAPTION_WAIT', 'READY', 'DISPATCHED', 'DISPATCH_UNCERTAIN')


def instant(value):
    if value.endswith('Z'):
        value = value[:-1] + '+00:00'
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise Problem('TIMEZONE_REQUIRED')
    return stamp.astimezone(UTC)


def boundary(stamp):
    # KST is UTC+09 without DST; do not depend on optional Windows tzdata.
    local = stamp.astimezone(timezone(timedelta(hours=9)))
    slots = [(11, 0), (16, 0), (21, 0)]
    times = [local.replace(hour=h, minute=m, second=0, microsecond=0) for h, m in slots]
    previous = [t for t in times if t <= local]
    return (max(previous) if previous else times[-1] - timedelta(days=1)).astimezone(UTC)


def parse_feed(xml, channel):
    cid = channel['channel_id']
    if not CID.fullmatch(cid):
        raise Problem('INVALID_CHANNEL_ID')
    try:
        root = ElementTree.fromstring(xml)
        if root.findtext('yt:channelId', namespaces=NS) not in (cid, cid[2:]):
            raise Problem('FEED_CHANNEL_MISMATCH')
        links = [x.get('href') for x in root.findall('a:link', NS) if x.get('rel') == 'alternate']
        if f'https://www.youtube.com/channel/{cid}' not in links:
            raise Problem('FEED_CHANNEL_MISMATCH')
        videos = []
        for entry in root.findall('a:entry', NS):
            if entry.findtext('yt:channelId', namespaces=NS) != cid:
                raise Problem('FEED_ENTRY_CHANNEL_MISMATCH')
            video_id = entry.findtext('yt:videoId', namespaces=NS)
            published = entry.findtext('a:published', namespaces=NS)
            videos.append({'video_id': video_id, 'url': canonical(video_id or ''),
                           'title': entry.findtext('a:title', default='', namespaces=NS),
                           'description': entry.findtext('media:group/media:description', default='', namespaces=NS),
                           'published_at': instant(published).isoformat(), 'channel_id': cid,
                           'channel': channel['name'], 'priority': channel.get('priority', 50)})
        return sorted(videos, key=lambda v: instant(v['published_at']), reverse=True)
    except Problem:
        raise
    except (ElementTree.ParseError, DefusedXmlException, ValueError, TypeError, AttributeError):
        raise Problem('INVALID_PUBLIC_FEED') from None


def public_feed(channel):
    try:
        response = TimedSession().get('https://www.youtube.com/feeds/videos.xml',
                                     params={'channel_id': channel['channel_id']})
        response.raise_for_status()
        if len(response.content) > 2 * 1024 * 1024:
            raise Problem('FEED_TOO_LARGE')
        return {'channel_id': channel['channel_id'], 'videos': parse_feed(response.content, channel)}
    except Problem as error:
        return {'channel_id': channel['channel_id'], 'error': error.code}
    except requests.RequestException:
        return {'channel_id': channel['channel_id'], 'error': 'PUBLIC_FEED_FAILED'}


def gh_json(repo, endpoint, optional=False):
    result = subprocess.run(['gh', 'api', f'repos/{repo}/{endpoint}'], capture_output=True,
                            text=True, encoding='utf-8', check=False, timeout=60)
    if result.returncode:
        if optional and '(HTTP 404)' in result.stderr:
            return None
        raise Problem('GITHUB_STATE_UNAVAILABLE')
    return json.loads(result.stdout)


def remote_file(repo, name, branch, optional=False):
    value = gh_json(repo, f'contents/{name}?ref={branch}', optional)
    return base64.b64decode(value['content']) if value else None


def public_uploads(channel_id):
    import yt_dlp
    options = {'extract_flat': True, 'skip_download': True, 'quiet': True, 'no_warnings': True,
               'logger': QuietLogger(), 'playlistend': 120, 'socket_timeout': 20,
               'retries': 0, 'extractor_retries': 0, 'proxy': '', 'cachedir': False}
    with yt_dlp.YoutubeDL(options) as downloader:
        value = downloader.extract_info('https://www.youtube.com/playlist?list=UU' + channel_id[2:], download=False)
    return list(value.get('entries', []))


def public_metadata(video_id):
    import yt_dlp
    options = {'skip_download': True, 'quiet': True, 'no_warnings': True,
               'logger': QuietLogger(), 'socket_timeout': 15, 'retries': 0,
               'extractor_retries': 0, 'proxy': '', 'cachedir': False,
               'ignore_no_formats_error': True, 'noplaylist': True}
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            value = downloader.extract_info(canonical(video_id), download=False)
        stamp = value.get('timestamp')
        if not isinstance(stamp, (int, float)):
            raise Problem('PUBLIC_PUBLISH_TIME_MISSING')
        return {'video_id': video_id, 'channel_id': value['channel_id'], 'title': value['title'],
                'description': value.get('description') or '',
                'published_at': datetime.fromtimestamp(stamp, UTC).isoformat()}
    except (ValueError, KeyError, TypeError, Problem):
        return {'video_id': video_id, 'error': 'PUBLIC_METADATA_FAILED'}
    except Exception:  # noqa: BLE001 -- optional public extractor reports version-specific failures
        return {'video_id': video_id, 'error': 'PUBLIC_METADATA_FAILED'}


class Monitor:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.policy = read_json(self.root / 'config/monitor.json')
        for forbidden in ('use_youtube_data_api', 'use_gemini', 'use_paid_api'):
            if self.policy.get(forbidden) is not False:
                raise Problem('NO_PAID_NO_GOOGLE_POLICY_REQUIRED')
        if not self.policy.get('enabled'):
            raise Problem('MONITOR_DISABLED')
        channel_config = read_json(self.root / 'config/channels.json')
        self.channels = channel_config['channels']
        self.excluded_channels = set(channel_config.get('excluded_channel_ids', []))
        self.channel_map = {c['channel_id']: c for c in self.channels}
        if len({c['channel_id'] for c in self.channels}) != len(self.channels):
            raise Problem('DUPLICATE_CHANNEL')
        (self.root / 'state').mkdir(exist_ok=True)
        self.db = sqlite3.connect(self.root / 'state/monitor.sqlite3', timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS channels(channel_id TEXT PRIMARY KEY,cursor TEXT,
              gap INTEGER DEFAULT 0,last_error TEXT,last_checked TEXT);
            CREATE TABLE IF NOT EXISTS videos(video_id TEXT PRIMARY KEY,published_at TEXT,
              title TEXT,channel_id TEXT,channel TEXT,priority INTEGER DEFAULT 50,
              status TEXT DEFAULT 'PENDING',reason TEXT,attempts INTEGER DEFAULT 0,
              retry_at REAL DEFAULT 0,request_id TEXT,run_id INTEGER);
            CREATE TABLE IF NOT EXISTS run_budget(slot TEXT PRIMARY KEY,video_ids TEXT,characters INTEGER);
            CREATE TABLE IF NOT EXISTS backlog_plans(plan_id TEXT PRIMARY KEY,created_at TEXT);
            CREATE TABLE IF NOT EXISTS backlog_items(video_id TEXT PRIMARY KEY,plan_id TEXT,
              wave INTEGER,not_before TEXT);
        ''')
        columns = {r[1] for r in self.db.execute('PRAGMA table_info(channels)')}
        if 'processed_at' not in columns:
            self.db.execute('ALTER TABLE channels ADD COLUMN processed_at TEXT')
            self.db.commit()
        video_columns = {r[1] for r in self.db.execute('PRAGMA table_info(videos)')}
        if 'description' not in video_columns:
            self.db.execute("ALTER TABLE videos ADD COLUMN description TEXT DEFAULT ''")
            self.db.commit()
        for channel in self.channels:
            self.db.execute("UPDATE videos SET channel=? WHERE channel_id=? AND channel=''",
                            (channel['name'], channel['channel_id']))
        self.db.commit()
        self.reconcile_selection()

    def selection_reason(self, item):
        item = dict(item)
        if item['channel_id'] in self.excluded_channels:
            return 'CHANNEL_REMOVED_BY_USER'
        channel = self.channel_map.get(item['channel_id'], {})
        pattern = channel.get('title_regex')
        if pattern and not re.search(pattern, item['title']):
            return 'CHANNEL_SERIES_EXCLUDED'
        content_pattern = channel.get('content_regex')
        if content_pattern and not re.search(content_pattern, item['title'] + '\n' +
                                            (item.get('description') or '')):
            return 'CHANNEL_PERSON_EXCLUDED'
        return None

    def reconcile_selection(self):
        # Preserve SENT and uncertain/in-flight records for dedup and investigation.
        for row in self.db.execute("SELECT * FROM videos WHERE status IN "
                                   "('PENDING','CAPTIONS_READY','CAPTION_WAIT','READY')").fetchall():
            reason = self.selection_reason(row)
            if reason:
                self.db.execute("UPDATE videos SET status='FILTERED',reason=? WHERE video_id=?",
                                (reason, row['video_id']))
        self.db.commit()

    def channel_start(self, channel):
        return max(instant(self.policy['initial_since']),
                   instant(channel.get('initial_since', self.policy['initial_since'])))

    def require_selected(self, row):
        reason = self.selection_reason(row)
        if reason:
            raise Problem(reason)

    def close(self):
        self.db.close()

    def restore_policy_filtered(self):
        if self.policy.get('delivery_policy') != 'all_selected':
            raise Problem('ALL_SELECTED_POLICY_REQUIRED')
        restored = []
        for row in self.db.execute("SELECT * FROM videos WHERE status='FILTERED' AND "
                                   "(reason IS NULL OR reason IN ('LOW_IMPORTANCE','SIMPLE_CONTENT','CONTENT_DUPLICATE'))").fetchall():
            reason = self.selection_reason(row)
            if reason:
                self.db.execute('UPDATE videos SET reason=? WHERE video_id=?', (reason, row['video_id']))
                continue
            path = self.root / 'summaries' / f"{row['video_id']}.summary.json"
            if not path.is_file():
                continue
            value = read_json(path)
            if value.get('importance', 5) < 3 or value.get('simple_content') or value.get('duplicate_of'):
                self.db.execute("UPDATE videos SET status='PENDING',reason='RESTORED_ALL_SELECTED',retry_at=0 WHERE video_id=?",
                                (row['video_id'],))
                restored.append(row['video_id'])
        self.db.commit()
        return {'restored': restored, 'count': len(restored)}

    def add(self, item, status='PENDING'):
        video_id = item['video_id']
        canonical(video_id)
        if status != 'SENT' and self.selection_reason(dict(channel_id=item.get('channel_id', ''),
                                                         title=item.get('title', ''),
                                                         description=item.get('description', ''))):
            return
        self.db.execute('INSERT OR IGNORE INTO videos '
            '(video_id,published_at,title,channel_id,channel,priority,status,description) VALUES (?,?,?,?,?,?,?,?)',
            (video_id, item.get('published_at', self.policy['initial_since']),
             item.get('title', video_id), item.get('channel_id', ''),
             item.get('channel') or item.get('channel_name', ''), item.get('priority', 50), status,
             item.get('description') or ''))

    def bootstrap(self):
        if self.db.execute("SELECT 1 FROM meta WHERE key='legacy_imported'").fetchone():
            return
        repo, branch = self.policy['repository'], self.policy['state_branch']
        raw = remote_file(repo, 'state.json', branch)
        legacy = json.loads(raw)
        for video_id, item in legacy.get('videos', {}).items():
            self.add(dict(item, video_id=video_id), 'SENT' if item.get('status') == 'sent' else 'PENDING')
        self.db.execute("INSERT INTO meta VALUES ('legacy_imported',?)", (now(),))
        self.db.commit()
        self.sync()

    def sync(self):
        raw = remote_file(self.policy['repository'], 'transcript_delivery.sqlite3',
                          self.policy['state_branch'], optional=True)
        changed = 0
        observed = {}
        if raw:
            path = self.root / 'state/remote-delivery-snapshot.sqlite3'
            path.write_bytes(raw)
            remote = sqlite3.connect(path)
            try:
                for video_id, encoded in remote.execute('SELECT video_id,parts FROM deliveries'):
                    if video_id.startswith('notice:'):
                        continue
                    parts = json.loads(encoded)
                    self.add({'video_id': video_id})
                    if all(p['status'] == 'SENT' for p in parts):
                        status, reason, retry = 'SENT', None, 0
                    elif any(p['status'] in ('SENDING', 'UNCERTAIN') for p in parts):
                        status, reason, retry = 'DELIVERY_UNCERTAIN', 'CHECK_TELEGRAM_BEFORE_RETRY', 0
                    elif any(p['status'] == 'RETRY' for p in parts):
                        status, reason = 'READY', 'TELEGRAM_RATE_LIMITED'
                        retry = max(p.get('retry_at', 0) for p in parts)
                    elif any(p['status'] == 'REJECTED' for p in parts):
                        status, reason, retry = 'DELIVERY_REJECTED', 'CHECK_BOT_CONFIGURATION', 0
                    else:
                        continue
                    observed[video_id] = status
                    # An in-flight remote checkpoint is not a failed delivery; leave dispatched work alone.
                    row = self.db.execute('SELECT status FROM videos WHERE video_id=?', (video_id,)).fetchone()
                    if status == 'DELIVERY_UNCERTAIN' and row and row['status'] == 'DISPATCHED':
                        continue
                    self.db.execute('UPDATE videos SET status=?,reason=?,retry_at=? WHERE video_id=?',
                                    (status, reason, retry, video_id))
                    changed += 1
            finally:
                remote.close()
        dispatched = self.db.execute("SELECT * FROM videos WHERE status IN ('DISPATCHED','DISPATCH_UNCERTAIN')").fetchall()
        if dispatched:
            runs = gh_json(self.policy['repository'], 'actions/workflows/transcript-delivery.yml/runs?per_page=100')['workflow_runs']
            for row in dispatched:
                match = next((r for r in runs if row['request_id'] and row['request_id'] in r.get('display_title', '')), None)
                if not match:
                    continue
                self.db.execute('UPDATE videos SET run_id=? WHERE video_id=?', (match['id'], row['video_id']))
                if match['status'] == 'completed':
                    # Re-read the remote snapshot after a later sync to distinguish accepted/uncertain/rejected.
                    # A successful job with a legacy duplicate is already covered by the import above.
                    if observed.get(row['video_id']) == 'DELIVERY_UNCERTAIN':
                        self.db.execute("UPDATE videos SET status='DELIVERY_UNCERTAIN',reason='CHECK_TELEGRAM_BEFORE_RETRY' WHERE video_id=?",
                                        (row['video_id'],))
                    else:
                        self.db.execute("UPDATE videos SET status='READY',reason='REMOTE_RUN_FINISHED_RECHECK_STATE',retry_at=? WHERE video_id=?",
                                        (time.time() + 60, row['video_id']))
        self.db.commit()
        self.reconcile_selection()
        return {'status': 'SYNCED', 'records_checked': changed}

    def discover(self, end=None, fetcher=public_feed):
        self.bootstrap()
        end = end or boundary(datetime.now(UTC))
        start = instant(self.policy['initial_since'])
        if end < start:
            raise Problem('BEFORE_INITIAL_WINDOW')
        added, errors, gaps = 0, [], []
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(fetcher, self.channels))
        for result in results:
            cid = result['channel_id']
            start = self.channel_start(self.channel_map[cid])
            row = self.db.execute('SELECT * FROM channels WHERE channel_id=?', (cid,)).fetchone()
            cursor = instant(row['cursor']) if row and row['cursor'] else start
            self.db.execute('INSERT OR IGNORE INTO channels(channel_id,cursor) VALUES (?,?)', (cid, start.isoformat()))
            if result.get('error'):
                self.db.execute('UPDATE channels SET last_error=?,last_checked=? WHERE channel_id=?',
                                (result['error'], now(), cid))
                errors.append({'channel_id': cid, 'reason': result['error']})
                continue
            videos = result['videos']
            # RSS exposes a bounded recent window. Never silently declare missing older pages complete.
            gap = bool(len(videos) >= 15 and min(instant(v['published_at']) for v in videos) > cursor)
            gap = gap or bool(row and row['gap'])
            for item in videos:
                # Re-scan the visible window for late indexing; ID dedup preserves past sends.
                if start <= instant(item['published_at']) < end:
                    before = self.db.total_changes
                    self.add(item)
                    added += self.db.total_changes - before
            if gap:
                gaps.append(cid)
            self.db.execute('UPDATE channels SET cursor=?,gap=?,last_error=NULL,last_checked=? WHERE channel_id=?',
                            ((cursor if gap else max(cursor, end)).isoformat(), int(gap), now(), cid))
        self.db.commit()
        result = {'status': 'DISCOVERED', 'until_utc': end.isoformat(), 'channels_checked': len(results),
                  'new_queue_items': added, 'errors': errors, 'coverage_gaps': gaps, **self.stats()}
        save_json(self.root / 'state/latest-discovery.json', result)
        return result

    def audit(self):
        """Report actual coverage; a failed lookup never means there were no uploads."""
        slot = boundary(datetime.now(UTC))
        channels = []
        for channel in self.channels:
            row = self.db.execute('SELECT * FROM channels WHERE channel_id=?',
                                  (channel['channel_id'],)).fetchone()
            checked = bool(row and row['last_checked'] and instant(row['last_checked']) >= slot)
            counts = {r[0]: r[1] for r in self.db.execute(
                'SELECT status,count(*) FROM videos WHERE channel_id=? GROUP BY status',
                (channel['channel_id'],))}
            reason = ('NOT_CHECKED_THIS_SLOT' if not checked else row['last_error'] or
                      ('COVERAGE_GAP' if row['gap'] else None))
            channels.append({'channel_id': channel['channel_id'], 'channel': channel['name'],
                             'checked_this_slot': checked, 'reason': reason,
                             'pending': sum(counts.get(s, 0) for s in PENDING),
                             'counts': counts})
        result = {'slot_utc': slot.isoformat(), 'channels_expected': len(channels),
                  'channels_checked': sum(c['checked_this_slot'] for c in channels),
                  'coverage_confirmed': all(not c['reason'] for c in channels),
                  'issues': [c for c in channels if c['reason']], 'channels': channels,
                  'pending': self.stats()['pending']}
        save_json(self.root / 'state/channel-audit.json', result)
        return result

    def progress(self, since=None, until=None):
        end = instant(until) if until else boundary(datetime.now(UTC))
        start = instant(since) if since else boundary(end - timedelta(seconds=1))
        if start >= end:
            raise Problem('INVALID_PROGRESS_WINDOW')
        videos = []
        counts = {'sent': 0, 'waiting': 0, 'filtered': 0, 'delivery_attention': 0}
        for row in self.db.execute('SELECT * FROM videos ORDER BY published_at,video_id'):
            if row['channel_id'] not in self.channel_map or self.selection_reason(row):
                continue
            if not start <= instant(row['published_at']) < end:
                continue
            category = ('sent' if row['status'] == 'SENT' else 'filtered' if row['status'] == 'FILTERED'
                        else 'waiting' if row['status'] in PENDING else 'delivery_attention')
            counts[category] += 1
            videos.append({k: row[k] for k in ('video_id','channel','title','status','reason')})
        audit = self.audit()
        result = {'since_utc': start.isoformat(), 'until_utc': end.isoformat(),
                  'known_matching_videos': len(videos), **counts,
                  'coverage_confirmed': audit['coverage_confirmed'], 'coverage_issues': audit['issues'],
                  'videos': videos}
        save_json(self.root / 'state/window-progress.json', result)
        return result

    def stats(self):
        counts = {r[0]: r[1] for r in self.db.execute('SELECT status,count(*) FROM videos GROUP BY status')}
        pending = sum(counts.get(status, 0) for status in PENDING)
        result = {'counts': counts, 'pending': pending,
                  'load_warning': pending >= self.policy['warning_pending_count']}
        pause = self.db.execute("SELECT value FROM meta WHERE key='caption_source_retry_at'").fetchone()
        if pause and float(pause['value']) > time.time():
            result['caption_source_resume_at_utc'] = datetime.fromtimestamp(float(pause['value']), UTC).isoformat()
        next_request = self.db.execute("SELECT value FROM meta WHERE key='caption_next_request_at'").fetchone()
        if next_request and float(next_request['value']) > time.time():
            result['caption_next_request_at_utc'] = datetime.fromtimestamp(float(next_request['value']), UTC).isoformat()
        plan = self.backlog_status()
        if plan:
            result['backlog_plan'] = plan
        return result

    def backlog_status(self):
        rows = self.db.execute("SELECT b.plan_id,b.wave,b.not_before,v.status FROM backlog_items b "
                               "JOIN videos v ON v.video_id=b.video_id ORDER BY b.wave").fetchall()
        if not rows:
            return None
        waves = {}
        for row in rows:
            wave = waves.setdefault(row['wave'], {'wave': row['wave'], 'not_before': row['not_before'],
                                                  'target': 0, 'sent': 0, 'filtered': 0, 'remaining': 0})
            wave['target'] += 1
            key = 'sent' if row['status'] == 'SENT' else 'filtered' if row['status'] == 'FILTERED' else 'remaining'
            wave[key] += 1
        return {'plan_id': rows[0]['plan_id'], 'waves': list(waves.values()),
                'remaining': sum(w['remaining'] for w in waves.values()),
                'usage_stop_percent': 80,
                'max_videos_per_slot': None if self.policy.get('delivery_policy') == 'all_selected' else 43,
                'max_characters_per_slot': None if self.policy.get('delivery_policy') == 'all_selected' else 360000}

    def create_backlog_plan(self, plan_id, starts):
        if self.backlog_status():
            return self.backlog_status()
        stamps = [instant(s) for s in starts]
        if len(stamps) != 3 or any((b-a).total_seconds() < 18000 for a,b in zip(stamps,stamps[1:])):
            raise Problem('THREE_SPACED_WAVES_REQUIRED')
        rows = self.db.execute("SELECT video_id FROM videos WHERE status IN "
                               "('PENDING','CAPTIONS_READY','CAPTION_WAIT','READY','DISPATCHED','DISPATCH_UNCERTAIN') "
                               "ORDER BY priority DESC,published_at,video_id").fetchall()
        if not rows:
            raise Problem('NO_PENDING_BACKLOG')
        size = len(rows) // 3
        with self.db:
            self.db.execute('INSERT INTO backlog_plans VALUES (?,?)', (plan_id, now()))
            for i,row in enumerate(rows):
                wave = 0 if i < size else 1 if i < size*2 else 2
                self.db.execute('INSERT INTO backlog_items VALUES (?,?,?,?)',
                                (row['video_id'],plan_id,wave+1,stamps[wave].isoformat()))
        return self.backlog_status()

    def collection_limits(self, video_id):
        row = self.db.execute('SELECT not_before FROM backlog_items WHERE video_id=?',(video_id,)).fetchone()
        if row and instant(row['not_before']) > datetime.now(UTC):
            raise Problem('BACKLOG_WAVE_NOT_DUE')
        if self.policy.get('delivery_policy') == 'all_selected':
            return None, None
        if row:
            return 43, 360000
        return self.policy['max_videos_per_run'], self.policy['max_transcript_characters_per_run']

    def recover_gap(self, channel_id, entries=None, metadata=public_metadata):
        channel = next((c for c in self.channels if c['channel_id'] == channel_id), None)
        if not channel:
            raise Problem('UNKNOWN_CHANNEL')
        entries = public_uploads(channel_id) if entries is None else entries
        start = self.channel_start(channel)
        end = datetime.now(UTC)
        added, failures, older, checked, reached_old = 0, [], 0, 0, False
        # The public uploads list is ordered by upload, not a promise of monotonic publish time.
        # Require ten older entries before stopping; keep a coverage warning on any failure.
        for offset in range(0, min(len(entries), 120), 10):
            chunk = entries[offset:offset+10]
            items, missing = {}, []
            for entry in chunk:
                video_id = entry.get('id', '')
                canonical(video_id)
                known = self.db.execute('SELECT * FROM videos WHERE video_id=?', (video_id,)).fetchone()
                if known:
                    items[video_id] = dict(known)
                else:
                    missing.append(video_id)
            with ThreadPoolExecutor(max_workers=4) as pool:
                for item in pool.map(metadata, missing):
                    items[item['video_id']] = item
            for entry in chunk:
                item = items[entry['id']]
                checked += 1
                if item.get('error') or item.get('channel_id') != channel_id:
                    failures.append(item['video_id'])
                    older = 0
                    continue
                published = instant(item['published_at'])
                if published < start:
                    older += 1
                else:
                    older = 0
                if start <= published < end:
                    before = self.db.total_changes
                    self.add(dict(item, channel=channel['name'], priority=channel.get('priority',50)))
                    added += self.db.total_changes - before
            if older >= 10:
                reached_old = True
                break
        complete = reached_old and not failures
        self.db.execute('UPDATE channels SET gap=?,cursor=CASE WHEN ? THEN ? ELSE cursor END,last_error=? WHERE channel_id=?',
                        (int(not complete), int(complete), end.isoformat(), None if complete else 'PUBLIC_RECOVERY_INCOMPLETE', channel_id))
        self.db.commit()
        return {'status': 'RECOVERED' if complete else 'RECOVERY_INCOMPLETE',
                'channel_id': channel_id, 'checked': checked, 'new_queue_items': added, 'failures': failures}

    def queue(self, limit=8):
        self.reconcile_selection()
        if self.policy.get('delivery_policy') == 'all_selected':
            # New uploads keep moving while the older three-wave plan is scheduled.
            # LIMIT is a page size; callers continue paging until blocked or exhausted.
            return [{k: row[k] for k in row.keys() if k != 'description'} for row in self.db.execute(
                "SELECT v.* FROM videos v LEFT JOIN backlog_items b ON b.video_id=v.video_id "
                "WHERE v.status IN ('PENDING','CAPTIONS_READY','CAPTION_WAIT','READY') AND v.retry_at<=? "
                "AND (b.video_id IS NULL OR b.not_before<=?) "
                "ORDER BY (v.status='READY') DESC,(v.status='CAPTIONS_READY') DESC,"
                "(b.video_id IS NOT NULL) DESC,b.wave,v.priority DESC,v.published_at,v.video_id LIMIT ?",
                (time.time(), now(), limit))]
        plan = self.backlog_status()
        if plan and plan['remaining']:
            return [{k: row[k] for k in row.keys() if k != 'description'} for row in self.db.execute("SELECT v.* FROM videos v JOIN backlog_items b "
                "ON b.video_id=v.video_id WHERE v.status IN ('PENDING','CAPTIONS_READY','CAPTION_WAIT','READY') "
                "AND v.retry_at<=? AND b.not_before<=? ORDER BY (v.status='READY') DESC,b.wave,"
                "v.priority DESC,v.published_at,v.video_id LIMIT ?", (time.time(), now(), limit))]
        return [{k: row[k] for k in row.keys() if k != 'description'} for row in self.db.execute("SELECT v.* FROM videos v LEFT JOIN channels c "
            "ON v.channel_id=c.channel_id WHERE v.status IN "
            "('PENDING','CAPTIONS_READY','CAPTION_WAIT','READY') AND v.retry_at<=? "
            "ORDER BY (v.status='READY') DESC,v.priority DESC,COALESCE(c.processed_at,'') ASC,"
            "v.published_at DESC,v.video_id LIMIT ?", (time.time(), limit))]

    def collect(self, video_id):
        row = self.db.execute('SELECT * FROM videos WHERE video_id=?', (video_id,)).fetchone()
        if not row or row['status'] in ('SENT', 'FILTERED', 'DISPATCHED', 'DISPATCH_UNCERTAIN',
                                        'DELIVERY_UNCERTAIN', 'DELIVERY_REJECTED'):
            raise Problem('VIDEO_NOT_AVAILABLE_FOR_COLLECTION')
        self.require_selected(row)
        if row['retry_at'] > time.time():
            raise Problem('VIDEO_WAITING_RETRY')
        video_limit, character_limit = self.collection_limits(video_id)
        pause = self.db.execute("SELECT value FROM meta WHERE key='caption_source_retry_at'").fetchone()
        if (pause and float(pause['value']) > time.time()
                and not (self.root / 'transcripts' / f'{video_id}.json').is_file()):
            raise Problem('CAPTION_SOURCE_COOLDOWN')
        slot = boundary(datetime.now(UTC)).isoformat()
        budget = self.db.execute('SELECT * FROM run_budget WHERE slot=?', (slot,)).fetchone()
        used_ids = json.loads(budget['video_ids']) if budget else []
        used_chars = budget['characters'] if budget else 0
        if video_limit is not None and video_id not in used_ids and len(used_ids) >= video_limit:
            raise Problem('RUN_VIDEO_BUDGET_REACHED')
        if not (self.root / 'transcripts' / f'{video_id}.json').is_file():
            # Reserve a shared start time before network I/O; independent CLI
            # processes must not create a burst of consecutive caption requests.
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                next_request = self.db.execute("SELECT value FROM meta WHERE key='caption_next_request_at'").fetchone()
                if next_request and float(next_request['value']) > time.time():
                    raise Problem('CAPTION_REQUEST_SPACING')
                interval = max(20, float(self.policy.get('caption_request_interval_seconds', 20)))
                self.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('caption_next_request_at',?)",
                                (str(time.time() + interval),))
        results = collect_batch([canonical(video_id)], self.root / 'transcripts', delay=0)
        result = results[0]
        if result['status'] in ('OK', 'CACHED'):
            status, retry = 'CAPTIONS_READY', 0
            if video_id not in used_ids:
                if character_limit is not None and used_chars + result['characters'] > character_limit:
                    raise Problem('RUN_TEXT_BUDGET_REACHED_TRANSCRIPT_PRESERVED')
                used_ids.append(video_id)
                self.db.execute('INSERT OR REPLACE INTO run_budget VALUES (?,?,?)',
                                (slot, json.dumps(used_ids), used_chars + result['characters']))
        else:
            status = 'CAPTION_WAIT'
            retry = time.time() + (86400 if result['status'] in ('AGE_RESTRICTED','UNAVAILABLE','ACCESS_RESTRICTED') else 21600)
            if result['status'] in ('IP_BLOCKED', 'RATE_LIMITED'):
                self.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('caption_source_retry_at',?)",
                                (str(retry),))
        self.db.execute('UPDATE videos SET status=?,reason=?,retry_at=?,attempts=attempts+1 WHERE video_id=?',
                        (status, result['status'], retry, video_id))
        self.db.execute('UPDATE channels SET processed_at=? WHERE channel_id=?', (now(), row['channel_id']))
        self.db.commit()
        return result

    def review(self, video_id, draft):
        row = self.db.execute('SELECT * FROM videos WHERE video_id=?', (video_id,)).fetchone()
        if not row or row['status'] not in ('PENDING', 'CAPTIONS_READY', 'CAPTION_WAIT'):
            raise Problem('VIDEO_NOT_AVAILABLE_FOR_REVIEW')
        self.require_selected(row)
        item = read_json(self.root / 'transcripts' / f'{video_id}.json')
        value = validate_summary(read_json(draft), item)
        value.setdefault('provider', 'codex-scheduled-review')
        duplicate = duplicate_transcript(item, self.root / 'transcripts')
        if duplicate:
            value['duplicate_of'] = duplicate
        target = self.root / 'summaries' / video_id
        save_json(target.with_suffix('.summary.json'), value)
        save_text(target.with_suffix('.summary.txt'), render_summary(value, item))
        policy = self.policy.get('delivery_policy', 'important')
        if policy != 'all_selected' and (value['importance'] < 3 or value['simple_content'] or value.get('duplicate_of')):
            status = 'FILTERED'
        else:
            status = 'READY'
            save_json(target.with_suffix('.delivery.json'), delivery_bundle(value, item, policy))
        reason = ('CONTENT_DUPLICATE' if value.get('duplicate_of') else
                  'SIMPLE_CONTENT' if value['simple_content'] else 'LOW_IMPORTANCE') if status == 'FILTERED' else None
        self.db.execute('UPDATE videos SET status=?,reason=?,retry_at=0 WHERE video_id=?', (status, reason, video_id))
        self.db.commit()
        return {'status': status, 'video_id': video_id}

    def dispatch(self, video_id):
        row = self.db.execute('SELECT * FROM videos WHERE video_id=?', (video_id,)).fetchone()
        if not row or row['status'] != 'READY' or row['retry_at'] > time.time():
            raise Problem('VIDEO_NOT_READY_FOR_DISPATCH')
        self.require_selected(row)
        if self.policy.get('require_channel_audit_before_dispatch'):
            audit = self.audit()
            if audit['channels_checked'] != audit['channels_expected']:
                raise Problem('CHANNEL_AUDIT_REQUIRED_BEFORE_DISPATCH')
        self.collection_limits(video_id)
        bundle = validate_bundle(read_json(self.root / 'summaries' / f'{video_id}.delivery.json'))
        if bundle.get('kind') == 'notice' or bundle.get('video_id') != video_id:
            raise Problem('SUMMARY_DISPATCH_VIDEO_MISMATCH')
        request_id = uuid.uuid4().hex
        self.db.execute("UPDATE videos SET status='DISPATCH_UNCERTAIN',request_id=?,run_id=NULL WHERE video_id=?",
                        (request_id, video_id))
        self.db.commit()
        inputs = {'bundle': base64.b64encode(json.dumps(bundle, ensure_ascii=False).encode()).decode(),
                  'request_id': request_id}
        try:
            result = subprocess.run(['gh','workflow','run','transcript-delivery.yml','--repo',
                self.policy['repository'],'--json'], input=json.dumps(inputs), capture_output=True,
                text=True, encoding='utf-8', check=False, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            raise Problem('DISPATCH_UNCERTAIN_DO_NOT_RETRY') from None
        if result.returncode:
            # Do not guess whether an interrupted HTTP dispatch was accepted.
            raise Problem('DISPATCH_UNCERTAIN_DO_NOT_RETRY')
        self.db.execute("UPDATE videos SET status='DISPATCHED' WHERE video_id=?", (video_id,))
        self.db.commit()
        return {'status': 'DISPATCHED', 'video_id': video_id, 'request_id': request_id}

    def notice(self, key, message, photo_video_id=None, preview_video_id=None):
        # Stable keys allow one backlog/usage warning per day, even across retries.
        record = 'notice-dispatched:' + key
        if self.db.execute('SELECT 1 FROM meta WHERE key=?', (record,)).fetchone():
            return {'status': 'NOTICE_ALREADY_REQUESTED', 'notice_key': key}
        bundle = notice_bundle(key, message, photo_video_id, preview_video_id)
        request_id = uuid.uuid4().hex
        inputs = {'bundle': base64.b64encode(json.dumps(bundle, ensure_ascii=False).encode()).decode(),
                  'request_id': request_id}
        result = subprocess.run(['gh', 'workflow', 'run', 'transcript-delivery.yml', '--repo',
            self.policy['repository'], '--json'], input=json.dumps(inputs), capture_output=True,
            text=True, encoding='utf-8', check=False, timeout=60)
        if result.returncode:
            raise Problem('NOTICE_DISPATCH_UNCERTAIN')
        self.db.execute('INSERT INTO meta VALUES (?,?)', (record, request_id))
        self.db.commit()
        return {'status': 'NOTICE_REQUESTED', 'notice_key': key, 'request_id': request_id}

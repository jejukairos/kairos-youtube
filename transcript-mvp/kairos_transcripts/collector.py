import html
import math
import re
import time
from itertools import pairwise
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

from .common import Problem, digest, now, read_json, save_json, save_text, timestamp

ID_RE = re.compile(r'^[A-Za-z0-9_-]{11}$')
LANGUAGES = ('ko', 'en', 'ja')


class TimedSession(requests.Session):
    def __init__(self):
        super().__init__()
        # Never inherit an unreviewed proxy or account credentials from the machine.
        self.trust_env = False

    def request(self, *args, **kwargs):
        kwargs.setdefault('timeout', (8, 25))
        return super().request(*args, **kwargs)


def extract_id(value):
    value = value.strip().strip('<>').strip('()')
    if ID_RE.fullmatch(value):
        return value
    parsed = urlparse(value)
    if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password:
        return None
    host = (parsed.hostname or '').lower()
    parts = parsed.path.strip('/').split('/')
    if host in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'):
        if parts == ['watch']:
            candidate = parse_qs(parsed.query).get('v', [''])[0]
        elif len(parts) == 2 and parts[0] in ('shorts', 'live', 'embed', 'v'):
            candidate = parts[1]
        else:
            return None
    elif host in ('youtu.be', 'www.youtu.be') and len(parts) == 1:
        candidate = parts[0]
    else:
        return None
    return candidate if ID_RE.fullmatch(candidate) else None


def canonical(video_id):
    if not ID_RE.fullmatch(video_id):
        raise Problem('INVALID_URL')
    return f'https://www.youtube.com/watch?v={video_id}'


def collect_values(values):
    seen, videos, invalid = set(), [], []
    for line, raw in enumerate(values, 1):
        raw = raw.strip()
        if not raw or raw.startswith('#'):
            continue
        video_id = extract_id(raw)
        if not video_id:
            # Do not copy arbitrary pasted input (which may contain a secret) to logs.
            invalid.append({'line': line, 'status': 'INVALID_URL'})
        elif video_id not in seen:
            seen.add(video_id)
            videos.append((video_id, canonical(video_id)))
    return videos, invalid


def collect_urls(path):
    return collect_values(Path(path).read_text(encoding='utf-8-sig').splitlines())


def language_rank(code):
    base = code.split('-')[0]
    return LANGUAGES.index(base) if base in LANGUAGES else len(LANGUAGES)


def choose_track(tracks):
    if not tracks:
        raise Problem('NO_CAPTIONS')
    return min(tracks, key=lambda t: (bool(t.is_generated), language_rank(t.language_code)))


def classify_failure(error):
    if isinstance(error, Problem):
        return error.code
    name = type(error).__name__
    if name in ('TranscriptsDisabled', 'NoTranscriptFound'):
        return 'NO_CAPTIONS'
    if name in ('RequestBlocked', 'IpBlocked'):
        return 'IP_BLOCKED'
    if name in ('AgeRestricted',):
        return 'AGE_RESTRICTED'
    if name in ('VideoUnavailable', 'VideoUnplayable', 'InvalidVideoId'):
        return 'UNAVAILABLE'
    if name in ('PoTokenRequired', 'YouTubeDataUnparsable'):
        return 'ACCESS_RESTRICTED'
    if name == 'TooManyRequests':
        return 'RATE_LIMITED'
    if isinstance(error, requests.Timeout):
        return 'NETWORK_TIMEOUT'
    if isinstance(error, requests.RequestException):
        return 'NETWORK_ERROR'
    return 'COLLECTION_FAILED'


def normalize_transcript(item):
    video_id = item.get('video_id', '')
    url = canonical(video_id)
    if not isinstance(item.get('segments'), list) or not item['segments']:
        raise Problem('EMPTY_TRANSCRIPT')
    segments = []
    for segment in item['segments']:
        try:
            start, duration = float(segment['start']), float(segment['duration'])
            text = str(segment['text']).strip()
        except (KeyError, TypeError, ValueError):
            raise Problem('INVALID_TRANSCRIPT') from None
        if not math.isfinite(start + duration) or min(start, duration) < 0:
            raise Problem('INVALID_TRANSCRIPT')
        if text:
            segments.append({'start': start, 'duration': duration, 'text': text})
    if not segments:
        raise Problem('EMPTY_TRANSCRIPT')
    if any(a['start'] > b['start'] for a, b in pairwise(segments)):
        raise Problem('INVALID_TRANSCRIPT_ORDER')
    item = dict(item, video_id=video_id, url=url, segments=segments)
    item['full_text'] = ' '.join(s['text'] for s in segments)
    item['transcript_hash'] = digest(segments)
    item.setdefault('title', video_id)
    item.setdefault('channel', '채널명 미확인')
    item.setdefault('captions_language', 'und')
    item.setdefault('captions_generated', None)
    item.setdefault('fetched_at_utc', now())
    item.setdefault('source', 'user_import')
    return item


def fetch_one(api, video_id):
    chosen = choose_track(list(api.list(video_id)))
    fetched = chosen.fetch()
    return normalize_transcript({
        'video_id': video_id, 'captions_language': chosen.language_code,
        'captions_generated': bool(chosen.is_generated),
        'source': 'youtube-transcript-api',
        'segments': [{'start': float(s.start), 'duration': float(s.duration),
                      'text': str(s.text)} for s in fetched],
    })


def add_metadata(item, session=None):
    try:
        session = session or TimedSession()
        response = session.get('https://www.youtube.com/oembed',
                               params={'url': item['url'], 'format': 'json'})
        response.raise_for_status()
        data = response.json()
        if data.get('title'):
            item['title'] = data['title']
        if data.get('author_name'):
            item['channel'] = data['author_name']
    except (requests.RequestException, ValueError, TypeError):
        item['metadata_status'] = 'UNAVAILABLE'
    return item


class QuietLogger:
    def debug(self, message):
        pass

    info = warning = error = debug


def parse_subtitle(text, extension):
    if extension == 'json3':
        import json
        events = json.loads(text).get('events', [])
        segments = []
        for event in events:
            words = ''.join(s.get('utf8', '') for s in event.get('segs', []))
            if words.strip():
                segments.append({'start': event['tStartMs'] / 1000,
                                 'duration': event.get('dDurationMs', 0) / 1000,
                                 'text': words.strip()})
        return segments
    cue = re.compile(r'(?m)^(\d{2}:)?(\d{2}):(\d{2}[.,]\d+)\s+-->\s+'
                     r'(\d{2}:)?(\d{2}):(\d{2}[.,]\d+)[^\n]*\n(.*?)(?=\n\s*\n|\Z)', re.DOTALL)
    result = []
    for match in cue.finditer(text.replace('\r\n', '\n')):
        def seconds(h, m, s):
            return int((h or '0:')[:-1]) * 3600 + int(m) * 60 + float(s.replace(',', '.'))
        start = seconds(*match.group(1, 2, 3))
        end = seconds(*match.group(4, 5, 6))
        words = html.unescape(re.sub(r'<[^>]*>', '', match[7])).strip()
        if words:
            result.append({'start': start, 'duration': end - start, 'text': words})
    return result


def fetch_ytdlp(video_id):
    # Python API does not read CLI config files; no cookies, login, proxy or media download.
    try:
        import yt_dlp
    except ImportError:
        raise Problem('YTDLP_NOT_INSTALLED') from None
    options = {'skip_download': True, 'quiet': True, 'no_warnings': True,
               'noplaylist': True, 'cachedir': False, 'socket_timeout': 20,
               'retries': 0, 'extractor_retries': 0, 'ignore_no_formats_error': True,
               'logger': QuietLogger(), 'proxy': ''}
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(canonical(video_id), download=False)
        if not info or info.get('age_limit', 0) >= 18:
            raise Problem('AGE_RESTRICTED')
        tracks = []
        for generated, key in [(False, 'subtitles'), (True, 'automatic_captions')]:
            for language, formats in (info.get(key) or {}).items():
                for preferred in ('json3', 'vtt', 'srt'):
                    match = next((f for f in formats if f.get('ext') == preferred), None)
                    if match:
                        tracks.append((generated, language_rank(language), language, match))
                        break
        if not tracks:
            raise Problem('NO_CAPTIONS')
        generated, _, language, selected = min(tracks, key=lambda t: (t[0], t[1]))
        subtitle_url = urlparse(selected['url'])
        host = subtitle_url.hostname or ''
        if subtitle_url.scheme != 'https' or not (host == 'youtube.com' or host.endswith('.youtube.com')):
            raise Problem('UNEXPECTED_CAPTION_HOST')
        response = TimedSession().get(selected['url'], allow_redirects=False)
        response.raise_for_status()
        return normalize_transcript({
            'video_id': video_id, 'title': info.get('title', video_id),
            'channel': info.get('channel') or info.get('uploader') or '채널명 미확인',
            'captions_generated': generated, 'captions_language': language,
            'source': 'yt-dlp-subtitles-only',
            'segments': parse_subtitle(response.text, selected['ext']),
        })
    except Problem:
        raise
    except requests.RequestException as error:
        raise Problem(classify_failure(error)) from None
    except Exception as error:  # noqa: BLE001 -- optional extractor has version-specific exception classes
        # Inspect internally but never log a signed URL, cookie or provider error body.
        message = str(error).lower()
        if 'age' in message and ('restrict' in message or 'confirm' in message):
            code = 'AGE_RESTRICTED'
        elif 'not a bot' in message or 'sign in' in message:
            code = 'ACCESS_RESTRICTED'
        elif '429' in message or 'too many requests' in message:
            code = 'RATE_LIMITED'
        elif 'unavailable' in message or 'private video' in message or 'not available' in message:
            code = 'UNAVAILABLE'
        else:
            code = 'YTDLP_FAILED'
        raise Problem(code) from None


def save_transcript(folder, item):
    item = normalize_transcript(item)
    folder = Path(folder)
    text = (f"제목: {item['title']}\n채널: {item['channel']}\nURL: {item['url']}\n"
            f"언어: {item['captions_language']} / 자동자막: {item['captions_generated']}\n\n")
    text += '\n'.join(f"[{timestamp(s['start'])}] {s['text']}" for s in item['segments']) + '\n'
    # Write TXT first; an interruption never leaves a valid JSON cache without its TXT.
    save_text(folder / f"{item['video_id']}.txt", text)
    save_json(folder / f"{item['video_id']}.json", item)
    return item


def collect_batch(values, folder, force=False, backend='transcript-api', fallback=False,
                  delay=1.5, api=None, metadata=True):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    videos, results = collect_values(values)
    if api is None and backend != 'yt-dlp':
        from youtube_transcript_api import YouTubeTranscriptApi
        api = YouTubeTranscriptApi(http_client=TimedSession())
    for index, (video_id, url) in enumerate(videos):
        result = {'video_id': video_id, 'url': url, 'checked_at_utc': now()}
        try:
            cached = folder / f'{video_id}.json'
            if cached.is_file() and not force:
                item = normalize_transcript(read_json(cached))
                if item['video_id'] != video_id:
                    raise Problem('CACHE_ID_MISMATCH')
                save_transcript(folder, item)
                result['status'] = 'CACHED'
            else:
                try:
                    item = fetch_ytdlp(video_id) if backend == 'yt-dlp' else fetch_one(api, video_id)
                except Exception as error:
                    code = classify_failure(error)
                    # Respect blocking/access restrictions; never try another route to bypass them.
                    if not fallback or backend == 'yt-dlp' or code not in ('NO_CAPTIONS', 'COLLECTION_FAILED'):
                        raise
                    item = fetch_ytdlp(video_id)
                if metadata:
                    add_metadata(item)
                save_transcript(folder, item)
                result['status'] = 'OK'
            result.update(language=item['captions_language'], source=item['source'],
                          segments=len(item['segments']), characters=len(item['full_text']),
                          json_path=f'{video_id}.json', text_path=f'{video_id}.txt')
        except Exception as error:  # noqa: BLE001 -- isolate one failed video without aborting its batch
            result.update(status=classify_failure(error), error_type=type(error).__name__)
        results.append(result)
        save_json(folder / 'index.json', results)
        print(f"[{index + 1}/{len(videos)}] {result['status']} {video_id}", flush=True)
        if index + 1 < len(videos) and delay:
            time.sleep(max(0, delay))
    save_json(folder / 'index.json', results)
    return results

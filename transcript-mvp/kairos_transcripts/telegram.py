import json
import os
import re
import sqlite3
import time
from pathlib import Path

import requests

from .collector import canonical
from .common import Problem, digest


def units(text):
    return len(text.encode('utf-16-le')) // 2


def thumbnail_url(video_id):
    canonical(video_id)
    return f'https://i.ytimg.com/vi/{video_id}/hqdefault.jpg'


def photo_card(video_id, channel, title):
    # Bound metadata independently; preserve the complete clickable original URL.
    caption = f"📺 {split_message(channel or 'YouTube', 220)[0]}\n📊 {split_message(title or video_id, 650)[0]}\n{canonical(video_id)}"
    return {'text': caption, 'preview': False, 'photo': thumbnail_url(video_id)}


def link_card(video_id, channel, title):
    url = canonical(video_id)
    text = f"📺 {split_message(channel or 'YouTube', 220)[0]}\n📊 {split_message(title or video_id, 650)[0]}\n{url}"
    return {'text': text, 'preview': True, 'preview_url': url}


def split_message(text, limit=3800):
    if not 2 <= limit <= 4096:
        raise Problem('INVALID_MESSAGE_LIMIT')
    result, current, size = [], [], 0
    for char in text:
        count = units(char)
        if size + count > limit:
            result.append(''.join(current))
            current, size = [], 0
        current.append(char)
        size += count
    if current:
        result.append(''.join(current))
    return result


def load_env(path=Path('.env')):
    """Literal settings only: no eval, shell expansion, or logging."""
    if not Path(path).is_file():
        return
    for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
        match = re.fullmatch(r'\s*(TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID)\s*=\s*(.*)', line)
        if match:
            value = match[2].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            os.environ.setdefault(match[1], value)


def validate_bundle(bundle):
    if not isinstance(bundle, dict) or bundle.get('schema') != 1:
        raise Problem('INVALID_DELIVERY_BUNDLE')
    notice = bundle.get('kind') == 'notice'
    if notice:
        if not re.fullmatch(r'notice:[A-Za-z0-9:._-]{1,110}', bundle.get('video_id', '')):
            raise Problem('INVALID_NOTICE_KEY')
    elif bundle.get('url') != canonical(bundle.get('video_id', '')):
        raise Problem('INVALID_DELIVERY_URL')
    if bundle.get('bundle_hash') != digest({k: v for k, v in bundle.items() if k != 'bundle_hash'}):
        raise Problem('DELIVERY_BUNDLE_CHANGED')
    policy = bundle.get('delivery_policy', 'important')
    if policy not in ('important', 'all_selected'):
        raise Problem('INVALID_DELIVERY_POLICY')
    minimum = 1 if policy == 'all_selected' else 3
    if not notice and (type(bundle.get('importance')) is not int or not minimum <= bundle['importance'] <= 5):
        raise Problem('FILTERED_LOW_IMPORTANCE')
    for key in (() if notice else ('transcript_hash', 'validated_summary_hash')):
        if not re.fullmatch(r'[a-f0-9]{64}', bundle.get(key, '')):
            raise Problem('INVALID_DELIVERY_HASH')
    parts = bundle.get('parts')
    if not isinstance(parts, list) or not (1 if notice else 2) <= len(parts) <= 20:
        raise Problem('INVALID_DELIVERY_PARTS')
    for part in parts:
        if (not isinstance(part, dict) or not isinstance(part.get('text'), str)
                or not part['text'].strip() or units(part['text']) > 4096
                or type(part.get('preview')) is not bool):
            raise Problem('INVALID_DELIVERY_PART')
        if 'photo' in part:
            photo = part['photo']
            match = re.fullmatch(r'https://i\.ytimg\.com/vi/([A-Za-z0-9_-]{11})/hqdefault\.jpg',
                                 photo) if isinstance(photo, str) else None
            if (not match or units(part['text']) > 1024 or part['preview']
                    or (not notice and match[1] != bundle['video_id'])
                    or canonical(match[1]) not in part['text']):
                raise Problem('INVALID_DELIVERY_PHOTO')
        if 'preview_url' in part:
            url = part['preview_url']
            match = re.fullmatch(r'https://www\.youtube\.com/watch\?v=([A-Za-z0-9_-]{11})',
                                 url) if isinstance(url, str) else None
            if (not match or not part['preview'] or 'photo' in part or url not in part['text']
                    or (not notice and match[1] != bundle['video_id'])):
                raise Problem('INVALID_DELIVERY_PREVIEW')
    return bundle


def notice_bundle(key, text, photo_video_id=None, preview_video_id=None):
    if photo_video_id and preview_video_id:
        raise Problem('CONFLICTING_NOTICE_MEDIA')
    value = {'schema': 1, 'kind': 'notice', 'video_id': 'notice:' + key,
             'parts': [{'text': p, 'preview': False} for p in split_message(text)]}
    if photo_video_id:
        value['parts'] = [{'text': text, 'preview': False, 'photo': thumbnail_url(photo_video_id)}]
    if preview_video_id:
        value['parts'] = [{'text': text, 'preview': True, 'preview_url': canonical(preview_video_id)}]
    value['bundle_hash'] = digest(value)
    return validate_bundle(value)


class Telegram:
    def __init__(self, token=None, chat_id=None, session=None):
        self.token = token or os.environ.get('TELEGRAM_BOT_TOKEN', '')
        self.chat_id = chat_id or os.environ.get('TELEGRAM_CHAT_ID', '')
        if not re.fullmatch(r'\d+:[A-Za-z0-9_-]{20,}', self.token):
            raise Problem('TELEGRAM_TOKEN_MISSING_OR_INVALID')
        if not re.fullmatch(r'-?\d+', str(self.chat_id)):
            raise Problem('TELEGRAM_CHAT_MISSING_OR_INVALID')
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.target_hash = digest([str(self.chat_id), self.token.split(':')[0]])

    def send(self, part):
        if self.token in part['text']:
            raise Problem('SECRET_IN_MESSAGE')
        try:
            if part.get('photo'):
                method = 'sendPhoto'
                payload = {'chat_id': self.chat_id, 'photo': part['photo'], 'caption': part['text']}
            else:
                method = 'sendMessage'
                payload = {'chat_id': self.chat_id, 'text': part['text'],
                           'link_preview_options': {'is_disabled': not part['preview']}}
                if part.get('preview_url'):
                    payload['link_preview_options'].update(url=part['preview_url'],
                                                          prefer_large_media=True,
                                                          show_above_text=False)
            response = self.session.post(f'https://api.telegram.org/bot{self.token}/{method}',
                json=payload,
                timeout=(8, 25), allow_redirects=False)
            body = response.json()
            if (response.status_code == 200 and isinstance(body, dict) and body.get('ok')
                    and type(body.get('result', {}).get('message_id')) is int):
                return {'status': 'SENT', 'message_id': body['result']['message_id']}
            code = body.get('error_code', response.status_code) if isinstance(body, dict) else response.status_code
            if code == 429:
                wait = body.get('parameters', {}).get('retry_after', 60)
                supplied_wait = type(wait) is int and wait >= 1 and 'retry_after' in body.get('parameters', {})
                if type(wait) is not int or wait < 1:
                    wait = 60
                return {'status': 'RETRY', 'retry_at': time.time() + wait,
                        'reason': 'TELEGRAM_RATE_LIMITED',
                        'http_status': response.status_code,
                        'provider_error_code': code,
                        'provider_retry_after_seconds': wait if supplied_wait else None,
                        'retry_after_source': 'provider' if supplied_wait else 'local_default'}
            if code in (400, 401, 403, 404):
                return {'status': 'REJECTED', 'reason': f'TELEGRAM_HTTP_{code}'}
        except (requests.RequestException, ValueError, TypeError, AttributeError):
            pass
        # POST may have arrived even if its response was lost. Never blindly retry it.
        return {'status': 'UNCERTAIN', 'reason': 'TELEGRAM_DELIVERY_UNCERTAIN'}


class Outbox:
    def __init__(self, path, checkpoint=None):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.execute('CREATE TABLE IF NOT EXISTS deliveries ('
                        'video_id TEXT PRIMARY KEY, bundle_hash TEXT NOT NULL, '
                        'target_hash TEXT NOT NULL, parts TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS chat_limits ('
                        'target_hash TEXT PRIMARY KEY,retry_at REAL,failure_count INTEGER DEFAULT 0)')
        self.db.commit()
        self.checkpoint = checkpoint

    def close(self):
        self.db.close()

    def deliver(self, bundle, client, preview_fallback=False):
        bundle = validate_bundle(bundle)
        video_id = bundle['video_id']
        self.db.execute('BEGIN IMMEDIATE')
        row = self.db.execute('SELECT bundle_hash, target_hash, parts FROM deliveries WHERE video_id=?',
                              (video_id,)).fetchone()
        if row is None:
            parts = [{'status': 'PENDING'} for _ in bundle['parts']]
            self.db.execute('INSERT INTO deliveries VALUES (?,?,?,?)',
                            (video_id, bundle['bundle_hash'], client.target_hash, json.dumps(parts)))
        else:
            if row[1] != client.target_hash:
                self.db.rollback()
                raise Problem('DELIVERY_TARGET_CHANGED')
            parts = json.loads(row[2])
            if all(p['status'] == 'SENT' for p in parts):
                self.db.commit()
                return {'status': 'ALREADY_SENT', 'video_id': video_id}
            if row[0] != bundle['bundle_hash']:
                self.db.rollback()
                raise Problem('DELIVERY_CONTENT_CHANGED')
        self.db.commit()
        for index, part in enumerate(bundle['parts']):
            self.db.execute('BEGIN IMMEDIATE')
            parts = json.loads(self.db.execute('SELECT parts FROM deliveries WHERE video_id=?',
                                               (video_id,)).fetchone()[0])
            state = parts[index]
            if state['status'] == 'SENT':
                self.db.commit()
                continue
            if state['status'] in ('SENDING', 'UNCERTAIN'):
                self.db.commit()
                return {'status': 'UNCERTAIN', 'video_id': video_id, 'part': index + 1}
            if state.get('retry_at', 0) > time.time():
                self.db.commit()
                return {'status': 'WAITING_RETRY', 'video_id': video_id, 'retry_at': state['retry_at']}
            limit = self.db.execute('SELECT retry_at FROM chat_limits WHERE target_hash=?',
                                    (client.target_hash,)).fetchone()
            shared_retry = limit[0] if limit else 0
            for encoded, in self.db.execute('SELECT parts FROM deliveries WHERE target_hash=?',
                                            (client.target_hash,)):
                shared_retry = max(shared_retry, max((p.get('retry_at', 0) for p in json.loads(encoded)
                                                       if p['status'] == 'RETRY'), default=0))
            if shared_retry > time.time():
                parts[index] = {'status': 'RETRY', 'retry_at': shared_retry,
                                'reason': 'TELEGRAM_CHAT_RATE_LIMITED'}
                self.db.execute('UPDATE deliveries SET parts=? WHERE video_id=?', (json.dumps(parts), video_id))
                self.db.commit()
                if self.checkpoint:
                    self.checkpoint()
                return {'status': 'WAITING_RETRY', 'video_id': video_id, 'retry_at': shared_retry}
            # Retry only a definitively rejected preview after the shared pause.
            # Keep the reviewed text/hash unchanged; only Telegram's rendering option differs.
            without_preview = bool(preview_fallback and part.get('preview') and
                                   (state.get('preview_disabled_after_rate_limit') or
                                    (state['status'] == 'RETRY' and
                                     state.get('reason') == 'TELEGRAM_RATE_LIMITED')))
            send_part = dict(part)
            if without_preview:
                send_part['preview'] = False
                send_part.pop('preview_url', None)
            parts[index] = {'status': 'SENDING'}
            if without_preview:
                parts[index]['preview_disabled_after_rate_limit'] = True
            self.db.execute('UPDATE deliveries SET parts=? WHERE video_id=?', (json.dumps(parts), video_id))
            self.db.commit()
            if self.checkpoint:
                self.checkpoint()  # Must be durable before the external send.
            try:
                result = client.send(send_part)
            except Exception:  # noqa: BLE001 -- preserve uncertain delivery even for unexpected transport failures
                result = {'status': 'UNCERTAIN', 'reason': 'CLIENT_FAILED_AFTER_SEND_STARTED'}
            if without_preview:
                result = dict(result, preview_disabled_after_rate_limit=True)
            self.db.execute('BEGIN IMMEDIATE')
            if result['status'] == 'RETRY':
                previous = self.db.execute('SELECT failure_count FROM chat_limits WHERE target_hash=?',
                                           (client.target_hash,)).fetchone()
                failures = (previous[0] if previous else 0) + 1
                # Respect provider retry_after and increase pauses on repeated failures.
                retry = max(result.get('retry_at', 0), time.time() + min(3600, 60 * 2 ** min(failures-1, 6)))
                result = dict(result, retry_at=retry)
                self.db.execute('INSERT OR REPLACE INTO chat_limits VALUES (?,?,?)',
                                (client.target_hash, retry, failures))
            elif result['status'] == 'SENT':
                self.db.execute('DELETE FROM chat_limits WHERE target_hash=?', (client.target_hash,))
            parts[index] = result
            self.db.execute('UPDATE deliveries SET parts=? WHERE video_id=?', (json.dumps(parts), video_id))
            self.db.commit()
            if self.checkpoint:
                self.checkpoint()
            if result['status'] != 'SENT':
                return {'video_id': video_id, 'part': index + 1,
                        **{k: v for k, v in result.items() if k != 'message_id'}}
        return {'status': 'SENT', 'video_id': video_id, 'parts': len(parts)}

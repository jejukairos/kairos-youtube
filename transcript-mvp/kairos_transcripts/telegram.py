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
    if bundle.get('url') != canonical(bundle.get('video_id', '')):
        raise Problem('INVALID_DELIVERY_URL')
    if bundle.get('bundle_hash') != digest({k: v for k, v in bundle.items() if k != 'bundle_hash'}):
        raise Problem('DELIVERY_BUNDLE_CHANGED')
    if type(bundle.get('importance')) is not int or not 3 <= bundle['importance'] <= 5:
        raise Problem('FILTERED_LOW_IMPORTANCE')
    for key in ('transcript_hash', 'validated_summary_hash'):
        if not re.fullmatch(r'[a-f0-9]{64}', bundle.get(key, '')):
            raise Problem('INVALID_DELIVERY_HASH')
    parts = bundle.get('parts')
    if not isinstance(parts, list) or not 2 <= len(parts) <= 20:
        raise Problem('INVALID_DELIVERY_PARTS')
    for part in parts:
        if (not isinstance(part, dict) or not isinstance(part.get('text'), str)
                or not part['text'].strip() or units(part['text']) > 4096
                or type(part.get('preview')) is not bool):
            raise Problem('INVALID_DELIVERY_PART')
    return bundle


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
            response = self.session.post(f'https://api.telegram.org/bot{self.token}/sendMessage',
                json={'chat_id': self.chat_id, 'text': part['text'],
                      'link_preview_options': {'is_disabled': not part['preview']}},
                timeout=(8, 25), allow_redirects=False)
            body = response.json()
            if (response.status_code == 200 and isinstance(body, dict) and body.get('ok')
                    and type(body.get('result', {}).get('message_id')) is int):
                return {'status': 'SENT', 'message_id': body['result']['message_id']}
            code = body.get('error_code', response.status_code) if isinstance(body, dict) else response.status_code
            if code == 429:
                wait = body.get('parameters', {}).get('retry_after', 60)
                if type(wait) is not int or wait < 1:
                    wait = 60
                return {'status': 'RETRY', 'retry_at': time.time() + wait,
                        'reason': 'TELEGRAM_RATE_LIMITED'}
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
        self.db.commit()
        self.checkpoint = checkpoint

    def close(self):
        self.db.close()

    def deliver(self, bundle, client):
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
            parts[index] = {'status': 'SENDING'}
            self.db.execute('UPDATE deliveries SET parts=? WHERE video_id=?', (json.dumps(parts), video_id))
            self.db.commit()
            if self.checkpoint:
                self.checkpoint()  # Must be durable before the external send.
            try:
                result = client.send(part)
            except Exception:  # noqa: BLE001 -- preserve uncertain delivery even for unexpected transport failures
                result = {'status': 'UNCERTAIN', 'reason': 'CLIENT_FAILED_AFTER_SEND_STARTED'}
            self.db.execute('BEGIN IMMEDIATE')
            parts[index] = result
            self.db.execute('UPDATE deliveries SET parts=? WHERE video_id=?', (json.dumps(parts), video_id))
            self.db.commit()
            if self.checkpoint:
                self.checkpoint()
            if result['status'] != 'SENT':
                return {'video_id': video_id, 'part': index + 1,
                        **{k: v for k, v in result.items() if k != 'message_id'}}
        return {'status': 'SENT', 'video_id': video_id, 'parts': len(parts)}

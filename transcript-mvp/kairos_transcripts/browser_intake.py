"""Loopback-only intake for complete transcripts read through normal browser UI.

No remote fetching, browser cookies, AI, Telegram, or credentials are used here.
"""
import argparse
import hmac
import json
import math
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from .collector import canonical, normalize_transcript, parse_youtube_text, save_transcript
from .common import Problem, read_json
from .monitor import Monitor


def text_checksum(text):
    value = 2166136261
    encoded = text.encode('utf-16-le')
    for i in range(0, len(encoded), 2):
        value = ((value ^ int.from_bytes(encoded[i:i+2], 'little')) * 16777619) & 0xffffffff
    return f'{value:08x}'


def save_browser_capture(root, value):
    root = Path(root)
    if not isinstance(value, dict):
        raise Problem('INVALID_BROWSER_CAPTURE')
    vid = value.get('video_id', '')
    canonical(vid)
    rows = value.get('rows')
    if (not isinstance(rows, list) or not rows or type(value.get('dom_count')) is not int
            or len(rows) != value['dom_count']):
        raise Problem('INCOMPLETE_BROWSER_CAPTURE')
    if any(not isinstance(row, dict) or not isinstance(row.get('time'), str)
           or not isinstance(row.get('text'), str) or not row['text'].strip() for row in rows):
        raise Problem('INVALID_BROWSER_CAPTURE_ROW')
    raw = '\n'.join(f"[{row['time']}] {row['text']}" for row in rows)
    checksum = text_checksum(raw)
    if checksum != value.get('text_checksum'):
        raise Problem('BROWSER_CAPTURE_CHECKSUM_MISMATCH')
    segments = parse_youtube_text(raw)
    duration = value.get('duration_seconds')
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
        raise Problem('BROWSER_VIDEO_DURATION_REQUIRED')
    if segments[-1]['start'] > duration + 5 or duration - segments[-1]['start'] > max(90, duration * .2):
        raise Problem('TRANSCRIPT_END_NOT_CONFIRMED')
    for key in ('title', 'channel'):
        if not isinstance(value.get(key), str) or not value[key].strip() or len(value[key]) > 1000:
            raise Problem('BROWSER_VIDEO_METADATA_REQUIRED')
    language = value.get('language', 'und')
    if not isinstance(language, str) or not re.fullmatch(r'[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*', language):
        raise Problem('INVALID_TRANSCRIPT_LANGUAGE')
    app = Monitor(root)
    try:
        row = app.db.execute('SELECT * FROM videos WHERE video_id=?', (vid,)).fetchone()
        if not row or row['status'] not in ('PENDING', 'CAPTION_WAIT', 'CAPTIONS_READY'):
            raise Problem('VIDEO_NOT_AVAILABLE_FOR_BROWSER_IMPORT')
        app.require_selected(row)
        if value.get('channel_id') != row['channel_id']:
            raise Problem('BROWSER_CAPTURE_CHANNEL_MISMATCH')
    finally:
        app.close()
    item = normalize_transcript(dict(video_id=vid, title=value['title'], channel=value['channel'],
        channel_id=value['channel_id'], source='browser-visible-script', captions_language=language,
        captions_generated=None, segments=segments, source_capture={
            'dom_count':len(rows), 'display_duration_seconds':duration,
            'text_fnv1a_utf16':checksum, 'text_characters':len(raw),
            'first':rows[0]['time'], 'last':rows[-1]['time']}))
    path = root / 'transcripts' / f'{vid}.json'
    if path.is_file() and read_json(path).get('transcript_hash') != item['transcript_hash']:
        raise Problem('EXISTING_TRANSCRIPT_CHANGED_REVIEW_REQUIRED')
    save_transcript(root/'transcripts', item)
    (root/'transcripts'/f'{vid}.browser.txt').write_text(raw+'\n', encoding='utf-8')
    return {'status':'SAVED', 'video_id':vid, 'segments':len(rows),
            'characters':len(item['full_text']), 'text_checksum':checksum}


class IntakeServer(ThreadingHTTPServer):
    def __init__(self, root, port=0):
        super().__init__(('127.0.0.1', port), IntakeHandler)
        self.root = Path(root).resolve()
        self.nonce = secrets.token_urlsafe(32)
        self.write_lock = threading.Lock()
        self.origin = f'http://127.0.0.1:{self.server_port}'


class IntakeHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # Do not log submitted captions or request bodies.

    def respond(self, code, body, content_type='text/html; charset=utf-8'):
        encoded = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(encoded)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Security-Policy', "default-src 'none'; form-action 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        if self.path != '/' or self.headers.get('Host') != self.server.origin.removeprefix('http://'):
            return self.respond(404, 'Not found')
        self.respond(200, '<!doctype html><html lang="ko"><meta charset="utf-8">'
            '<title>카이로스 로컬 자막 저장</title><h1>카이로스 로컬 자막 저장</h1>'
            '<p>공개 영상 화면에서 확인한 전체 자막만 이 PC에 저장합니다.</p>'
            '<form method="post" action="/capture">'
            f'<input type="hidden" name="nonce" value="{self.server.nonce}">'
            '<label for="payload">전체 자막 JSON</label><br>'
            '<textarea id="payload" name="payload" rows="10" cols="90" required></textarea><br>'
            '<button type="submit">검증하고 저장</button></form></html>')

    def do_POST(self):
        if (self.path != '/capture' or self.headers.get('Origin') != self.server.origin
                or self.headers.get('Host') != self.server.origin.removeprefix('http://')):
            return self.respond(403, 'Forbidden')
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 4 * 1024 * 1024:
                raise Problem('BROWSER_CAPTURE_SIZE_LIMIT')
            fields = parse_qs(self.rfile.read(size).decode('utf-8'))
            if not hmac.compare_digest(fields.get('nonce', [''])[0], self.server.nonce):
                raise Problem('LOCAL_CAPTURE_SESSION_MISMATCH')
            payload = json.loads(fields.get('payload', [''])[0])
            with self.server.write_lock:
                result = save_browser_capture(self.server.root, payload)
            self.respond(200, json.dumps(result, ensure_ascii=True), 'application/json')
        except (Problem, ValueError, TypeError, KeyError) as error:
            reason = error.code if isinstance(error, Problem) else 'INVALID_BROWSER_CAPTURE'
            self.respond(400, json.dumps({'status':'FAILED', 'reason':reason}), 'application/json')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    server = IntakeServer(args.root, args.port)
    print(json.dumps({'status':'LOCAL_INTAKE_READY', 'url':server.origin}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()

"""Manual GitHub Actions transport. No collection or AI, no secrets in arguments/logs."""
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from kairos_transcripts.common import Problem
from kairos_transcripts.telegram import Outbox, Telegram, validate_bundle

BRANCH = 'kairos-youtube-state'
FILE = 'transcript_delivery.sqlite3'


class RemoteState:
    def __init__(self, repo, path):
        self.repo, self.path, self.sha = repo, path, None
        self.last_write = 0

    def api(self, endpoint, payload=None, optional=False):
        args = ['gh', 'api', f'repos/{self.repo}/{endpoint}']
        if payload is not None:
            args += ['--method', 'PUT', '--input', '-']
        result = subprocess.run(args, input=json.dumps(payload) if payload is not None else None,
                                text=True, encoding='utf-8', capture_output=True, check=False)
        if result.returncode:
            if optional and '(HTTP 404)' in result.stderr:
                return None
            raise Problem('REMOTE_STATE_ACCESS_FAILED')
        try:
            return json.loads(result.stdout)
        except ValueError:
            raise Problem('INVALID_REMOTE_STATE_RESPONSE') from None

    def restore(self):
        value = self.api(f'contents/{FILE}?ref={BRANCH}', optional=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if value:
            self.sha = value['sha']
            self.path.write_bytes(base64.b64decode(value['content']))

    def legacy_sent(self, video_id):
        value = self.api(f'contents/state.json?ref={BRANCH}')
        state = json.loads(base64.b64decode(value['content']))
        return state.get('videos', {}).get(video_id, {}).get('status') == 'sent'

    def save(self):
        if time.monotonic() - self.last_write < 11:
            time.sleep(11 - (time.monotonic() - self.last_write))
        content = self.path.read_bytes()
        if len(content) > 8 * 1024 * 1024:
            raise Problem('REMOTE_STATE_SIZE_LIMIT')
        payload = {'branch': BRANCH, 'message': 'Checkpoint manual transcript delivery',
                   'content': base64.b64encode(content).decode('ascii')}
        if self.sha:
            payload['sha'] = self.sha
        value = self.api(f'contents/{FILE}', payload)
        self.sha = value['content']['sha']
        self.last_write = time.monotonic()


def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REPOSITORY') != 'jejukairos/kairos-youtube':
        raise Problem('MANUAL_GITHUB_WORKFLOW_REQUIRED')
    encoded = os.environ.get('DELIVERY_BUNDLE_BASE64', '')
    if len(encoded) > 60000:
        raise Problem('DELIVERY_INPUT_TOO_LARGE')
    try:
        bundle = validate_bundle(json.loads(base64.b64decode(encoded, validate=True)))
    except (ValueError, TypeError):
        raise Problem('INVALID_DELIVERY_INPUT') from None
    remote = RemoteState(os.environ['GITHUB_REPOSITORY'], Path('state/manual-delivery.sqlite3'))
    if bundle.get('kind') != 'notice' and remote.legacy_sent(bundle['video_id']):
        print(json.dumps({'status': 'ALREADY_SENT_LEGACY', 'video_id': bundle['video_id']}))
        return 0
    remote.restore()
    outbox = Outbox(remote.path, checkpoint=remote.save)
    try:
        result = outbox.deliver(bundle, Telegram(), preview_fallback=True)
    finally:
        outbox.close()
    print(json.dumps(result))
    return 0 if result['status'] in ('SENT', 'ALREADY_SENT') else 2


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Problem as error:
        print(json.dumps({'status': 'FAILED', 'reason': error.code}), file=sys.stderr)
        raise SystemExit(2) from None
    except (OSError, ValueError, KeyError, TypeError):
        print('{"status":"FAILED","reason":"INVALID_LOCAL_OR_REMOTE_STATE"}', file=sys.stderr)
        raise SystemExit(2) from None

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from kairos_transcripts.common import Problem, read_json
from kairos_transcripts.monitor import Monitor
from kairos_transcripts.summary import evidence_packet


def main():
    p = argparse.ArgumentParser(description='32개 채널 공개 피드 감시; Google·유료 AI API 없음')
    sub = p.add_subparsers(dest='command', required=True)
    discover = sub.add_parser('discover')
    discover.add_argument('--until-now', action='store_true')
    queue = sub.add_parser('queue')
    queue.add_argument('--limit', type=int, default=8)
    for name in ('collect', 'packet', 'dispatch'):
        sub.add_parser(name).add_argument('video_id')
    sub.add_parser('recover-gap').add_argument('channel_id')
    notice = sub.add_parser('notice')
    notice.add_argument('--key', required=True)
    notice.add_argument('--message-file', type=Path, required=True)
    review = sub.add_parser('review')
    review.add_argument('video_id')
    review.add_argument('draft', type=Path)
    sub.add_parser('sync')
    sub.add_parser('status')
    plan = sub.add_parser('plan-backlog')
    plan.add_argument('--id', required=True)
    plan.add_argument('--starts', nargs=3, required=True)
    args = p.parse_args()
    root = Path(__file__).resolve().parent
    app = Monitor(root)
    try:
        if args.command == 'discover':
            result = app.discover(datetime.now(UTC) if args.until_now else None)
        elif args.command == 'queue':
            result = app.queue(args.limit)
        elif args.command == 'collect':
            result = app.collect(args.video_id)
        elif args.command == 'packet':
            print(evidence_packet(read_json(root / 'transcripts' / f'{args.video_id}.json')))
            return 0
        elif args.command == 'review':
            result = app.review(args.video_id, args.draft)
        elif args.command == 'dispatch':
            result = app.dispatch(args.video_id)
        elif args.command == 'sync':
            result = app.sync()
        elif args.command == 'recover-gap':
            result = app.recover_gap(args.channel_id)
        elif args.command == 'notice':
            result = app.notice(args.key, args.message_file.read_text(encoding='utf-8'))
        elif args.command == 'plan-backlog':
            result = app.create_backlog_plan(args.id, args.starts)
        else:
            result = app.stats()
        print(json.dumps(result, ensure_ascii=True))
        return 0
    finally:
        app.close()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Problem as error:
        print(json.dumps({'status':'FAILED', 'reason':error.code}), file=sys.stderr)
        raise SystemExit(2) from None

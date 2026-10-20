import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from .collector import collect_batch, extract_id, normalize_transcript, save_transcript
from .common import Problem, now, read_json, save_json, save_text
from .summary import (
    delivery_bundle,
    duplicate_transcript,
    evidence_packet,
    generate_local,
    render_summary,
    template,
    validate_summary,
)
from .telegram import Outbox, Telegram, load_env, validate_bundle


def main(argv=None):
    parser = argparse.ArgumentParser(description='Kairos 무료 자막 수집·투자 요약·Telegram')
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('collect', help='여러 URL의 공개 자막 수집')
    p.add_argument('urls', nargs='*')
    p.add_argument('--urls-file', type=Path)
    p.add_argument('--output-dir', type=Path, default=Path('transcripts'))
    p.add_argument('--force', action='store_true')
    p.add_argument('--backend', choices=['transcript-api', 'yt-dlp'], default='transcript-api')
    p.add_argument('--fallback-yt-dlp', action='store_true')
    p.add_argument('--sleep', type=float, default=1.5)
    p = commands.add_parser('import-transcript', help='사용자가 가진 JSON/SRT/VTT 가져오기')
    p.add_argument('file', type=Path)
    p.add_argument('--video-url', required=True)
    p.add_argument('--title', required=True)
    p.add_argument('--channel', default='사용자 입력')
    p.add_argument('--language', default='ko')
    p.add_argument('--output-dir', type=Path, default=Path('transcripts'))
    for name, help_text in [('prepare', '전체 자막과 요약 양식 내보내기'),
                            ('summarize', '로컬 AI만 사용; 모델이 없으면 SKIPPED'),
                            ('review', '작성한 요약의 근거 검사 및 발송 파일 생성')]:
        p = commands.add_parser(name, help=help_text)
        p.add_argument('transcript', type=Path)
        p.add_argument('--output-dir', type=Path, default=Path('summaries'))
        if name == 'summarize':
            p.add_argument('--model', help='이미 설치된 로컬 Ollama 모델명')
        if name == 'review':
            p.add_argument('summary', type=Path)
    p = commands.add_parser('send', help='검사 완료 발송 파일; 기본은 미리보기')
    p.add_argument('bundle', type=Path)
    p.add_argument('--send', action='store_true', help='실제 Telegram 발송')
    p.add_argument('--state', type=Path, default=Path('state/delivery.sqlite3'))
    args = parser.parse_args(argv)
    try:
        if args.command == 'collect':
            values = list(args.urls)
            if args.urls_file:
                values += args.urls_file.read_text(encoding='utf-8-sig').splitlines()
            if not values and Path('urls.txt').is_file():
                values = Path('urls.txt').read_text(encoding='utf-8-sig').splitlines()
            if not values:
                raise Problem('URL_INPUT_REQUIRED')
            results = collect_batch(values, args.output_dir, args.force, args.backend,
                                    args.fallback_yt_dlp, args.sleep)
            print(json.dumps({'status': 'COLLECTION_COMPLETE',
                              'counts': dict(Counter(r['status'] for r in results))}))
            return 0 if results and all(r['status'] in ('OK', 'CACHED') for r in results) else 2
        if args.command == 'import-transcript':
            from .collector import parse_subtitle
            video_id = extract_id(args.video_url)
            if not video_id:
                raise Problem('INVALID_URL')
            if args.file.suffix.lower() == '.json':
                source = read_json(args.file)
                segments = source.get('segments') if isinstance(source, dict) else source
            elif args.file.suffix.lower() in ('.srt', '.vtt'):
                segments = parse_subtitle(args.file.read_text(encoding='utf-8-sig'), args.file.suffix[1:])
            else:
                raise Problem('IMPORT_REQUIRES_JSON_SRT_VTT_WITH_TIMESTAMPS')
            item = save_transcript(args.output_dir, {'video_id': video_id, 'segments': segments,
                                   'title': args.title, 'channel': args.channel,
                                   'captions_language': args.language, 'source': 'user_import'})
            result = {'status': 'IMPORTED', 'video_id': item['video_id']}
        elif args.command in ('prepare', 'summarize', 'review'):
            item = normalize_transcript(read_json(args.transcript))
            target = args.output_dir / item['video_id']
            if args.command == 'prepare':
                save_text(target.with_suffix('.packet.txt'), evidence_packet(item))
                save_json(target.with_suffix('.template.json'), template(item))
                result = {'status': 'PREPARED', 'video_id': item['video_id']}
            else:
                if args.command == 'summarize':
                    if not args.model:
                        result = {'status': 'SKIPPED', 'reason': 'NO_LOCAL_AI_MODEL',
                                  'video_id': item['video_id'], 'checked_at_utc': now()}
                        save_json(target.with_suffix('.status.json'), result)
                        print(json.dumps(result))
                        return 0
                    value = generate_local(item, args.model)
                else:
                    value = validate_summary(read_json(args.summary), item)
                    value.setdefault('provider', 'owner-reviewed-import')
                duplicate = duplicate_transcript(item, args.transcript.parent)
                if duplicate:
                    value['duplicate_of'] = duplicate
                save_json(target.with_suffix('.summary.json'), value)
                save_text(target.with_suffix('.summary.txt'), render_summary(value, item))
                if value['importance'] < 3 or value['simple_content'] or value.get('duplicate_of'):
                    result = {'status': 'FILTERED', 'video_id': item['video_id']}
                    stale = target.with_suffix('.delivery.json')
                    if stale.is_file():
                        stale.unlink()
                else:
                    bundle = delivery_bundle(value, item)
                    save_json(target.with_suffix('.delivery.json'), bundle)
                    result = {'status': 'READY', 'video_id': item['video_id'], 'parts': len(bundle['parts'])}
        else:
            bundle = validate_bundle(read_json(args.bundle))
            if not args.send:
                result = {'status': 'DRY_RUN', 'video_id': bundle['video_id'], 'parts': len(bundle['parts'])}
            else:
                load_env()
                outbox = Outbox(args.state)
                try:
                    result = outbox.deliver(bundle, Telegram())
                finally:
                    outbox.close()
        print(json.dumps(result, ensure_ascii=True))
        return 0 if result['status'] in ('IMPORTED', 'PREPARED', 'READY', 'FILTERED',
                                         'DRY_RUN', 'SENT', 'ALREADY_SENT') else 2
    except Problem as error:
        print(json.dumps({'status': 'SKIPPED' if args.command == 'summarize' else 'FAILED',
                          'reason': error.code}), file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({'status': 'FAILED', 'reason': 'INVALID_OR_UNREADABLE_LOCAL_FILE'}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

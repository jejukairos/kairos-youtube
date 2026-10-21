# Kairos 자막 수집 → 투자 요약 → Telegram MVP

**현재 운영(2026-10-21): 32개 채널의 Codex 자막 요약 예약을 활성화했습니다.** 한국 시간 11:30·15:00·20:00·23:00에
같은 채팅에서 공개 피드 확인 → 무료 자막 → Codex 요약 → Telegram 순서로 실행합니다. 이전 Gemini/Data API 예약은 계속 중지입니다.
로컬 PC와 앱이 켜져 있어야 하며 계정 사용 한도를 사용합니다. 실제 검증·남은 대기·예약 등록은 [검증 기록](docs/VALIDATION.md)을 보세요.
운영 에이전트는 [OPERATIONS](OPERATIONS.md)를 따릅니다. 아래 CLI의 summarize는 별도 로컬 모델 선택지이며 예약 실행에는 필요하지 않습니다.

먼저 [쉬운 사용법](QUICKSTART.md)을 보세요.

2026-10-20 Windows 개발 PC에서 `L2GG_DeUDa0`, `n6nO1MkYId0`, `QslXeEZ0tPI` 공개 한국어 자막 수집에 성공했습니다.
대체 경로 `yt-dlp`도 첫 영상 자막 수집에 성공했습니다. 영상·음성 파일은 다운로드하지 않았습니다.
실제 결과와 제한은 [검증 기록](docs/VALIDATION.md)에 남깁니다.

**CLI 단독으로 AI 요약하는 환경은 아닙니다.** 이 PC에는 Ollama/Whisper가 설치되어 있지 않습니다.
AI 모델이 없으면 `SKIPPED`로 보고하고 자막을 보존합니다. 현재 가능한 경로는 무료 자막 수집 →
기존 대화 또는 예약된 Codex 실행에서 요약 → 근거 검사 → 승인된 지정 Telegram 발송입니다. 이 대화의 요약도 계정 사용 한도를 사용합니다.
YouTube Data API·Gemini·OpenAI API·유료 프록시·유료 전사 서비스는 호출하지 않습니다.

## 설치 및 명령

Python 3.10+; 실제 검증 Python 3.12. Windows/macOS/Linux CLI를 지원합니다.

```sh
python -m venv .venv
# Windows: .venv/Scripts/python.exe
# macOS/Linux: .venv/bin/python
python -m pip install -r requirements.txt
# 선택 사항: 자막 전용 대체 경로
python -m pip install -r requirements-optional.txt

python batch_transcripts.py --urls-file urls.txt --output-dir transcripts
python kairos_cli.py collect https://youtu.be/L2GG_DeUDa0 https://youtu.be/n6nO1MkYId0
python kairos_cli.py collect --urls-file urls.txt --backend yt-dlp --output-dir transcripts-ytdlp
python kairos_cli.py prepare transcripts/L2GG_DeUDa0.json
python kairos_cli.py summarize transcripts/L2GG_DeUDa0.json
# 모델이 없다면 위 명령은 SKIPPED. 아래는 이미 설치한 로컬 모델만 허용합니다.
python kairos_cli.py summarize transcripts/L2GG_DeUDa0.json --model YOUR_INSTALLED_LOCAL_MODEL
python kairos_cli.py review transcripts/L2GG_DeUDa0.json summaries/L2GG_DeUDa0.reviewed-draft.json
python kairos_cli.py send summaries/L2GG_DeUDa0.delivery.json
python kairos_cli.py send summaries/L2GG_DeUDa0.delivery.json --send
python -m unittest discover -s tests -v
```

가상환경 생성 뒤에는 해당 환경의 Python 실행 경로를 사용하세요. Windows 더블클릭 수집은 `collect-captions.cmd`입니다.
핵심 의존성만 설치한 환경에서는 선택적 yt-dlp 테스트를 위해 `requirements-optional.txt`도 설치해야 합니다.
`--fallback-yt-dlp`는 제한되지 않은 일반 자막 오류에만 대체 경로를 사용합니다.
IP 차단·인증·연령·지역 제한은 대체 경로로 우회하지 않습니다.

소유한 SRT/VTT 또는 `segments: [{start,duration,text}]` JSON 가져오기:

```sh
python kairos_cli.py import-transcript captions.srt --video-url https://youtu.be/L2GG_DeUDa0 --title "영상 제목"
```

일반 TXT는 시간 정보를 새로 추측해 붙이지 않습니다. 일반 TXT는 이 대화에서 요약할 수 있으며,
프로그램의 시간 근거 검사에는 SRT/VTT/JSON을 사용하세요.

## 요약 구조와 검사 범위

`prepare`는 자막 전체가 들어 있는 `.packet.txt`와 `.template.json`을 만듭니다.
작성한 요약의 각 문단은 `kind`, `text`, `evidence: [{segment, quote}]`를 갖습니다.
`segment`는 원본 JSON 배열의 0부터 시작하는 번호입니다. 해시로 원본 버전을 묶고 실제 자막에
인용이 있는지, 숫자가 인용 구간에 존재하는지, 최소 세 구간 근거가 있는지 검사합니다.
사실·발언·전망·조건·위험·확인할 데이터·영상 시점·요약자 해석을 구분합니다.
검증된 외부 사실을 뜻하는 `verified_fact`는 허용하지 않습니다.
하워드 막스 관점의 해석은 요약자의 사고틀 적용이며 실제 막스 발언으로 표시하지 않습니다.

이 검사는 문단과 인용의 의미적 일치, 숫자의 단위·부호, 원본 발언의 사실 여부까지 자동 증명하지 않습니다.
자동자막의 인명·수치 오인식, 누락, 화면 정보는 사람이 검토해야 합니다. 원문 근거 없이 수치를 고치지 않습니다.
원문이 완전히 같은 영상은 추가 발송을 막고, 의미가 유사한 다른 영상은 검토자가 `duplicate_of`로 표시합니다.

## 로컬 AI와 오프라인 전사

Ollama는 고정 주소 `127.0.0.1:11434`의 이미 설치된 로컬 모델만 선택할 수 있습니다.
클라우드 모델·원격 호스트·자동 모델 다운로드는 지원하지 않습니다. 입력이 안전 크기를 넘으면
자막을 잘라 요약하지 않고 수동 검토 자료로 넘깁니다. 현재 PC에서는 모델 생성 품질을 실검증하지 않았습니다.

자막이 없을 때 faster-whisper/Whisper는 별도 선택지로만 설계합니다. 지금 자동 실행하지 않습니다.
본인이 사용 권한을 가진 로컬 음성, CPU/GPU·메모리·디스크 여유, 모델 라이선스와 다운로드 크기를 먼저 확인해야 합니다.
미디어 자동 다운로드·클라우드 전사는 구현하지 않았습니다. 이후 전사 결과를 시간 포함 JSON/SRT로 가져올 수 있습니다.

## Telegram과 저장

비밀값은 `.env`/환경변수의 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`만 사용합니다.
숫자 Chat ID 한 곳만 지정하며 로그에는 값·원시 오류 응답·토큰 포함 URL을 출력하지 않습니다.
URL 카드 다음에 요약을 보냅니다. 본문은 UTF-16 기준 4096 이내로 나누며 기본은 미리보기입니다.

SQLite outbox에 전송 시작과 메시지 ID를 기록해 부분 성공 후 재시도를 복구합니다.
429는 제공된 대기 시각을 보존하고 다음 명시적 실행에서 재시도합니다.
네트워크 타임아웃·서버 오류·응답 유실·중간 종료는 `UNCERTAIN`으로 차단합니다.
Telegram Bot API에 멱등 키가 없어 도착 불확실 메시지를 무조건 자동 재전송하면 중복될 수 있기 때문입니다.
`state/delivery.sqlite3`는 발송 기록이므로 지우거나 다른 PC로 복제하지 말고 일관되게 보관하세요.
로컬과 GitHub 수동 발송은 같은 상태 사본을 관리해야 합니다. 전달 경로를 바꿀 때는 최신 outbox를
동기화하고 두 경로를 동시에 사용하지 마세요. 이번 실제 발송 기록은 로컬에도 동기화했습니다.

기존 저장소의 수동 전용 workflow는 검토 완료 발송 묶음만 받습니다. AI·YouTube 수집·예약 이벤트가 없습니다.
기존 state branch에 SQLite를 전송 전후 체크포인트하여 실행기가 사라져도 중복을 차단하며,
이전 Gemini 자동화의 `sent` 기록도 읽어 같은 영상의 재전송을 막습니다. 이전 예약 workflow는 활성화하지 않습니다.
전송 묶음의 해시는 변조 감지용이며 작성자의 신원이나 자막 사실을 인증하는 전자서명이 아닙니다.

자막·요약·상태·환경 파일은 기본 Git 제외입니다. 원문 자막을 외부에 재배포하지 않습니다.
클라우드 공개 IP에서는 수집이 차단될 수 있어 무료·무중단·무제한 수집을 보장하지 않습니다.

자료: [자막 라이브러리](https://github.com/jdepoix/youtube-transcript-api),
[yt-dlp](https://github.com/yt-dlp/yt-dlp), [Telegram](https://core.telegram.org/bots/api#sendmessage),
[Ollama](https://docs.ollama.com/api/generate).

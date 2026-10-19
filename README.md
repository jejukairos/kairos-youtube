# Kairos YouTube Intelligence v4.3

32개 공개 YouTube 채널의 새 영상을 Gemini Developer API로 분석해 개인 Telegram으로 전달합니다. **코드·Mock 검증·공개 YouTube 조회·Telegram 연결 및 실제 테스트 발송을 완료했으며, 실제 Gemini 영상 분석과 예약 실행은 아직 완료되지 않았습니다.**

운영 준비 저장소: https://github.com/jejukairos/kairos-youtube . `GEMINI_API_KEY` Secret 등록과 GitHub 인증을 확인했습니다. Secret 값은 읽지 않았으며, 키 교체 여부와 무료 프로젝트 확인 기록의 연결 검증 전에는 실제 AI 호출을 차단합니다. `KAIROS_ENABLED`는 아직 활성화하지 않았습니다.

기존 뉴스용 `kairos` 및 다른 수집 프로젝트와 독립된 프로그램입니다. 이 폴더의 **내용을 전용 GitHub 저장소의 루트에 배포**해야 `.github/workflows`가 작동합니다. 상위 작업 폴더 전체를 업로드하지 마세요.

## 구현

- `config/channels.yaml`: 요청받은 32개 URL, 실제 Channel ID, 공식 채널 페이지 검증 시각. YAML 1.2에서 유효한 JSON 형식으로 저장해 추가 라이브러리가 필요 없습니다.
- 공식 공개 업로드 피드로 매번 전체 채널 확인. 초기 실행 시각 이전 영상은 제외합니다.
- 피드의 최신 15개만으로 장애 기간을 복구할 수 없으면 누락 경고와 원래 조회 기준시각을 보존합니다. 별도 `YOUTUBE_API_KEY`가 있으면 공식 업로드 재생목록 API를 페이지별로 조회합니다.
- 영상 길이·공개 재생 여부 확인 후 실제 YouTube URL을 Gemini에 `fileData`로 전달합니다. 제목·설명만으로 요약하지 않습니다.
- 초반·중반·후반의 시각 근거, 길이 일치, 전체 접근 여부, JSON 구조를 검사합니다. 불충분·잘림·비공개·길이 미확인 응답은 완료 처리하지 않습니다. 모델의 자기 보고와 시각 근거 검사가 실제 시청을 독립적으로 증명하지는 않습니다. 실제 품질은 연결 후 영상 1개로 검증해야 합니다.
- 보통 5~9개 핵심 문단, 정보가 많으면 최대 15개. `📊 제목`, `▶️ 문단`, `🔗 원본 URL`. 숫자·기간·단위·주장과 사실의 구분·전망 조건·영상에서 언급된 위험을 보존하도록 요청합니다.
- `video_id`로 분석·발송 중복 방지. 요약 저장 후 Telegram 실패 시 요약을 재생성하지 않습니다.
- Telegram 길이는 UTF-16 단위로 제한하고 긴 요약을 분할합니다. 지정한 숫자 Chat ID 한 곳에만 전송합니다.
- 일일 요청·분당 요청·분당 토큰·자체 일일 토큰 예산·영상 시간·YouTube 할당량·실행 시간·상태 저장 크기를 검사합니다.
- 실패한 요청도 무료 예산을 예약한 상태로 보존합니다. 유료 모델·검색·배치·외부 저장 서비스로 전환하지 않습니다.
- 인증 실패는 영속 중단 상태로 기록합니다. 키를 교체하고 무료 확인 정보를 갱신해야 재개합니다. Google 비밀번호·쿠키·브라우저 인증을 사용하지 않습니다.

## 로컬 실행

Python 3.12 이상이 필요합니다. Linux에는 시스템 시간대 데이터가 기본 제공됩니다. Windows에서는 `America/Los_Angeles` 시간대가 없을 경우 공식 Python `tzdata` 패키지가 필요합니다. 현재 개발 PC에는 데이터가 있어 추가 설치 없이 검증했습니다.

```powershell
cd kairos-youtube
python -m unittest discover -s tests -v
python -m kairos_youtube status
python -m kairos_youtube probe --sample-duration
python -m kairos_youtube discover
```

`probe`는 **키를 읽거나 Gemini·Telegram을 호출하지 않고** 공식 공개 피드·공개 영상 메타데이터만 검사합니다. `discover`도 Gemini·Telegram을 호출하지 않습니다. `run`은 안전 조건을 검사한 후 분석과 발송을 수행합니다.

```powershell
python -m kairos_youtube run
```

키가 없으면 조회와 대기열 보존을 계속하고 `gemini_key_missing`으로 분석을 차단합니다. 운영 환경에서는 Secrets를 환경변수로 주입하며, 키를 명령행 인자로 전달하지 않습니다.

설치 및 배포: [안전한 설정 절차](docs/SETUP.md). 검증 결과: [VALIDATION](docs/VALIDATION.md), [실제 공개 피드 검사 기록](docs/public-probe.json).

## 비용 정책

```text
FREE_ONLY=true
MAX_SPEND_USD=0
ALLOW_PAID_FALLBACK=false
AI_PROVIDER=gemini
```

이 설정은 프로그램 정책이며 **Google 결제를 기술적으로 차단하는 장치가 아닙니다.** 2026-10-19에 실제 로그인된 AI Studio에서 대상 프로젝트의 **무료 등급**, 결제 화면의 **결제 계정 없음**을 확인했습니다. 현재 Gemini 3.8 Flash 표시 한도는 **RPM 5 / TPM 250,000 / RPD 20**입니다. 비공개 프로젝트 식별자가 포함된 관측 기록은 Git 제외 경로 `state/free-tier-observation.json`에만 보관합니다. 이 관측은 새 키 폐기·교체·등록 완료를 뜻하지 않습니다. API 키만으로 Cloud Billing 상태나 그 키가 속한 프로젝트를 인증해 조회할 수 있다고 가정하지 않습니다.

새 키·키 교체 확인·프로젝트 ID·무료 등급·결제 미연결·전용 프로젝트 여부·실제 한도·키 지문이 일치하는 `FREE_TIER_CONFIRMATION`이 없으면 실제 Google 호출을 차단합니다. 확인은 최대 7일 동안 유효하며, 공식 가격 검토는 30일 뒤 만료됩니다. 만료 시 대기열은 보존되고 분석은 중단됩니다. 갱신은 수동 확인이며, 정상 실행 중 Google 계정에 자동 로그인하지 않습니다. 다른 프로그램과 같은 프로젝트를 공유하면 이 프로그램의 기록만으로 전체 사용량을 통제할 수 없어 전용 무료 프로젝트를 요구합니다.

2026-10-19에 공식 가격·모델·영상 문서를 확인하여 `gemini-3.8-flash`를 선택했습니다. 무료 입력·출력 및 동영상 입력이 문서에 명시된 고정 모델입니다. 실제 계정의 모델 제공 여부는 안전 조건 통과 후 `models.get`으로 다시 확인합니다. 자동 모델 변경은 없습니다. [가격](https://ai.google.dev/gemini-api/docs/pricing), [모델](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash).

무료 공개 YouTube URL 입력은 미리보기이며 하루 8시간 제한입니다. 초기화 시각이 영상 기능 문서에 명시되지 않아 프로그램은 **최근 24시간 합산 8시간**을 더 엄격하게 적용합니다. `countTokens`에서 영상 처리가 발생할 가능성까지 고려해 토큰 계산과 요약 각각의 영상 시간을 예약합니다. 따라서 실제 처리 가능한 고유 영상 시간은 8시간보다 작을 수 있습니다. [영상 입력](https://ai.google.dev/gemini-api/docs/video-understanding).

RPM·TPM·RPD는 계정별 AI Studio 실제 한도를 입력해야 합니다. RPD는 미국 태평양 자정에 초기화되며 서머타임을 반영합니다. 문서에 고정된 공통 무료 요청 수를 임의로 넣지 않습니다. 일일 토큰 예산은 실제 TPD가 있으면 그 이하로, 없으면 사용자가 정한 보수적인 상한으로 설정합니다. 요청 전에 동일 입력의 `countTokens` 결과와 출력 최대치 8,192 토큰을 예약합니다. 실패해도 차감 기록을 되돌리지 않으며, 동일 입력의 토큰 계산은 24시간 캐시합니다. [한도](https://ai.google.dev/gemini-api/docs/rate-limits), [토큰 API](https://ai.google.dev/api/tokens).

## 예약·영속 상태

`30 3,12 * * *`: 매일 12:30 / 21:30 KST. GitHub가 예약 실행을 지연할 수 있어 정각 발송을 보장하지는 않습니다.

추가 비용을 피하기 위해 워크플로는 **공개 저장소의 표준 Ubuntu 러너에서만** 실행됩니다. 비공개 저장소에서는 러너 할당 전에 중단됩니다. 공개 저장소 코드·채널 목록·요약과 상태 이력은 공개됩니다. Secrets는 저장하지 않습니다. 비공개 운영이 필요하면 현재 워크플로의 차단을 해제하지 말고 사용자의 무료 실행 잔액·결제 차단 정책 또는 기존 PC 자체 러너를 별도로 확인해야 합니다. [GitHub 공식 과금 기준](https://docs.github.com/en/billing/concepts/product-billing/github-actions).

실행 동시성은 저장소 전체에서 하나로 제한하고, 각 외부 호출/발송 전에 상태를 `kairos-youtube-state` 전용 브랜치에 체크포인트합니다. 저장 실패 시 외부 작업을 진행하지 않습니다. `--force-with-lease`로 오래된 실행의 덮어쓰기를 거부합니다. 로컬 실행에는 별도 파일 잠금이 있습니다. 캐시·artifact를 영속 DB로 사용하지 않습니다.

상태 파일 최대 8 MiB, Git 객체 저장 공간 최대 100 MiB, 분석 실행 예산 20분, 워크플로 제한 25분, 한 실행 최대 영상 10개입니다. 추가 영상은 이월됩니다. 무료 영상을 다운로드하거나 파일 API에 업로드하지 않습니다. 발송된 영상 ID는 영구 보존합니다. 용량이 차면 기록을 지워 중복 위험을 만드는 대신 중단합니다.

## 장애와 재개

| 코드 | 동작 |
| --- | --- |
| `google_auth_failed` | 키 지문과 중단 상태 저장, 교체·확인 전 재시도 차단 |
| `free_tier_unverified` | 실제 Google 호출 차단, 대기열 유지 |
| `free_quota_exhausted` | 대기 저장, 분당 한도는 다음 분 이후, 일일/영상 한도는 보수적으로 다음 이용 시점까지 이월 |
| `video_input_failed` 계열 | 미완료 유지, 지수 간격 재시도 |
| `gemini_response_failed` 계열 | 잘림·JSON·근거 부족을 완료로 표시하지 않음 |
| `youtube_discovery_failed` 계열 | 해당 채널 기준시각 유지, 다른 채널 조회 계속 |
| `feed_gap_youtube_key_required` | 최신 피드 범위 밖 누락 경고, 별도 YouTube 키로 복구 |
| `telegram_send_failed` | 확인된 미발송 부분만 나중에 재전송 |
| `telegram_delivery_uncertain` | 중복 방지를 위해 자동 재발송 중단, 사용자 도착 확인 필요 |
| `github_schedule_failed_or_stale` | 보조 watchdog이 최근 성공 시각과 실패 결과 확인 후 관리자 통지 |

Telegram Bot API에는 발송용 멱등 키가 없어, 원격 수신 성공과 상태 저장 사이의 장애에서 완전한 exactly-once를 보장할 수 없습니다. 도착 여부가 불명확하면 실제 메시지를 확인한 후에만 해당 부분의 message ID를 연결합니다.

```powershell
python -m kairos_youtube --git-state reconcile --video-id VIDEO_ID --message-id MESSAGE_ID
```

GitHub Actions 안에서 원격 상태를 사용할 때의 명령입니다. 로컬에서는 원격 저장소 clone에서 실행해야 합니다. 도착하지 않았다고 확정한 경우의 자동 재발송 명령은 제공하지 않습니다. 중복 없이 재처리할 수 있도록 실제 메시지와 기록을 검토한 후 복구해야 합니다.

보조 watchdog도 GitHub Actions에서 실행되므로 GitHub 전체 장애·모든 예약 비활성화·Secrets 누락 시 Telegram 통지를 보장할 수 없습니다. 공개 저장소 장기 비활동에 따른 예약 비활성화, GitHub 알림도 주기적으로 확인해야 합니다. 외부 유료 감시 서비스는 사용하지 않습니다.

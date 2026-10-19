# 키 교체와 실제 배포

## 1. 노출된 Google 키 폐기

Google 계정 로그인과 초기 보안 인증은 이미 완료된 상태입니다. 프로그램에서 재로그인하거나 비밀번호·인증번호·복구코드를 받지 않습니다. 2단계 인증을 유지하세요.

1. 사용자가 직접 [AI Studio API Keys](https://aistudio.google.com/api-keys)에서 노출된 기존 키를 삭제/폐기합니다. 기존 키는 이 프로그램에 입력하지 않습니다.
2. 같은 화면의 프로젝트를 열어 **Free Tier**인지 확인합니다. Cloud Console의 해당 프로젝트 Billing에서 결제 계정이 연결되어 있지 않은지 확인합니다. 유료 계정이나 무료 체험 결제 크레딧에 의존하지 않습니다.
3. 해당 프로젝트의 새 키를 발급합니다. 지원되는 경우 Generative Language API로 API 제한을 설정합니다. 결제수단 등록·Upgrade·유료 플랜 전환은 하지 않습니다.
4. 새 키는 대화·문서·코드·스크린샷에 복사하지 말고 GitHub의 Secret 입력란에 직접 등록합니다.
5. 키가 속한 프로젝트의 활성 모델 한도 화면에서 `gemini-3.8-flash`의 RPM·TPM·RPD를 확인합니다. 모델이 없거나 무료 한도가 0이면 실제 분석은 활성화하지 않습니다.

2026-10-19에 현재 로그인된 AI Studio의 `Default Gemini Project`에서 **무료 등급**, Billing 화면의 **결제 계정 없음**을 읽기 전용으로 확인했습니다. 모델 한도 표에서도 Gemini 3.8 Flash의 **RPM 5 / TPM 250,000 / RPD 20**, 현재 표시 사용량 0을 확인했습니다. 사용자 계정에 재로그인하거나 키를 열어보지 않았습니다. 이 한도는 변경될 수 있으므로 새 키를 등록할 때 같은 프로젝트인지 다시 확인합니다. 노출 키 폐기·새 키 등록이 아직 확인되지 않아 실제 API 호출은 차단한 상태입니다.

## 2. GitHub 저장소·인증

사용자가 만든 전용 저장소는 https://github.com/jejukairos/kairos-youtube 입니다. GitHub 인증 갱신 및 `GEMINI_API_KEY` Secret 존재를 확인했습니다. 비밀번호나 인증번호를 에이전트에 전달하지 않습니다. Telegram 설정과 실제 연결 검증이 끝나기 전에는 예약 실행을 활성화하지 않습니다.

추가 비용을 보장하는 기본 배포는 전용 **공개** 저장소와 표준 Ubuntu 러너입니다. 공개 저장소에는 채널 설정과 생성된 요약/상태도 공개됩니다. 이 공개 범위에 동의한 저장소만 지정하세요. 비공개 저장소로 배포해도 제공한 워크플로는 실행되지 않습니다.

이 `kairos-youtube` 폴더의 내용만 저장소 루트에 업로드합니다. `.env`, `state/`, 사용자 키를 올리지 않습니다. 관련 없는 기존 폴더·Telegram 세션을 업로드하지 않습니다.

워크플로가 활성화되려면 기본 브랜치에 `.github/workflows/youtube.yml`, `.github/workflows/watchdog.yml`이 있어야 합니다. Settings → Actions에서 저장소 Actions 실행과 워크플로의 contents 쓰기 권한을 허용합니다. 토큰은 `contents: write`, `actions: read`만 사용합니다. 상태 브랜치에 대한 쓰기가 막혀 있으면 외부 작업 전에 실행이 중단됩니다.

## 3. Secrets

Settings → Secrets and variables → Actions → New repository secret:

| 이름 | 내용 |
| --- | --- |
| `GEMINI_API_KEY` | 기존 키 폐기 후 새로 발급한 키 |
| `TELEGRAM_BOT_TOKEN` | 사용자의 BotFather 봇 토큰 |
| `TELEGRAM_CHAT_ID` | 수신자 자신의 개인 대화 숫자 ID |
| `FREE_TIER_CONFIRMATION` | 아래에서 생성한 무료 프로젝트 확인 JSON |
| `YOUTUBE_API_KEY` | 선택 사항. 피드 누락 복구용 별도 YouTube Data API 키 |

Telegram에서 해당 봇과 개인 대화를 시작하고 `/start`를 먼저 보냅니다. 수신자 번호를 직접 찾을 필요 없이, 에이전트가 안내한 `연결` 문장을 본인의 봇에 보내서 Chat ID를 안전하게 확인할 수 있습니다. 토큰이 포함된 API URL을 공유하거나 브라우저 기록·스크린샷에 남기지 마세요. 프로그램은 채널/그룹 이름이나 메시지 속 임의 Chat ID로 수신자를 바꾸지 않습니다.

### 개인 대화 연결 보조 기능

`Kairos private Telegram setup` 워크플로는 수동으로만 실행하며 Gemini를 호출하거나 예약 실행을 활성화하지 않습니다. `identify`는 Bot API `getMe`로 토큰이 유효한지 확인하고 공개 봇 사용자 이름만 출력합니다. `link`는 최대 12시간 유효한 임시 연결 문장을 정확히 보낸 개인 대화 한 곳만 찾습니다. 그룹·봇 작성 메시지·오래된 메시지·여러 수신자는 거부합니다. `getUpdates`에 offset을 지정해 기존 메시지를 확인 처리하거나 기존 webhook을 삭제하지 않습니다.

개인 Chat ID는 실행 전에 로컬에서 생성한 RSA-3072 공개키로 암호화하여 로그에 내보냅니다. 복호화 개인키는 Git 제외 `state/`에만 보관하고, 로컬 보조 도구가 `TELEGRAM_CHAT_ID` Secret을 등록한 후 임시 키·연결 Secret·공개키 Variable을 정리합니다. 공개 로그에 토큰·개인 대화 원문·Chat ID를 그대로 출력하지 않습니다. 에이전트가 이 작업을 처리하며 사용자는 안내된 문장만 자신의 봇에 보내면 됩니다.

GitHub 실행기 배정이 지연되는 경우 `tools/telegram-link-wait.mjs RUN_ID`가 현재 요청한 연결 작업을 로컬에서 최대 3시간 동안 기다릴 수 있습니다. 성공하면 수신자 Secret을 등록하고 연결 테스트를 한 번 요청합니다. 대기 진행 상태는 Git 제외 `state/telegram-link-progress.json`에 저장하며, 기존에 보낸 문장과 임시 개인키를 그대로 사용합니다. 테스트는 고정된 개인 Chat ID에 한 번만 발송하며, 네트워크 결과가 불명확해도 자동 재발송하지 않습니다. 이 보조 작업은 Gemini 분석이나 정기 실행을 활성화하지 않습니다.

## 4. 무료 프로젝트 확인 기록

현재 확인된 값은 `ACTUAL_RPM=5`, `ACTUAL_TPM=250000`, `ACTUAL_RPD=20`입니다. 아래 명령에 등록 시점의 실제 값을 넣습니다. `TPD_CAP`은 Google TPD 제한이 있으면 그 이하, 없으면 직접 정한 보수적 일일 토큰 상한입니다. Gemini 전용 무료 프로젝트를 사용합니다. 관측한 프로젝트 ID는 공개 문서 대신 Git 제외 경로 `state/free-tier-observation.json`에 보관했습니다.

```powershell
python -m kairos_youtube confirm-free-tier `
  --project PROJECT_ID `
  --tier free `
  --billing-disabled CONFIRMED `
  --old-key-revoked CONFIRMED `
  --dedicated-project CONFIRMED `
  --rpm ACTUAL_RPM `
  --tpm ACTUAL_TPM `
  --rpd ACTUAL_RPD `
  --tokens-per-day TPD_CAP `
  --video-seconds-per-day 28800
```

명령이 새 키를 **숨김 입력**으로 한 번 읽어 SHA-256 지문만 기록합니다. 키 자체는 파일에 저장하지 않습니다. `state/free-tier-confirmation.json`의 JSON 전체를 `FREE_TIER_CONFIRMATION` Secret에 등록합니다. GitHub CLI를 사용할 경우:

```powershell
Get-Content state/free-tier-confirmation.json -Raw | gh secret set FREE_TIER_CONFIRMATION --repo OWNER/REPO
```

프로그램은 무료 확인의 최대 유효기간을 7일로 제한합니다. 만료되면 확인 기록을 갱신해야 하며, Google에 자동 로그인하지 않습니다. Cloud Billing이 연결되거나 등급이 바뀌면 `KAIROS_ENABLED`를 즉시 `false`로 변경하세요. 확인 JSON은 실제 프로젝트 과금 상태를 검증했다는 사용자의 기록이며, Google의 결제 잠금이나 실시간 Billing API 증명이 아닙니다. API 키의 프로젝트가 잘못 매핑되면 이 로컬 검사로 알아낼 수 없으므로 반드시 같은 프로젝트에서 새 키를 발급해야 합니다.

가격 문서 확인일이 30일을 넘으면 무료 모델과 YouTube 입력 가격을 공식 문서에서 다시 확인한 후 `config/policy.json`의 검토일을 갱신합니다. 단순히 날짜만 연장하지 않습니다.

## 5. 실제 연결 검증 후 정기 운영

1. `KAIROS_ENABLED`는 비워 두거나 `false`로 둡니다. 수동 `discover`와 `smoke`만 실행할 수 있으며 예약 실행은 계속 꺼져 있습니다. Secrets 미등록·무료 확인 실패 상태에서는 영상 분석이 차단됩니다.
2. Actions → Kairos YouTube twice daily → Run workflow → `discover`. 32개 채널 조회와 `kairos-youtube-state` 브랜치 생성을 확인합니다. 과거 영상은 발송되지 않습니다.
3. 같은 워크플로 → `smoke`. 등록된 채널의 공개 영상 **한 개**의 `https://www.youtube.com/watch?v=VIDEO_ID` URL을 입력합니다.
4. 실제 Gemini 응답이 영상 내용을 반영하는지, 숫자와 주장·근거·전망 조건을 확인합니다. 봇이 개인 Chat ID에 보낸 요약과 원본 URL을 검토합니다. 성공한 테스트 영상도 동일 상태에 기록해 정기 실행에서 재분석·재발송하지 않습니다.
5. 실제 영상 분석·Telegram 요약 도착을 확인한 뒤 Variables에 `KAIROS_ENABLED=true`를 등록합니다. `run`을 수동 실행하고 신규 영상·브리핑·미처리 건수를 확인합니다. 이후 cron은 12:30/21:30 KST에 예약됩니다.
6. Actions 실행 기록에서 실제 예약 실행까지 확인해야 배포 검증이 완료됩니다. 워크플로 파일 생성만으로 예약 배포 완료라고 표시하지 않습니다.

정기 실행은 서버 측 API 키를 사용하며 Google 로그인 승인·SMS·CAPTCHA·Authenticator 입력을 요구하지 않습니다. 인증 오류는 중단하고 새 키 등록을 기다립니다. 로그인 쿠키·Google 브라우저 세션은 저장하지 않습니다.

## 6. 선택 사항: YouTube Data API

Gemini 키가 YouTube API에도 자동으로 허용된다고 가정하지 않습니다. 누락 복구가 필요하면 사용자가 별도 무료 프로젝트/키에서 **YouTube Data API v3**를 활성화하고 `YOUTUBE_API_KEY` Secret에 등록합니다. 결제수단은 등록하지 않습니다. Cloud Console의 실제 일일 quota도 확인합니다.

공식 `channels.list`, `playlistItems.list`는 각 요청의 quota 비용이 1입니다. 비싼 `search.list`는 사용하지 않습니다. 프로그램 자체 상한은 하루 1,000 단위이며 실제 허용량이 이보다 작으면 `policy.json`에서 낮춥니다. 미등록 상태에서도 공개 RSS 조회는 작동하지만 피드 15개 범위를 넘어선 누락 복구를 보장할 수 없습니다. [channels.list](https://developers.google.com/youtube/v3/docs/channels/list), [playlistItems.list](https://developers.google.com/youtube/v3/docs/playlistItems/list).

## 7. 운영 중 중단·복구

- 중단: Variables → `KAIROS_ENABLED=false`. 브랜치·영상 ID 이력은 보존합니다.
- 키 인증 실패: Google 계정 재로그인 자동화 없이 사용자가 직접 키 상태를 확인하고 교체합니다. 새 키와 확인 JSON을 함께 갱신합니다.
- 무료 한도: 대기열은 지우지 않습니다. 다음 실행에서 이용 가능한 한도 범위 안에서 우선순위가 높은 영상을 먼저 처리합니다.
- 로컬 강제 종료로 `state/state.lock`이 남으면 실제 실행 프로세스가 없는지 확인한 뒤 **그 잠금 파일 하나만** 삭제합니다. 원격 상태 브랜치는 지우지 않습니다.
- Telegram 도착 불명확: 실제 대화에서 해당 부분을 확인한 후 README의 reconcile 명령을 사용합니다. 중복 방지를 위해 자동 재발송하지 않습니다.
- 저장 한도: 무작정 상태를 초기화하지 않습니다. 발송 ID를 보존한 채 사람이 기록/보존 정책을 검토해야 합니다.

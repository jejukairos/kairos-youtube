# 유튜브 조회용 키 연결

2026-10-20: 사용자의 `YOUTUBE_API_KEY` 등록을 확인했고, 실행 `39366079455`에서 공식 공개 영상 정보 조회 → Gemini 영상 분석 → 6문단 요약 검사 → Telegram 메시지 2개 발송 → 원격 sent 저장까지 성공했습니다. 아래 절차는 추후 키 교체·재등록 시에도 사용할 수 있습니다. 키 값은 확인·출력하지 않았습니다.

Gemini 키와 별도로 공개 영상의 길이·채널·공개 상태를 확인하는 키입니다. 현재 GitHub 실행 서버의 유튜브 시청 페이지는 로그인 확인을 요구하므로, 로그인 자동화를 추가하지 않고 공식 YouTube Data API를 사용합니다.

이미 확인한 **결제 계정이 연결되지 않은 프로젝트**에서 진행합니다. 카드 등록, 무료 체험 결제 계정 만들기, 결제 연결, 유료 업그레이드를 요구하면 진행하지 마세요.

1. [YouTube Data API v3 열기](https://console.cloud.google.com/apis/library/youtube.googleapis.com). 교육용 계정과 `Default Gemini Project`를 선택한 뒤 **사용**을 누릅니다. **관리**로 표시되면 이미 사용 설정된 상태입니다.
2. [사용자 인증 정보 열기](https://console.cloud.google.com/apis/credentials). 같은 프로젝트에서 **사용자 인증 정보 만들기 → API 키**를 선택합니다. 이름은 `Kairos YouTube`로 정해도 됩니다. API 제한에서 **YouTube Data API v3**만 선택하고 **만들기**를 누릅니다. 생성 직후 제한을 설정하는 화면이라면 해당 키를 열어 같은 제한을 저장합니다. 애플리케이션 제한은 **없음**으로 둡니다. GitHub의 IP는 고정되지 않으므로 개인 PC IP를 입력하지 않습니다. **서비스 계정을 통해 API 호출 인증** 항목은 선택하지 않습니다. YouTube 공개 정보 조회에는 서비스 계정에 연결하지 않은 일반 API 키를 사용합니다. 키 전체를 대화나 스크린샷으로 보내지 않습니다.
3. [GitHub 비밀 보관함 열기](https://github.com/jejukairos/kairos-youtube/settings/secrets/actions). **New repository secret**을 누릅니다. Name은 `YOUTUBE_API_KEY`, Secret은 새 유튜브 조회용 키를 붙여넣고 **Add secret**으로 저장합니다.

[할당량 화면](https://console.cloud.google.com/apis/api/youtube.googleapis.com/quotas)에서 실제 일일 할당량도 확인합니다. 프로그램은 하루 최대 1,000단위로 제한하며, 프로젝트의 실제 한도가 더 낮으면 그 이하로 조정합니다. 다른 앱에서 소비한 할당량은 이 프로그램의 장부에 포함되지 않으므로 Google의 할당량 오류가 나오면 보류합니다. 자동으로 추가 할당량을 신청하거나 결제를 연결하지 않습니다.

에이전트는 Secret의 값을 출력하지 않고 공개 영상 조회와 실제 요약·발송을 검증한 뒤 정기 운영 준비 상태를 보고합니다. 요청된 일정은 한국 시간 매일 11시 30분·15시·20시·23시입니다. 키 등록만으로 실제 정기 요약·발송 완료라고 판단하지 않습니다.

공식 안내: [YouTube API 시작하기](https://developers.google.com/youtube/v3/getting-started), [API 키 생성·제한](https://docs.cloud.google.com/docs/authentication/api-keys#create_an_api_key), [영상 정보 조회](https://developers.google.com/youtube/v3/docs/videos/list).

## 이전 근거 시각 오류와 현재 상태

초기 응답에는 영상 시각의 분·초를 연결한 숫자와 실제 초가 섞이는 패턴이 있었습니다. AI가 초 환산까지 수행하도록 한 출력 설계의 문제를 줄이기 위해, 공식 영상 안내가 권장하는 `MM:SS`(1시간 이상 `HH:MM:SS`)를 받고 프로그램이 직접 환산하도록 수정했습니다. 예를 들어 `03:45`는 정확히 225초입니다. 기존 영상 길이·초반/중반/후반 근거 검사와 불충분 응답 거부는 그대로 유지합니다. 잘못된 시각을 임의로 잘라 통과시키지 않습니다.

별도로 GitHub 서버에서 공개 시청 페이지에 로그인 요청이 발생했고, 잦은 원격 상태 저장 중 오류가 있었습니다. 공식 YouTube API 조회 모듈을 준비했고, 공개 피드 조회 결과는 묶어서 저장하며 원격 저장 간격도 제한했습니다. 근거 시각 형식을 고쳤다는 사실만으로 모든 요약 문장·숫자의 의미 정확성을 독립적으로 증명하는 것은 아닙니다.

2026-10-20 초기 검증에서 실제 16분 50초 영상의 근거 범위를 확인했고, 해당 영상은 9문단 요약·Telegram 메시지 1개 발송·sent 기록이 확인됐습니다. 이후 YouTube 조회 키 등록과 실제 연결을 검증했습니다. 무료 한도·입력 제한·검증 실패 때문에 모든 영상의 즉시 처리 또는 오류 0건을 보장하지는 않습니다. 보류 기록은 보존하며 지원 범위와 무료 예산 안에서 재처리합니다.

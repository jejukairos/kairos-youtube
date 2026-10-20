# 유튜브 조회용 키 연결

Gemini 키와 별도로 공개 영상의 길이·채널·공개 상태를 확인하는 키입니다. 현재 GitHub 실행 서버의 유튜브 시청 페이지는 로그인 확인을 요구하므로, 로그인 자동화를 추가하지 않고 공식 YouTube Data API를 사용합니다.

이미 확인한 **결제 계정이 연결되지 않은 프로젝트**에서 진행합니다. 카드 등록, 무료 체험 결제 계정 만들기, 결제 연결, 유료 업그레이드를 요구하면 진행하지 마세요.

1. [YouTube Data API v3 열기](https://console.cloud.google.com/apis/library/youtube.googleapis.com). 교육용 계정과 `Default Gemini Project`를 선택한 뒤 **사용**을 누릅니다. **관리**로 표시되면 이미 사용 설정된 상태입니다.
2. [사용자 인증 정보 열기](https://console.cloud.google.com/apis/credentials). 같은 프로젝트에서 **사용자 인증 정보 만들기 → API 키**를 선택합니다. 키 제한을 편집할 수 있으면 API 제한은 **YouTube Data API v3**만 허용합니다. GitHub의 IP는 고정되지 않으므로 특정 개인 PC IP를 입력하지 않습니다. 키 전체를 대화나 스크린샷으로 보내지 않습니다.
3. [GitHub 비밀 보관함 열기](https://github.com/jejukairos/kairos-youtube/settings/secrets/actions). **New repository secret**을 누릅니다. Name은 `YOUTUBE_API_KEY`, Secret은 새 유튜브 조회용 키를 붙여넣고 **Add secret**으로 저장합니다.

[할당량 화면](https://console.cloud.google.com/apis/api/youtube.googleapis.com/quotas)에서 실제 일일 할당량도 확인합니다. 프로그램은 하루 최대 1,000단위로 제한하며, 프로젝트의 실제 한도가 더 낮으면 그 이하로 조정합니다. 다른 앱에서 소비한 할당량은 이 프로그램의 장부에 포함되지 않으므로 Google의 할당량 오류가 나오면 보류합니다. 자동으로 추가 할당량을 신청하거나 결제를 연결하지 않습니다.

에이전트는 Secret의 값을 출력하지 않고 공개 영상 조회와 실제 요약·발송을 검증한 뒤 정기 운영 준비 상태를 보고합니다. 요청된 일정은 한국 시간 매일 11시·15시·20시·23시입니다. 키 등록만으로 실제 정기 요약·발송 완료라고 판단하지 않습니다.

공식 안내: [YouTube API 시작하기](https://developers.google.com/youtube/v3/getting-started), [영상 정보 조회](https://developers.google.com/youtube/v3/docs/videos/list).

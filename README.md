# 나라장터 키워드 모니터링

조달청 공식 API(공공데이터포털)로 나라장터 **입찰공고 · 발주계획 · 사전규격**을 조회해서, 사업명에 관심 키워드가 들어간 신규 건을 슬랙 채널로 보고합니다.

```
설정 웹페이지 ──저장──▶ config.json (이 저장소)
                               │
GitHub Actions (매시간 확인) ──▶ 발송 요일·시각이면 조회 → 필터 → 슬랙 채널
```

- **설정은 웹페이지에서**: 조회 대상, 키워드, 제외 조건(제외어·금액·기관), 발송 요일·시각, 메시지 형식
- 수정하는 즉시 예시 메시지 미리보기, 저장하면 다음 발송부터 적용
- **실제 데이터 테스트**: 버튼 한 번으로 실제 조회 결과 미리보기 또는 슬랙에 [테스트] 전송
- 이미 보고한 건은 다시 보내지 않음 (`state.json`에 기록)

---

## 설정 웹페이지 사용법

### 처음 한 번: 페이지 열기
1. 이 저장소의 `admin/index.html` 파일을 클릭 → 오른쪽 위 **다운로드(↓ Download raw file)** 버튼
2. 받은 파일을 원하는 폴더(예: 문서)에 두고 **크롬으로 열기** → 크롬 즐겨찾기에 추가
   - 이후로는 즐겨찾기로 바로 엽니다. 페이지 기능이 바뀌면 같은 방법으로 다시 받으면 됩니다.

### 처음 한 번: GitHub 토큰 만들기
페이지가 이 저장소의 설정 파일을 읽고 저장하려면 이 저장소 전용 열쇠(토큰)가 필요합니다.

1. GitHub → 오른쪽 위 프로필 사진 → **Settings** → 왼쪽 맨 아래 **Developer settings**
2. **Personal access tokens → Fine-grained tokens → Generate new token**
3. Token name `나라장터 설정`, Expiration **1년**
4. Repository access: **Only select repositories** → `narajangteo-monitor`
5. Permissions → Repository permissions에서 두 가지만 변경
   - **Contents: Read and write**
   - **Actions: Read and write**
6. **Generate token** → 표시된 `github_pat_...` 값을 복사해 설정 페이지의 "GitHub 토큰" 칸에 붙여넣고 **연결**

토큰은 내 브라우저에만 저장되고 GitHub 외에는 보내지 않습니다. 다른 사람과 공유하지 말고, 유출이 의심되면 같은 화면에서 삭제(Delete) 후 새로 만드세요. 1년 뒤 만료되면 새로 만들어 다시 붙여넣으면 됩니다.

### 평소 사용
| 하고 싶은 것 | 방법 |
|---|---|
| 키워드·제외 조건 바꾸기 | 입력 후 Enter → 오른쪽 예시 미리보기 확인 → 상단 **저장** |
| 발송 시각 바꾸기 / 하루 여러 번 받기 | "발송 시간 · 주기"에서 요일·시각 버튼 선택 → 저장 |
| 메시지 모양 바꾸기 | "메시지 형식"에서 문구 수정, `{변수}` 버튼으로 항목 넣기 → 저장 |
| 실제로 어떻게 올지 확인 | 오른쪽 **실제 데이터 테스트** → 미리보기 실행 (1~2분, 슬랙엔 안 감) |
| 슬랙으로 시험 발송 | **슬랙으로 테스트 전송** (메시지 앞에 [테스트] 표시, 발송 기록엔 반영 안 됨) |

> 테스트는 **저장된 설정**으로 실행됩니다. 바꾼 내용을 테스트하려면 먼저 저장하세요.

### 발송 시각 동작 방식
GitHub가 한국시간 06~23시 매시 17분에 깨어나 설정된 요일·시각인지 확인합니다. 해당 시각이면 지난 발송 이후 새로 올라온 건을 보내고, 아니면 바로 종료합니다. (Actions 탭에 "예약 확인" 실행이 매시간 쌓이는 건 정상입니다.)
GitHub 사정으로 늦어지거나 실패하면 3시간 안에 다음 실행이 이어서 보냅니다.

---

## 처음 설치 (완료됨 · 새로 설치할 때만 참고)

### 1단계. 공공데이터포털 인증키 발급 (API 활용신청)

1. [data.go.kr](https://www.data.go.kr) 에 로그인합니다 (회원가입 필요 시 가입).
2. 상단 검색창에서 아래 **3개 API**를 하나씩 검색해 들어가 **[활용신청]** 을 누릅니다.
   | 검색어 | 비고 |
   |---|---|
   | 조달청_나라장터 입찰공고정보서비스 | 입찰공고 |
   | 조달청_나라장터 발주계획현황서비스 | 발주계획 |
   | 조달청_나라장터 사전규격정보서비스 | 사전규격 |
   - 활용목적: "기타" 선택 후 "자사 솔루션 관련 입찰공고 모니터링" 정도로 작성
   - 개발계정은 보통 **자동 승인**됩니다.
3. **마이페이지 → 개발계정** (또는 데이터활용 → Open API → 활용신청 현황)에서 승인된 API를 누르면 **일반 인증키(Decoding)** 가 보입니다. 이 값을 복사해 메모장에 적어 둡니다.
   - 인증키는 계정당 하나라서 3개 API 모두 같은 키를 씁니다.
   - 승인 직후 1~2시간은 "등록되지 않은 키" 오류가 날 수 있습니다. 정상입니다.

### 2단계. 슬랙 Webhook 주소 만들기

슬랙 채널에 메시지를 넣을 수 있는 전용 주소를 만드는 과정입니다.

1. 알림 받을 슬랙 채널을 정합니다 (예: `#나라장터-알림`, 없으면 새로 만들기).
2. [api.slack.com/apps](https://api.slack.com/apps) 접속 → **Create New App** → **From scratch**
   - App Name: `나라장터 알림` / Workspace: 회사 워크스페이스 선택 → Create App
3. 왼쪽 메뉴 **Incoming Webhooks** → 오른쪽 스위치를 **On**
4. 아래쪽 **Add New Webhook to Workspace** → 1번에서 정한 채널 선택 → **허용**
5. 생성된 `https://hooks.slack.com/services/...` 주소를 복사해 메모장에 적어 둡니다.
   - 워크스페이스 설정상 앱 설치에 관리자 승인이 필요하면, 요청 후 승인되면 이어서 진행하세요.
   - 이 주소를 아는 사람은 누구나 채널에 글을 쓸 수 있으니 외부에 공유하지 마세요.

### 3단계. GitHub 저장소 만들고 파일 올리기

1. [github.com](https://github.com) 에 **개인 계정**으로 로그인
2. 오른쪽 위 **+** → **New repository**
   - Repository name: `narajangteo-monitor`
   - **Private** 선택 → **Create repository**
3. 만들어진 화면에서 **uploading an existing file** 링크(또는 Add file → Upload files) 클릭
4. 받은 파일 중 **`monitor.py`, `config.json`, `README.md`** 3개를 끌어다 놓고 → 아래 **Commit changes**
5. 자동 실행 설정 파일은 숨김 폴더(`.github`) 안에 있어 끌어다 놓기가 잘 안 되므로 직접 만듭니다.
   - 저장소 첫 화면에서 **Add file → Create new file**
   - 파일 이름 칸에 정확히 `.github/workflows/narajangteo.yml` 입력 (슬래시를 치면 폴더가 자동으로 생깁니다)
   - 받은 `narajangteo.yml` 파일을 텍스트 편집기로 열어 내용을 전부 복사 → 붙여넣기
   - **Commit changes** 클릭

### 4단계. 인증키와 Webhook 주소 등록 (Secrets)

코드에 비밀번호를 직접 쓰지 않고 GitHub 금고(Secrets)에 넣어 두는 단계입니다.

1. 저장소 상단 **Settings** → 왼쪽 **Secrets and variables → Actions**
2. **New repository secret** 을 눌러 2개를 등록합니다. (이름은 대소문자까지 정확히)
   | Name | Secret (값) |
   |---|---|
   | `G2B_SERVICE_KEY` | 1단계의 인증키(Decoding) |
   | `SLACK_WEBHOOK_URL` | 2단계의 hooks.slack.com 주소 |

---

## 문제해결

| 메시지 | 원인과 조치 |
|---|---|
| 설정 페이지 "토큰이 올바르지 않거나 만료" | 토큰을 새로 만들어 붙여넣기 |
| 설정 페이지 403/404 | 토큰의 저장소 선택(narajangteo-monitor)과 권한 2가지(Contents, Actions) 확인 |
| `SERVICE_KEY_IS_NOT_REGISTERED_ERROR` | 해당 API 활용신청 여부 확인, 승인 직후면 1~2시간 뒤 재시도 |
| `연결 실패: timed out` | 공공데이터포털이 해외(GitHub 서버) 접속을 늦게 처리한 경우. 자동 재시도됨 |
| 저장 시 "다른 곳에서 설정이 바뀜" | 페이지 새로고침 후 다시 수정 |
| 기타 오류 | 실행 로그를 복사해 Claude에게 전달 |

## 파일 구성

| 파일 | 역할 |
|---|---|
| `admin/index.html` | 설정 웹페이지 |
| `config.json` | 설정 값 (웹페이지가 읽고 저장) |
| `monitor.py` | 조회·필터·슬랙 전송 프로그램 |
| `.github/workflows/narajangteo.yml` | 매시간 실행 · 테스트 실행 정의 |
| `state.json` | 이미 보낸 공고·발송 기록 (자동) |
| `previews/latest.json` | 최근 미리보기 결과 (자동) |

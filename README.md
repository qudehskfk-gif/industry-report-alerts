# 산업 심층 리포트 텔레그램 알림

한경 컨센서스와 네이버 금융의 **산업 리포트**를 정기적으로 확인하고, 새 심층 분석 자료를 텔레그램으로 보냅니다. 개별 기업 리포트 게시판은 수집하지 않습니다. 서버 없이 실행할 수 있도록 GitHub Actions 설정을 포함했습니다.

현재 제공된 파일은 실행 준비가 된 프로그램입니다. **텔레그램 봇 생성, GitHub 저장소 업로드, Secrets 등록 후 첫 실행을 해야 알림이 시작됩니다.**

두 Telegram Secret 중 하나라도 비어 있으면 자동 실행은 오류 없이 연결 대기 상태로 종료합니다. Actions의 초록색 표시만으로 연결 완료를 판단하지 말고, 두 값을 등록한 뒤 `send_test`로 실제 테스트 메시지 도착을 확인하세요.

## 알림 기준

- 산업 리포트 중 제목이나 PDF 앞 2쪽에 `In-depth`, `심층 분석·리포트·보고서`, `산업 대해부` 표시가 있거나 PDF가 기본 **20쪽 이상**인 자료를 선별합니다.
- 제목에 일간·주간·월간, 브리핑, 브리프, 실적·어닝 프리뷰·리뷰 등의 표시가 있는 자료는 제외합니다. 심층 표시나 분량 기준을 만족해도 이 제외 규칙이 우선합니다. 산업 리포트에 여러 기업의 분석이 포함된 경우는 수집 대상입니다.
- 제목에 정기 자료 표시가 없더라도 네이버 본문의 첫 강조 제목이나 PDF 표지에 주간·월간 등의 표시가 있으면 추가로 걸러냅니다.
- 같은 자료가 두 사이트에 올라오면 PDF 내용과 제목·증권사·날짜를 이용해 중복을 줄입니다.
- 알림에는 제목, 증권사, 발행일, 선별 근거, 원문·PDF 링크가 들어갑니다.
- **사이트별 첫 수집 성공 시 기존 목록만 기록하며, 과거 리포트를 한꺼번에 보내지 않습니다. 이후 새로 발견한 자료부터 알립니다.** 처음에 한 사이트의 수집만 실패했다면 해당 사이트는 복구 후 처음 수집할 때 기준 목록을 만듭니다.

분량과 표기 기반의 선별이므로 사람이 판단하는 심층 리포트와 차이가 있을 수 있습니다. `MIN_PDF_PAGES`로 분량 기준을 조정할 수 있습니다. PDF 다운로드·분석이 실패하면 해당 자료를 다음 실행에서 재시도하며, 25 MiB를 초과하는 PDF는 검사하지 않습니다. 사이트별 수집 결과와 오류는 실행 로그에서 확인할 수 있습니다.

## 빠른 설정: 안내에 따라 한 번에 연결하기

처음 설정한다면 아래 도우미를 권장합니다. 직접 저장소와 Secrets를 등록하는 방법은 다음 절에 있습니다.

1. [Python 3.12 이상](https://www.python.org/downloads/), [Git](https://git-scm.com/downloads), [GitHub CLI](https://cli.github.com)를 설치하고 GitHub 계정을 준비합니다. 설치 후 새 터미널을 엽니다.
2. 텔레그램에서 공식 [@BotFather](https://t.me/BotFather)에 `/newbot`을 보내 봇을 만들고 토큰을 보관합니다.
3. 프로젝트 압축을 **기존 Git 저장소 밖의 새 폴더**에 풀고, 터미널에서 그 폴더로 이동합니다.
4. 다음 명령을 실행합니다.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
gh auth login --scopes workflow
python scripts/deploy_github.py
```

Windows PowerShell에서는 두 번째 명령 대신 `.venv\Scripts\Activate.ps1`을 사용합니다. 이미 GitHub CLI에 로그인했다면 로그인 명령은 생략할 수 있습니다. 기존 인증에 워크플로 업로드 권한이 없으면 `gh auth refresh -s workflow`로 추가하세요.

도우미가 토큰을 화면에 숨겨 입력받고, 만든 봇에 `/start`를 보내도록 안내합니다. 수신할 개인 대화방과 새 저장소 이름을 확인하고 마지막에 `yes`를 입력하면 **공개 저장소 생성 → 프로그램 업로드 → GitHub Secrets 등록 → 첫 실행 요청**을 진행합니다. 토큰은 파일이나 명령줄 인수에 저장하지 않습니다.

텔레그램 테스트 메시지와 안내된 Actions 페이지의 실행 결과를 확인하세요. 첫 실행이 성공하면 30분 주기로 확인합니다. 기존 Git 저장소는 자동 변경하지 않으며, 설정이 중간에 실패하면 이미 만든 저장소의 수동 설정으로 이어갈 수 있습니다.

설정이 일부 완료된 폴더에서는 도우미를 다시 실행하지 않습니다. 특히 저장소 생성과 `origin` 등록 후 `.github` 업로드 권한 때문에 push만 실패했다면, 같은 폴더에서 `gh auth refresh -s workflow`를 실행한 뒤 `git push --set-upstream origin main`으로 업로드를 마칩니다. 이후 아래 수동 설정의 Secrets 두 개 등록과 Actions 첫 실행을 진행하면 됩니다. 저장소 생성 자체가 실패했다면 GitHub에 실제로 생성된 저장소와 주소부터 확인하세요.

## 수동 설정 1. 텔레그램 봇 만들기

1. 텔레그램에서 공식 [@BotFather](https://t.me/BotFather)를 열고 `/newbot`을 보냅니다.
2. 안내에 따라 봇 이름과 사용자 이름을 정하고 **봇 토큰**을 보관합니다.
3. 방금 만든 봇의 대화방을 열어 **시작**을 누르거나 `/start`를 보냅니다. 봇은 먼저 대화를 시작할 수 없습니다.
4. 아래의 로컬 준비를 한 뒤 `python -m reportbot telegram-setup`을 실행합니다. 토큰을 입력하고 화면 안내에 따라 봇에 `/start`를 보낸 뒤 Enter를 누르면 **개인 대화방의 채팅 ID**를 확인할 수 있습니다. 이 설정 도구는 그룹·채널 ID를 찾지 않습니다.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m reportbot telegram-setup
```

Windows PowerShell에서는 활성화 명령이 `.venv\Scripts\Activate.ps1`입니다. Python 3.12 이상을 권장합니다. 위 명령은 압축을 푼 프로젝트 폴더에서 실행합니다. 토큰을 채팅이나 공개 저장소에 붙여 넣지 마세요.

## 수동 설정 2. GitHub에서 서버 없이 실행하기

1. [GitHub](https://github.com/new)에서 **공개(Public) 저장소**를 만듭니다.
2. 프로젝트 파일을 업로드합니다. 저장소 최상위에 `requirements.txt`, `reportbot/`, `.github/workflows/monitor.yml`이 있어야 합니다. `.github` 폴더까지 포함하고, `.env`와 `.venv`는 업로드하지 않습니다. GitHub 웹 업로드가 숨김 폴더를 누락하면 GitHub Desktop 또는 Git으로 폴더 전체를 올리세요.
3. 저장소의 **Settings → Secrets and variables → Actions → New repository secret**에서 두 값을 등록합니다.

| Secret 이름 | 값 |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | BotFather에서 받은 봇 토큰 |
| `TELEGRAM_CHAT_ID` | `telegram-setup`에서 확인한 채팅 ID |

4. 저장소의 **Actions → Industry report alerts → Run workflow**를 누릅니다. 연결 확인 메시지를 받으려면 `send_test`를 체크합니다.
5. 실행이 초록색으로 완료되고 텔레그램 테스트 메시지가 도착하면 준비가 끝납니다. 첫 실행은 현재 목록을 기준으로 기록합니다.

그다음부터 **30분마다** 확인합니다. GitHub 예약 작업은 지연되거나 건너뛰어질 수 있으므로 게시 즉시 도착하는 실시간 알림은 아닙니다. 예약 실행은 기본 브랜치에 워크플로가 있을 때 동작합니다. 계정의 GitHub Actions 사용 한도·과금 설정도 확인하세요.

알림 기록은 자동 생성되는 `notification-state` 브랜치의 `state.sqlite3`에 보관합니다. **이 브랜치를 지우지 마세요.** 기록이 없는 첫 실행은 다시 기준 목록을 만들기 때문에 그 사이 자료가 알림에서 빠질 수 있습니다. 브랜치는 있는데 파일이 없으면 자동 초기화하지 않고 오류를 냅니다. 이전 커밋의 파일을 복원한 뒤 실행하세요.

저장소 정책이 Actions의 쓰기를 막으면 **Settings → Actions → General → Workflow permissions**에서 읽기·쓰기를 허용하거나 조직 관리자에게 해당 저장소의 정책 변경을 요청해야 합니다. 프로그램은 외부 서버나 별도 유료 API를 사용하지 않습니다.

## 로컬 PC에서 실행하기

항상 켜 둔 PC가 있다면 GitHub 없이 실행할 수 있습니다. `.env.example`을 `.env`로 복사해 두 텔레그램 값을 채우면 프로그램이 자동으로 읽습니다.

```bash
cp .env.example .env
# 편집기에서 .env에 토큰과 채팅 ID 입력
python -m reportbot test-message
python -m reportbot check --dry-run
python -m reportbot check
python -m reportbot daemon
```

`check`는 한 번 확인하고 종료하며 `daemon`은 30분 간격으로 계속 확인합니다. PC가 꺼지거나 명령창을 닫으면 실행도 멈춥니다. `check --dry-run`은 발송과 알림 기록을 변경하지 않는 점검용입니다. GitHub에서 실행할 때는 `.env`를 쓰지 않고 Secrets를 사용합니다.

## 설정

GitHub에서는 `.github/workflows/monitor.yml`의 `env` 값을, 로컬에서는 환경변수를 바꿉니다.

| 이름 | 기본값 | 용도 |
| --- | --- | --- |
| `MIN_PDF_PAGES` | `20` | 심층 표기가 없는 PDF의 최소 쪽수 |
| `MAX_LIST_PAGES` | `3` | 매번 확인할 사이트별 목록 페이지 수 |
| `MAX_REPORT_AGE_DAYS` | `14` | 현재 날짜로부터 최근 며칠의 자료를 확인할지 |
| `POLL_SECONDS` | `1800` | 로컬 `daemon` 확인 간격(초) |
| `STATE_PATH` | `data/state.sqlite3` | 발송·기준 목록 기록 파일 |

GitHub의 확인 간격은 `POLL_SECONDS` 대신 워크플로의 `cron`으로 바꿉니다. 오래 실행을 멈췄다가 재개하면 조회 페이지·기간 밖으로 밀려난 자료는 발견하지 못할 수 있습니다. 긴 공백 뒤에는 `MAX_LIST_PAGES`와 기간을 함께 늘려 확인하세요.

## 운영과 점검

- 알림이 없으면 먼저 Actions 실행 성공 여부와 새 자료의 선별 기준을 확인합니다.
- `401`이면 봇 토큰을, `403` 또는 채팅 오류면 만든 봇에 `/start`를 보냈는지와 채팅 ID를 확인합니다.
- 사이트 구조 변경, 차단, PDF 다운로드 실패 등은 실행 로그에서 확인합니다. 한 실행 안의 네트워크 재시도 횟수에는 제한이 있으며, 처리하지 못한 자료는 다음 정기 실행에서 다시 확인합니다.
- 한 사이트 수집이 실패해도 다른 사이트는 계속 처리하며, 실패한 실행도 이미 저장된 알림 기록을 보관합니다. 확인 단계에는 20분 제한을 두고 그 뒤 별도 단계에서 기록을 저장합니다. 작업 전체가 강제 종료되거나 기록 push가 실패하면 마지막 실행 기록이 보관되지 않을 수 있습니다.
- 발송 성공 후에 기록을 남깁니다. 발송 직후 프로세스나 기록 저장이 실패한 경우에는 다음 실행에서 같은 알림이 다시 올 수 있습니다.
- 자동화를 멈추려면 Actions에서 해당 워크플로를 비활성화합니다. 토큰이 노출되면 BotFather에서 재발급한 뒤 Secret을 교체하세요.

개발 점검:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

2026-10-25 검증: 자동 테스트 49개 통과. 실제 한경 산업 52건·네이버 산업 60건의 첫 기준선 저장과 재확인에서 오류·발송이 모두 0건임을 확인했습니다. 실제 PDF의 쪽수·심층 표시도 검사했습니다. 실제 텔레그램 발송과 GitHub 예약 실행은 봇·계정 연결 후 확인해야 합니다.

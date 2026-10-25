# 산업 심층 리포트 텔레그램 알림

한경 컨센서스와 네이버 금융의 **산업 리포트**를 확인하고, 조건에 맞는 미발송 자료를 **PDF 원문 첨부**로 텔레그램에 보냅니다. 개별 기업 리포트 게시판은 수집하지 않습니다. GitHub Actions에서 **매일 한국시간 오후 5시**에 한 번 실행합니다.

PDF는 메시지의 문서 첨부를 눌러 바로 열 수 있습니다. 제목, 증권사, 발간일, 출처, 쪽수와 선정 근거는 문서 설명에 표시합니다. 봇 토큰과 채팅 ID는 GitHub Secrets에서 읽으며 공개 코드·실행 로그·알림 기록 DB에 넣지 않습니다.

## 언제 어떤 자료를 보내나요?

- 매일 17:00에 공개 목록을 다시 확인하고 **아직 발송 완료로 기록되지 않은 조건 충족 자료**를 모두 처리합니다.
- 전날 새로 등록된 자료와 그 이후 실행 시점까지 추가된 자료도 포함합니다. PDF 다운로드나 전송에 실패한 자료는 대기열에 남겨 다음 실행에서 다시 시도합니다.
- 발간일과 사이트 등록 시점은 다를 수 있습니다. **발간일이 오래돼도 새 등록으로 확인한 자료를 발간일만으로 제외하지 않습니다.**
- 같은 자료가 양쪽 사이트에 올라오면 제목·증권사·발간일과 PDF 내용 지문으로 중복 발송을 줄입니다. 발송 완료는 텔레그램의 파일 전송 성공 뒤 기록합니다.
- 처음 목록으로 저장했던 한경 52건·네이버 60건 중 미발송 자료도 대기열에서 검토합니다. 이미 실제로 보낸 자료는 완료 기록으로 구분합니다.
- 설정 이전의 전체 아카이브를 무제한 소급 발송하지는 않습니다. 기존 기준 목록 밖의 오래된 과거 자료는 `historical_pre_start`로 구분하고, 초기 기준점 이후의 새 등록 자료를 추적합니다.

이번 전환 후 다음 정기 실행은 **2026년 10월 26일 월요일 17:00(한국시간)**입니다. 그때까지 발견한 새 등록 자료와 기존 대기열의 미발송 자료가 대상입니다. 따라서 “오늘까지 등록되지 않은 자료만” 보내는 방식이 아닙니다. 이전에 링크로 보낸 5건은 별도 수동 작업에서 PDF 첨부로 다시 보낸 뒤 완료로 기록합니다.

새 자료 탐색은 사이트가 제공하는 최근 **45일 조회 범위**를 기본으로 하며, 사이트별 최대 100페이지를 확인합니다. 이미 발견해 대기열에 저장한 미발송 자료는 이 범위 밖으로 밀려나도 재시도합니다. 사이트가 공개하지 않는 등록 시각이나 조회 범위 밖으로 늦게 들어온 자료까지 완전하게 추적한다고 보장하지는 않습니다.

GitHub 예약 실행은 지연되거나 건너뛰어질 수 있어 17:00 정각 도착을 보장하지 않습니다. 워크플로의 `0 8 * * *`는 UTC 08:00, 한국시간 17:00입니다.

## 선별 기준

- 제목이나 PDF 앞 2쪽에 `In-depth`, `심층 분석·리포트·보고서`, `산업 대해부` 표시가 있거나 PDF가 기본 **20쪽 이상**인 산업 자료를 선별합니다.
- 제목의 개별 종목 코드·목표주가·투자의견 표시를 추가로 걸러냅니다. 산업 리포트 안에 여러 기업 분석이 포함된 경우는 대상입니다.
- 일간·주간·월간, 브리핑·브리프, 실적·어닝 프리뷰·리뷰 등의 정기 자료 표시가 있는 제목은 제외합니다. 네이버 본문의 첫 강조 제목이나 PDF 표지의 정기 자료 표시도 확인합니다.
- 사람이 판단하는 심층 리포트와 차이가 있을 수 있습니다. `MIN_PDF_PAGES`로 분량 기준을 조정할 수 있습니다.
- 검사 가능한 PDF 최대 크기는 25 MiB입니다. 다운로드·분석·첨부 전송에 실패한 자료는 완료로 표시하지 않습니다.

## 휴대폰에서 봇 연결

1. 공식 [@BotFather](https://t.me/BotFather)에서 `/newbot`으로 봇을 만들고, 만든 봇의 개인 대화방에 `/start`를 보냅니다.
2. 저장소 **Settings → Secrets and variables → Actions → New repository secret**에서 `TELEGRAM_BOT_TOKEN`을 등록합니다.
3. **Actions → Connect Telegram recipient → Run workflow**를 실행합니다. 개인 대화방이 하나뿐이면 봇이 그곳으로 채팅 ID와 등록 링크를 보냅니다. 공개 로그에는 채팅 ID를 표시하지 않습니다.
4. 받은 숫자를 `TELEGRAM_CHAT_ID` Secret으로 등록합니다.
5. **Actions → Industry report alerts → Run workflow**에서 `send_test`를 체크해 테스트 메시지를 확인합니다. `preview_only`는 체크하지 않습니다.

| Secret 이름 | 값 |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | BotFather에서 받은 봇 토큰 |
| `TELEGRAM_CHAT_ID` | 봇이 개인 대화방으로 보낸 수신자 ID 숫자 |

두 Secret 중 하나라도 비어 있으면 실행은 오류 없이 연결 대기 상태로 종료합니다. 초록색 표시만으로 완료를 판단하지 말고 실제 테스트 메시지 도착과 수집 단계 실행을 확인하세요.

이 저장소의 연결 확인 워크플로는 `YBDIndepthreportcodex_bot`을 확인합니다. 다른 봇을 사용한다면 `.github/workflows/telegram-connect.yml`의 `EXPECTED_BOT_USERNAME`을 변경하세요. 개인 대화방이 여러 개거나 `/start` 기록이 없으면 수신자를 자동 선택하지 않습니다.

## 실행 모드와 알림 기록

**Industry report alerts**는 매일 오후 5시에 실행하며 수동 실행도 가능합니다. `preview_only`를 켜면 자료를 수집·분석해 미발송 대기열을 저장하지만 PDF는 보내지 않습니다. `send_test`도 이 모드에서는 실행하지 않습니다. 미리보기 뒤 다음 정기 실행에서 조건에 맞는 대기 자료를 보냅니다.

**Repair previous five as PDF attachments**는 이전에 링크로 보낸 정확한 5건을 PDF 문서로 다시 보내고, 성공한 건을 발송 완료로 기록하는 수동 작업입니다. 정기 실행과 같은 `industry-report-monitor` 실행 그룹을 사용해 기록 저장이 충돌하지 않게 합니다.

**Send five recent industry reports**는 요청 시 기존 자료 5건을 고르는 별도 수동 작업입니다. 정기 알림 기록을 변경하지 않으며, 다시 실행하면 같은 자료가 포함될 수 있습니다.

발송 상태와 미처리 자료의 공개 메타데이터는 `notification-state` 브랜치의 `state.sqlite3`에 보관합니다. **이 브랜치를 삭제하지 마세요.** 브랜치는 있는데 파일이 없으면 임의 초기화하지 않고 중단합니다. 이전 커밋의 파일을 복원한 뒤 실행하세요.

사이트가 일시적으로 실패해도 이미 저장한 대기 자료는 남아 있습니다. 발송 성공 뒤 DB 저장이나 Git push가 실패하면 다음 실행에서 중복될 수 있습니다. 전송 결과가 불확실한 네트워크 오류는 한 실행 안에서 무조건 재전송하지 않습니다.

## 새 GitHub 저장소에 설치

현재 저장소 외에 직접 설치하려면 다음 두 방법 중 하나를 사용합니다. 공개 저장소는 표준 GitHub 실행 환경을 무료로 사용할 수 있습니다. 비공개 저장소는 계정의 Actions 무료 한도와 과금 설정을 확인하세요.

### 설정 도우미

[Python 3.12 이상](https://www.python.org/downloads/), [Git](https://git-scm.com/downloads), [GitHub CLI](https://cli.github.com)를 설치하고 프로젝트를 기존 Git 저장소 밖의 새 폴더에 풉니다.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
gh auth login --scopes workflow
python scripts/deploy_github.py
```

Windows PowerShell의 활성화 명령은 `.venv\Scripts\Activate.ps1`입니다. 이미 로그인했다면 `gh auth login`은 생략할 수 있습니다. 업로드 권한이 없으면 `gh auth refresh -s workflow`를 실행하세요.

도우미는 토큰을 숨겨 입력받고 개인 대화방·새 저장소 이름을 확인한 뒤 **공개 저장소 생성 → 프로그램 업로드 → Secrets 등록 → 첫 실행 요청**을 진행합니다. 토큰은 파일이나 명령줄 인수에 저장하지 않습니다. 기존 Git 저장소는 자동 변경하지 않습니다.

중간에 실패했다면 도우미를 다시 실행하지 말고 완료된 단계를 확인해 수동으로 이어가세요. 저장소와 `origin`이 만들어진 뒤 push만 실패했다면 `gh auth refresh -s workflow` 후 `git push --set-upstream origin main`으로 업로드를 마칠 수 있습니다.

### 수동 설치

1. [GitHub](https://github.com/new)에서 공개 저장소를 만듭니다.
2. 최상위에 `requirements.txt`, `reportbot/`, `scripts/`, `.github/workflows/`가 오도록 업로드합니다. `.env`, `.venv`, 로컬 알림 DB는 올리지 않습니다.
3. 위의 휴대폰 연결 절차로 두 Secrets를 등록합니다.
4. **Industry report alerts**를 수동 실행해 테스트 메시지·양쪽 수집·상태 저장 성공을 확인합니다.

예약 실행은 기본 브랜치에 워크플로가 있어야 동작합니다. 저장소 정책이 Actions 쓰기를 막으면 **Settings → Actions → General → Workflow permissions**에서 읽기·쓰기를 허용해야 합니다.

## 로컬에서 확인하기

`.env.example`을 `.env`로 복사해 토큰과 채팅 ID를 채우면 자동으로 읽습니다. 아래 명령은 프로젝트 폴더에서 실행합니다.

```bash
cp .env.example .env
python -m reportbot test-message
python -m reportbot preview
python -m reportbot check
```

`preview`는 대기열을 갱신하고 분석하되 보내지 않습니다. `check`는 미발송 자료를 확인해 발송합니다. 로컬 프로세스가 꺼져 있어도 GitHub 예약 실행은 계속됩니다.

## 설정

GitHub에서는 워크플로 `env`를, 로컬에서는 `.env`를 바꿉니다.

| 이름 | 기본값 | 용도 |
| --- | --- | --- |
| `MIN_PDF_PAGES` | `20` | 심층 표기가 없는 PDF의 최소 쪽수 |
| `MAX_LIST_PAGES` | `100` | 사이트별 탐색 페이지 상한 |
| `SCAN_LOOKBACK_DAYS` | `45` | 새 등록 탐색을 위한 공개 목록 조회 기간 |
| `POLL_SECONDS` | `86400` | 선택형 로컬 daemon의 반복 간격; GitHub 17시 예약과는 별도 |
| `STATE_PATH` | `data/state.sqlite3` | 발송·미처리 대기열 기록 파일 |

발간일 나이를 기준으로 미발송 자료를 버리는 설정은 사용하지 않습니다. 이미 대기열에 들어간 자료는 탐색 기간이 지나도 유지됩니다. GitHub 실행 시점은 `POLL_SECONDS`가 아니라 `cron`으로 정합니다.

## 운영과 점검

- 알림이 없으면 Actions에서 Secrets 확인, 자료 수집, 발송, 상태 저장 단계가 실제 실행됐는지 확인합니다.
- 한 사이트 수집에 실패해도 다른 출처와 저장된 대기열은 계속 처리합니다. 종료 뒤 `always()` 저장 단계에서 성공한 발송 기록을 보관합니다.
- 확인·발송 단계는 50분, 작업 전체는 60분으로 제한해 별도 기록 저장 시간을 확보합니다. 강제 종료나 기록 push 실패 시 마지막 변경이 보관되지 않을 수 있습니다.
- `401`은 토큰, `403` 또는 채팅 오류는 봇의 `/start` 여부와 수신자 ID를 확인합니다. 비밀값을 로그에 출력하지 않습니다.
- 자동화를 멈추려면 Actions에서 **Industry report alerts**를 비활성화합니다.

개발 점검:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

"""Interactive, opt-in deployment from an extracted project folder.

Run with: python scripts/deploy_github.py
This script creates a public GitHub repository only after a final confirmation.
"""

from __future__ import annotations

import getpass
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class SetupError(RuntimeError):
    pass


def command(*args: str, secret_input: str | None = None) -> str:
    """Capture output; never echo arguments or the secret supplied over stdin."""
    result = subprocess.run(
        list(args), cwd=ROOT, input=secret_input, text=True, capture_output=True
    )
    if result.returncode:
        # Secret-setting errors are deliberately generic; no secret can be echoed.
        if secret_input is not None:
            raise SetupError("GitHub Secret 등록에 실패했습니다. 저장소 권한을 확인하세요.")
        detail = (result.stderr or result.stdout).strip()
        raise SetupError(f"{args[0]} 작업 실패: {detail[:1500]}")
    return result.stdout.strip()


def prerequisites() -> str:
    missing = [name for name in ("git", "gh") if shutil.which(name) is None]
    if missing:
        raise SetupError(
            f"필요한 프로그램이 없습니다: {', '.join(missing)}. "
            "Git(https://git-scm.com/downloads)과 GitHub CLI(https://cli.github.com)를 "
            "설치한 뒤 새 터미널에서 다시 실행하세요."
        )
    if any((folder / ".git").exists() for folder in (ROOT, *ROOT.parents)):
        raise SetupError(
            "이 폴더 또는 상위 폴더에 기존 Git 저장소가 있습니다. "
            "기존 저장소를 바꾸지 않도록 중단합니다. 압축을 별도 폴더에 새로 풀고 실행하세요. "
            "앞선 설정이 일부 완료되었다면 README의 수동 설정 방법을 이용하세요."
        )
    check = subprocess.run(
        ["gh", "auth", "status"], cwd=ROOT, text=True, capture_output=True
    )
    if check.returncode:
        raise SetupError(
            "GitHub CLI 로그인이 필요합니다. 터미널에서 gh auth login --scopes workflow를 실행하고 "
            "GitHub.com 계정으로 로그인한 뒤 다시 실행하세요. "
            "GH_TOKEN 등 환경변수를 설정했다면 해당 자격 증명도 확인하세요."
        )
    login = command("gh", "api", "user", "--jq", ".login")
    if not re.fullmatch(r"[A-Za-z0-9-]+", login):
        raise SetupError("GitHub 사용자 이름을 확인하지 못했습니다.")
    return login


def telegram_recipient() -> tuple[str, str, str]:
    try:
        from reportbot.telegram import Telegram, TelegramError
    except ImportError as exc:
        raise SetupError(
            "의존성을 설치하세요: python -m pip install -r requirements.txt"
        ) from exc

    token = getpass.getpass("BotFather에서 받은 봇 토큰 (화면에 표시되지 않음): ").strip()
    try:
        telegram = Telegram(token)
        identity = telegram.call("getMe", {})
        print(f"텔레그램에서 https://t.me/{identity['username']} 을 열고 /start를 보내세요.")
        input("보낸 뒤 Enter를 누르세요: ")
        updates = telegram.call(
            "getUpdates", {"timeout": 0, "allowed_updates": ["message"]}
        )
    except TelegramError as exc:
        raise SetupError(str(exc)) from None
    chats: dict[str, str] = {}
    for update in updates:
        chat = update.get("message", {}).get("chat", {})
        if chat.get("type") == "private":
            label = str(chat.get("first_name", "개인 대화방"))
            chats[str(chat["id"])] = "".join(c for c in label if c.isprintable())
    if not chats:
        raise SetupError(
            "개인 대화방을 찾지 못했습니다. 만든 봇에 /start를 보내고 다시 실행하세요. "
            "기존 webhook이 설정된 봇은 먼저 webhook을 해제해야 합니다."
        )
    choices = list(chats.items())
    for index, (chat_id, name) in enumerate(choices, 1):
        print(f"  {index}. {name} (채팅 ID: {chat_id})")
    if len(choices) == 1:
        chosen = choices[0]
    else:
        answer = input("본인의 대화방 번호를 입력하세요: ").strip()
        if not answer.isdecimal() or not 1 <= int(answer) <= len(choices):
            raise SetupError("올바른 대화방 번호를 입력한 뒤 다시 실행하세요.")
        chosen = choices[int(answer) - 1]
    return token, chosen[0], chosen[1]


def deploy() -> int:
    print("산업 리포트 알림 자동 설정 — GitHub 공개 저장소를 만듭니다.")
    login = prerequisites()
    token, chat_id, recipient = telegram_recipient()
    repo_name = input("새 저장소 이름 [industry-report-alerts]: ").strip() or "industry-report-alerts"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", repo_name):
        raise SetupError("저장소 이름은 영문·숫자로 시작하고 영문·숫자·점·밑줄·하이픈만 사용하세요.")
    repository = f"{login}/{repo_name}"
    print(f"\n생성할 공개 저장소: {repository}")
    print(f"텔레그램 수신자: {recipient} (채팅 ID: {chat_id})")
    print("프로그램을 올리고, 봇 토큰·채팅 ID를 GitHub Secrets에 등록합니다.")
    print("연결 테스트 메시지를 보내고 매일 한국시간 오후 5시에 확인하는 자동화를 시작합니다.")
    print("조건에 맞는 미발송 자료를 PDF 원문 첨부로 보냅니다.")
    if input("이 설정을 진행하려면 yes를 입력하세요: ").strip().lower() != "yes":
        print("취소했습니다. Git 저장소나 GitHub 설정은 변경하지 않았습니다.")
        return 0

    command("git", "init", "--initial-branch=main")
    # Use gh's existing login for this repository without changing global Git config.
    command("git", "config", "--local", "credential.https://github.com.helper", "")
    command("git", "config", "--local", "--add", "credential.https://github.com.helper", "!gh auth git-credential")
    for setting, fallback in (
        ("user.name", "Industry report setup"),
        ("user.email", "industry-report-setup@users.noreply.github.com"),
    ):
        existing = subprocess.run(
            ["git", "config", "--get", setting], cwd=ROOT, text=True, capture_output=True
        )
        if existing.returncode != 0 or not existing.stdout.strip():
            command("git", "config", "--local", setting, fallback)
    paths = [
        "README.md", "requirements.txt", "requirements-dev.txt", ".gitignore",
        ".env.example", "reportbot", "scripts", "tests", ".github",
    ]
    command("git", "add", "--", *(path for path in paths if (ROOT / path).exists()))
    command("git", "commit", "-m", "Add industry research Telegram monitor")
    command("gh", "repo", "create", repository, "--public", "--source", str(ROOT), "--push")
    print(f"공개 저장소를 만들었습니다: https://github.com/{repository}")
    command("gh", "secret", "set", "TELEGRAM_BOT_TOKEN", "--repo", repository, secret_input=token)
    command("gh", "secret", "set", "TELEGRAM_CHAT_ID", "--repo", repository, secret_input=chat_id)
    print("텔레그램 설정을 GitHub Secrets에 등록했습니다.")
    # GitHub may need a few seconds to index a newly pushed workflow.
    started = False
    for attempt in range(3):
        try:
            command(
                "gh", "workflow", "run", "monitor.yml", "--repo", repository,
                "--ref", "main", "-f", "send_test=true",
            )
            started = True
            break
        except SetupError:
            if attempt < 2:
                time.sleep(3)
    if started:
        print("첫 실행을 요청했습니다. 텔레그램 테스트 메시지와 Actions 결과를 확인하세요.")
    else:
        print("저장소와 Secrets 등록은 완료했지만 첫 실행 요청을 확인하지 못했습니다.")
        print("Actions → Industry report alerts → Run workflow에서 send_test를 체크해 실행하세요.")
    print(f"실행 상태: https://github.com/{repository}/actions")
    print("계정의 Actions 사용 한도·과금 설정을 확인하세요. notification-state 브랜치는 삭제하지 마세요.")
    return 0 if started else 1


if __name__ == "__main__":
    try:
        raise SystemExit(deploy())
    except (KeyboardInterrupt, EOFError):
        print("\n설정을 중단했습니다. 이미 만든 저장소가 있으면 README의 수동 설정으로 이어가세요.")
        raise SystemExit(1)
    except SetupError as exc:
        print(f"설정 실패: {exc}", file=sys.stderr)
        print("일부 단계가 완료되었다면 README의 수동 설정으로 이어갈 수 있습니다.", file=sys.stderr)
        raise SystemExit(1)

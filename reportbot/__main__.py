import argparse
import getpass
import logging
import sys
import time

from filelock import FileLock, Timeout

from .config import Config
from .telegram import Telegram, TelegramError


def setup(config: Config) -> int:
    token = config.token or getpass.getpass("@BotFather에서 받은 봇 토큰 (화면에 표시되지 않음): ").strip()
    telegram = Telegram(token)
    identity = telegram.call("getMe", {})
    print(f"텔레그램에서 https://t.me/{identity['username']} 을 열고 /start 를 보내세요.")
    input("보낸 뒤 Enter를 누르세요: ")
    updates = telegram.call("getUpdates", {"timeout": 0, "allowed_updates": ["message"]})
    chats = {}
    for update in updates:
        chat = update.get("message", {}).get("chat", {})
        if chat.get("type") == "private":
            chats[str(chat["id"])] = chat.get("first_name", "개인 채팅")
    if not chats:
        print("개인 채팅을 찾지 못했습니다. 봇에 /start를 보내고 다시 실행하세요. 기존 webhook이 있으면 먼저 해제해야 합니다.")
        return 1
    for chat_id, name in chats.items():
        print(f"{name}: TELEGRAM_CHAT_ID={chat_id}")
    print("본인의 chat ID와 봇 토큰을 GitHub Actions Secrets에 각각 등록하세요. 토큰은 채팅에 올리지 마세요.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="산업 심층 리포트 → 텔레그램 알림")
    commands = parser.add_subparsers(dest="command", required=True)
    once = commands.add_parser("check", help="미발송 산업 심층 리포트 모두 PDF 첨부 발송")
    once.add_argument("--dry-run", action="store_true", help="현재 후보 분석 미리보기; 상태·발송 없음")
    commands.add_parser("telegram-setup", help="봇 확인 및 개인 chat ID 찾기")
    commands.add_parser("test-message", help="등록된 개인 채팅에 테스트 메시지 발송")
    commands.add_parser("daemon", help="이 프로세스가 켜져 있는 동안 주기적으로 확인")
    commands.add_parser("preview", help="발송 없이 미발송 대기열과 PDF 후보 확인·저장")
    commands.add_parser("repair-five", help="이전에 링크로 보낸 5건을 PDF 첨부로 교체하고 발송 기록 저장")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = Config.from_env()
        if args.command == "telegram-setup":
            return setup(config)
        if args.command == "test-message":
            result = Telegram(config.token, config.chat_id).send("✅ 산업 심층 리포트 알림 연결 테스트입니다. 한경 컨센서스·네이버 산업 리포트를 확인합니다.")
            destination = result.get("chat", {}).get("type") if isinstance(result, dict) else None
            print("그룹방으로 테스트 메시지를 보냈습니다." if destination in {"group", "supergroup"} else "개인 대화방으로 테스트 메시지를 보냈습니다.")
            return 0
        from .runner import check, repair_five

        if args.command == "check" and args.dry_run:
            return check(config, dry_run=True)
        config.state_path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(config.state_path) + ".lock", timeout=0):
            if args.command == "check":
                return check(config)
            if args.command == "preview":
                return check(config, preview_only=True)
            if args.command == "repair-five":
                return repair_five(config)
            while True:
                check(config)
                time.sleep(config.poll_seconds)
    except Timeout:
        print("이미 다른 알림 프로세스가 실행 중입니다.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    except (TelegramError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        # A network exception can include the bot-token URL; avoid a traceback by default.
        print(f"실행 실패 ({type(exc).__name__}). 설정과 네트워크를 확인하세요.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

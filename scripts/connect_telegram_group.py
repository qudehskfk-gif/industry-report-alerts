"""Confirm an owner's Telegram group and send its ID only to the owner privately."""

import os
import re
import sys

from .connect_telegram import ConnectionSetupError, call_api


SECRET_URL = "https://github.com/qudehskfk-gif/industry-report-alerts/settings/secrets/actions/TELEGRAM_CHAT_ID"


def connect_group(token: str, owner_chat_id: str, expected_username: str = "") -> None:
    token = token.strip()
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise ConnectionSetupError("GitHub Secret TELEGRAM_BOT_TOKEN을 확인하세요.")
    try:
        owner_id = int(owner_chat_id)
    except (TypeError, ValueError):
        raise ConnectionSetupError("기존 개인 수신자의 TELEGRAM_CHAT_ID Secret이 필요합니다.") from None
    if owner_id < 0:
        raise ConnectionSetupError("현재 수신처가 이미 그룹입니다. 기존 그룹 수신 설정을 확인하세요.")
    if owner_id == 0:
        raise ConnectionSetupError("기존 개인 수신자의 TELEGRAM_CHAT_ID Secret이 필요합니다.")

    identity = call_api(token, "getMe", {})
    if not isinstance(identity, dict) or not identity.get("is_bot"):
        raise ConnectionSetupError("텔레그램 봇 계정을 확인하지 못했습니다.")
    username = identity.get("username", "")
    if not isinstance(username, str) or not username:
        raise ConnectionSetupError("텔레그램 봇 계정 이름을 확인하지 못했습니다.")
    if expected_username and username.lower() != expected_username.lstrip("@").lower():
        raise ConnectionSetupError("등록된 토큰의 봇 계정이 예상한 계정과 다릅니다.")

    updates = call_api(token, "getUpdates", {"timeout": 0, "limit": 100, "allowed_updates": ["message"]})
    if not isinstance(updates, list):
        raise ConnectionSetupError("텔레그램 대화 목록 응답을 확인하지 못했습니다.")
    groups = set()
    for update in updates:
        if not isinstance(update, dict):
            continue
        message = update.get("message")
        if not isinstance(message, dict):
            continue
        chat, sender = message.get("chat"), message.get("from")
        if not isinstance(chat, dict) or not isinstance(sender, dict):
            continue
        chat_id, sender_id = chat.get("id"), sender.get("id")
        if chat.get("type") not in {"group", "supergroup"} or type(chat_id) is not int or chat_id >= 0:
            continue
        if type(sender_id) is not int or sender_id != owner_id or sender.get("is_bot"):
            continue
        text = message.get("text")
        words = text.strip().split() if isinstance(text, str) else []
        if not words:
            continue
        command = words[0].lower()
        started = command == f"/start@{username.lower()}" and words[1:] == ["reports"]
        connected = command == f"/connect@{username.lower()}" and len(words) == 1
        if started or connected:
            groups.add(chat_id)

    if not groups:
        raise ConnectionSetupError("본인의 그룹방에 봇을 추가하고 /connect@봇아이디 를 보낸 뒤 다시 실행하세요.")
    if len(groups) != 1:
        raise ConnectionSetupError("연결 요청을 보낸 그룹이 여러 개여서 자동 선택하지 않았습니다. 연결할 그룹을 확인하세요.")
    group_id = next(iter(groups))
    call_api(token, "sendMessage", {
        "chat_id": group_id,
        "text": "✅ 산업 심층 리포트를 함께 받을 그룹을 확인했습니다.\n수신처 설정 안내를 봇 소유자의 개인 대화방으로 보냅니다. 설정을 마치면 매일 한국시간 오후 5시에 이 그룹으로 리포트를 보냅니다.",
        "link_preview_options": {"is_disabled": True},
    })
    call_api(token, "sendMessage", {
        "chat_id": owner_id,
        "text": (
            "✅ 리포트를 받을 그룹을 확인했습니다.\n\n"
            "아래 GitHub 화면에서 기존 TELEGRAM_CHAT_ID Secret을 수정해 주세요.\n"
            "Name: TELEGRAM_CHAT_ID\n"
            f"새 Secret 값: {group_id}\n\n"
            "앞의 마이너스(-) 부호까지 모두 복사해 기존 값을 교체하세요.\n"
            f"{SECRET_URL}\n\n"
            "변경하면 앞으로 리포트는 개인 대화방 대신 이 그룹으로 발송됩니다.\n"
            "봇 토큰은 변경하지 않습니다. 등록이 끝나면 ChatGPT에 ‘그룹 ID 등록했어’라고 알려 주세요."
        ),
        "link_preview_options": {"is_disabled": True},
    })
    print("선택한 그룹과 기존 개인 대화방으로 연결 안내를 보냈습니다. 토큰·채팅 ID·그룹 이름은 로그에 표시하지 않습니다.")


def main() -> int:
    try:
        connect_group(
            os.getenv("TELEGRAM_BOT_TOKEN", ""),
            os.getenv("TELEGRAM_CHAT_ID", ""),
            os.getenv("EXPECTED_BOT_USERNAME", ""),
        )
        return 0
    except ConnectionSetupError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        print("그룹 연결 확인 중 오류가 발생했습니다. 토큰이나 대화 내용은 로그에 표시하지 않습니다.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

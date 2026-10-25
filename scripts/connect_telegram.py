"""Deliver the recipient ID to its private Telegram chat, never to public logs."""

import json
import os
import re
import sys
from urllib.error import URLError
from urllib.request import Request, urlopen


class ConnectionSetupError(RuntimeError):
    pass


def call_api(token: str, method: str, payload: dict):
    request = Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            body = json.loads(response.read(2 * 1024 * 1024))
    except (URLError, OSError, ValueError):
        raise ConnectionSetupError("텔레그램 연결 실패. 봇 토큰과 네트워크를 확인하세요.") from None
    if not isinstance(body, dict) or not body.get("ok") or "result" not in body:
        raise ConnectionSetupError("텔레그램 API 요청 실패. 토큰·봇 차단·webhook 설정을 확인하세요.")
    return body["result"]


def connect(token: str, expected_username: str = "") -> None:
    token = token.strip()
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise ConnectionSetupError("GitHub Secret TELEGRAM_BOT_TOKEN을 확인하세요.")
    identity = call_api(token, "getMe", {})
    if not isinstance(identity, dict) or not identity.get("is_bot"):
        raise ConnectionSetupError("텔레그램 봇 계정을 확인하지 못했습니다.")
    if expected_username and identity.get("username", "").lower() != expected_username.lstrip("@").lower():
        raise ConnectionSetupError("등록된 토큰의 봇 계정이 예상한 계정과 다릅니다.")
    updates = call_api(token, "getUpdates", {"timeout": 0, "limit": 100, "allowed_updates": ["message"]})
    if not isinstance(updates, list):
        raise ConnectionSetupError("텔레그램 대화 목록 응답을 확인하지 못했습니다.")
    chats = {}
    started = set()
    for update in updates:
        if not isinstance(update, dict):
            continue
        message = update.get("message", {})
        chat = message.get("chat", {})
        chat_id = chat.get("id")
        if chat.get("type") != "private" or not isinstance(chat_id, int) or chat_id <= 0:
            continue
        chats[chat_id] = True
        text = message.get("text", "")
        words = text.strip().split() if isinstance(text, str) else []
        if words and words[0].split("@")[0] == "/start":
            started.add(chat_id)
    if not chats:
        raise ConnectionSetupError("봇 개인 대화방에 /start를 보낸 뒤 다시 실행하세요.")
    if len(chats) != 1:
        raise ConnectionSetupError("개인 대화방이 여러 개여서 자동 선택하지 않았습니다. 수신자 ID를 직접 확인해 등록하세요.")
    chat_id = next(iter(chats))
    if chat_id not in started:
        raise ConnectionSetupError("봇 개인 대화방에 /start를 다시 보낸 뒤 실행하세요.")
    text = (
        "✅ 산업 심층 리포트 봇 연결을 확인했습니다.\n\n"
        "GitHub에서 수신자 Secret을 하나 더 등록해 주세요.\n"
        "Name: TELEGRAM_CHAT_ID\n"
        f"Secret: {chat_id}\n\n"
        "위 Secret의 숫자를 복사해서 아래 화면에 붙여 넣으면 됩니다.\n"
        "https://github.com/qudehskfk-gif/industry-report-alerts/settings/secrets/actions/new\n\n"
        "등록이 끝나면 ChatGPT에 ‘채팅 ID 등록했어’라고 알려 주세요."
    )
    call_api(token, "sendMessage", {"chat_id": chat_id, "text": text, "link_preview_options": {"is_disabled": True}})
    print("개인 텔레그램으로 연결 안내를 보냈습니다. 토큰·채팅 ID는 로그에 표시하지 않습니다.")


def main() -> int:
    try:
        connect(os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("EXPECTED_BOT_USERNAME", ""))
        return 0
    except ConnectionSetupError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        print("봇 연결 확인 중 오류가 발생했습니다. 토큰이나 대화 내용은 로그에 표시하지 않습니다.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

import html
import time

import requests

from .classify import Decision
from .models import Report


class TelegramError(RuntimeError):
    pass


class Telegram:
    def __init__(self, token: str, chat_id: str = ""):
        if not token or not token.strip() or ":" not in token:
            raise TelegramError("TELEGRAM_BOT_TOKEN이 필요합니다. @BotFather에서 봇을 만든 뒤 등록하세요.")
        self.token = token
        self.chat_id = chat_id
        # Never attach retrying adapters to POST: an uncertain timeout can duplicate a message.
        self.session = requests.Session()

    def call(self, method: str, payload: dict):
        for attempt in range(3):
            try:
                response = self.session.post(f"https://api.telegram.org/bot{self.token}/{method}", json=payload, timeout=(10, 35))
            except requests.RequestException:
                raise TelegramError("텔레그램 연결 실패. 토큰·네트워크를 확인하세요. 전송 결과가 불확실하면 중복될 수 있습니다.") from None
            try:
                body = response.json()
            except ValueError:
                raise TelegramError(f"텔레그램 응답 형식 오류 (HTTP {response.status_code})") from None
            if response.status_code == 429 and attempt < 2:
                delay = int(body.get("parameters", {}).get("retry_after", 1))
                if delay > 30:
                    raise TelegramError("텔레그램 요청 제한. 다음 실행에서 재시도합니다.")
                time.sleep(max(delay, 1))
                continue
            if not response.ok or not body.get("ok"):
                # Remote exception text may echo sensitive material; only expose a code.
                raise TelegramError(f"텔레그램 API 오류 ({body.get('error_code', response.status_code)}). 토큰·chat ID·봇 차단 여부를 확인하세요.")
            return body["result"]
        raise TelegramError("텔레그램 요청 제한")

    def send(self, text: str):
        if not self.chat_id:
            raise TelegramError("TELEGRAM_CHAT_ID가 필요합니다. python -m reportbot telegram-setup을 실행하세요.")
        return self.call("sendMessage", {
            "chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        })

    def send_report(self, report: Report, decision: Decision):
        esc = html.escape
        lines = [
            "📚 <b>새 산업 심층 리포트</b>",
            f"<b>{esc(report.title[:600])}</b>",
            f"{esc(report.broker)} · {report.published_date.isoformat()}",
            f"출처: {esc({'hankyung': '한경 컨센서스', 'naver': '네이버 금융'}.get(report.source, report.source))}",
            f"선정: {esc(decision.reason)}",
            f'<a href="{esc(report.detail_url, quote=True)}">원문 보기</a>',
        ]
        if report.pdf_url:
            lines.append(f'<a href="{esc(report.pdf_url, quote=True)}">PDF 보기</a>')
        return self.send("\n".join(lines))

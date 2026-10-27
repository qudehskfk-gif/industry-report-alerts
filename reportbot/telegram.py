import hashlib
import html
from html.parser import HTMLParser
import io
import logging
import os
import re
import time
import unicodedata

import requests

from .classify import Decision, MAX_PDF_BYTES
from .http import make_session
from .models import Report
from .summarize import SummaryError, summarize_pdf, validate_summary_sections

log = logging.getLogger(__name__)


class TelegramError(RuntimeError):
    pass


def _utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le", errors="ignore")) // 2


class _CaptionText(HTMLParser):
    """Collect the visible text of our fixed, escaped Telegram HTML markup."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def _caption_utf16_length(caption: str) -> int:
    # Telegram's caption limit applies after entity parsing. Formatting tags,
    # escaped entity spellings and a link's href do not consume visible space.
    parser = _CaptionText()
    parser.feed(caption)
    parser.close()
    return _utf16_length("".join(parser.parts))


def _escaped_trim(text: str, limit: int) -> str:
    """Bound raw HTML units without cutting an entity or a Unicode character."""
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", str(text))
    result = []
    used = 0
    for character in cleaned:
        escaped = html.escape(character, quote=True)
        size = _utf16_length(escaped)
        if used + size > limit - 1:
            result.append("…")
            break
        result.append(escaped)
        used += size
    return "".join(result)


def pdf_filename(report: Report) -> str:
    text = f"{report.published_date:%Y_%m_%d}_{report.broker}_{report.title}"
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]+', "_", text)
    text = re.sub(r"\s+", "_", text).strip(" ._") or "industry_report"
    # Leave room for the extension, preserving complete UTF-8 characters.
    stem = text.encode("utf-8", errors="ignore")[:176].decode("utf-8", errors="ignore").rstrip(" ._")
    return stem + ".pdf"


def report_caption(report: Report, decision: Decision) -> str:
    source = {"hankyung": "한경 컨센서스", "naver": "네이버 금융"}.get(report.source, report.source)
    pages = f"{decision.pages}쪽" if decision.pages is not None else "미확인"
    lines = [
        "📚 <b>산업 심층 리포트</b>",
        f"<b>{_escaped_trim(report.title, 350)}</b>",
        f"{_escaped_trim(report.broker, 90)} · {report.published_date.isoformat()}",
    ]
    if report.sector:
        lines.append(f"섹터: {_escaped_trim(report.sector, 80)}")
    lines += [
        f"출처: {_escaped_trim(source, 35)}",
        f"분량: {_escaped_trim(pages, 30)}",
        f"선정: {_escaped_trim(decision.reason, 120)}",
        "첨부: PDF 원문",
    ]
    caption = "\n".join(lines)
    link = f'<a href="{html.escape(report.detail_url, quote=True)}">원문 페이지</a>'
    # Very long upstream URLs are omitted instead of being truncated into a bad link.
    if _utf16_length(caption + "\n" + link) <= 1024:
        caption += "\n" + link
    return caption


def report_error_message(report: Report, reason: str) -> str:
    """Link to the report page when a PDF could not be processed or attached.

    ``reason`` is a fixed user-facing stage description, never exception text.
    """
    source = {"hankyung": "한경 컨센서스", "naver": "네이버 금융"}.get(report.source, report.source)
    lines = [
        "⚠️ <b>PDF 첨부 대신 원문 링크 안내</b>",
        f"<b>{_escaped_trim(report.title, 600)}</b>",
        f"{_escaped_trim(report.broker, 120)} · {report.published_date.isoformat()}",
    ]
    if report.sector:
        lines.append(f"섹터: {_escaped_trim(report.sector, 100)}")
    lines += [
        f"출처: {_escaped_trim(source, 60)}",
        f"확인 단계: {_escaped_trim(reason, 160)}",
        "PDF 첨부를 완료하지 못해 원문 페이지 링크를 보냅니다.",
        "이 자료는 자동 재시도하지 않습니다.",
    ]
    text = "\n".join(lines)
    link = f'<a href="{html.escape(report.detail_url, quote=True)}">리포트 원문 페이지 열기</a>'
    if _utf16_length(text + "\n" + link) > 4096:
        # Do not corrupt the URL or silently omit the requested report link.
        raise TelegramError("리포트 안내 메시지의 원문 주소가 텔레그램 길이 제한을 초과했습니다.")
    return text + "\n" + link


def report_summary_caption(report: Report, decision: Decision, summary: list[dict]) -> str:
    """Keep the complete sectioned summary beside the PDF in one caption."""
    try:
        sections = validate_summary_sections(summary)
    except SummaryError:
        raise TelegramError("리포트 요약은 제목 3개 또는 5개와 제목별 설명 3~5줄이어야 합니다.") from None
    body_lines = []
    for index, section in enumerate(sections, 1):
        if body_lines:
            body_lines.append("")
        body_lines.append(f"<b>{index}. {html.escape(section['heading'], quote=False)}</b>")
        body_lines.extend(f"• {html.escape(point, quote=False)}" for point in section["points"])
    body = "\n".join(body_lines)
    if _caption_utf16_length(body) > 1024:
        raise TelegramError("리포트 요약이 PDF 설명 길이 제한을 초과했습니다.")

    source = {"hankyung": "한경 컨센서스", "naver": "네이버 금융"}.get(report.source, report.source)
    pages = f"{decision.pages}쪽" if decision.pages is not None else "쪽수 미확인"

    def metadata(title_limit, broker_limit, sector_limit, source_limit):
        details = [_escaped_trim(source, source_limit), _escaped_trim(pages, 20)]
        if report.sector:
            details.insert(0, f"섹터: {_escaped_trim(report.sector, sector_limit)}")
        return "\n".join([
            f"📚 <b>{_escaped_trim(report.title, title_limit)}</b>",
            f"{_escaped_trim(report.broker, broker_limit)} · {report.published_date.isoformat()}",
            " · ".join(details),
        ])

    def compact_metadata(*, include_title):
        # Keep identification and sector details when the summary needs most
        # of the caption. The document's filename also carries its full title.
        lines = [f"{_escaped_trim(report.broker, 25)} · {report.published_date.isoformat()}"]
        details = []
        if report.sector:
            details.append(f"섹터: {_escaped_trim(report.sector, 25)}")
        details.append(_escaped_trim(pages, 20))
        lines.append(" · ".join(details))
        if include_title:
            lines.insert(0, f"📚 <b>{_escaped_trim(report.title, 20)}</b>")
        return "\n".join(lines)

    # Preserve every heading and explanation. Reduce optional metadata first.
    candidates = [metadata(120, 45, 40, 24), metadata(60, 25, 22, 18),
                  compact_metadata(include_title=True), compact_metadata(include_title=False),
                  metadata(30, 16, 12, 12),
                  f"📚 <b>{_escaped_trim(report.title, 24)}</b>", ""]
    caption = body
    for header in candidates:
        candidate = header + "\n\n" + body if header else body
        if _caption_utf16_length(candidate) <= 1024:
            caption = candidate
            break
    link = f'<a href="{html.escape(report.detail_url, quote=True)}">원문 페이지</a>'
    if _caption_utf16_length(caption + "\n" + link) <= 1024:
        caption += "\n" + link
    return caption


def report_summary_message(report: Report, summary: list[dict]) -> str:
    """Keep the sectioned formatter available for callers of the old helper."""
    return report_summary_caption(report, Decision(True, "AI 요약"), summary)


def download_pdf(url: str) -> bytes:
    """Use the same browser-compatible source client as listing/PDF inspection."""
    try:
        with make_session() as source_session:
            with source_session.get(url, stream=True, timeout=(10, 60)) as response:
                response.raise_for_status()
                length = response.headers.get("Content-Length")
                if length and int(length) > MAX_PDF_BYTES:
                    raise TelegramError("PDF가 첨부 검사 한도 25 MiB를 초과했습니다.")
                data = bytearray()
                for chunk in response.iter_content(chunk_size=65536):
                    data.extend(chunk)
                    if len(data) > MAX_PDF_BYTES:
                        raise TelegramError("PDF가 첨부 검사 한도 25 MiB를 초과했습니다.")
    except (requests.RequestException, ValueError):
        raise TelegramError("PDF 다운로드에 실패했습니다.") from None
    if b"%PDF-" not in data[:1024]:
        raise TelegramError("PDF 원문이 아닌 응답을 받아 첨부하지 않았습니다.")
    return bytes(data)


class Telegram:
    def __init__(self, token: str, chat_id: str = ""):
        if not token or not token.strip() or ":" not in token:
            raise TelegramError("TELEGRAM_BOT_TOKEN이 필요합니다. @BotFather에서 봇을 만든 뒤 등록하세요.")
        self.token = token
        self.chat_id = chat_id
        # Never attach retrying adapters to POST: an uncertain timeout can duplicate a message.
        self.session = requests.Session()

    def call(self, method: str, payload: dict, *, files: dict | None = None):
        for attempt in range(3):
            try:
                if files is None:
                    response = self.session.post(f"https://api.telegram.org/bot{self.token}/{method}", json=payload, timeout=(10, 35))
                else:
                    # Telegram's explicit 429 rejects the upload. Rewind for that
                    # retry; uncertain network failures are never replayed.
                    for attachment in files.values():
                        attachment[1].seek(0)
                    response = self.session.post(
                        f"https://api.telegram.org/bot{self.token}/{method}",
                        data=payload, files=files, timeout=(10, 120),
                    )
            except requests.RequestException:
                raise TelegramError("텔레그램 연결 실패. 토큰·네트워크를 확인하세요. 전송 결과가 불확실하면 중복될 수 있습니다.") from None
            try:
                body = response.json()
            except ValueError:
                raise TelegramError(f"텔레그램 응답 형식 오류 (HTTP {response.status_code})") from None
            if not isinstance(body, dict):
                raise TelegramError(f"텔레그램 응답 형식 오류 (HTTP {response.status_code})")
            if response.status_code == 429 and attempt < 2:
                parameters = body.get("parameters")
                try:
                    delay = int(parameters.get("retry_after", 1)) if isinstance(parameters, dict) else 1
                except (TypeError, ValueError):
                    raise TelegramError("텔레그램 요청 제한 응답 형식 오류") from None
                if delay > 30:
                    raise TelegramError("텔레그램 요청 제한으로 전송하지 못했습니다.")
                time.sleep(max(delay, 1))
                continue
            if not response.ok or not body.get("ok"):
                # A previous caption edit may have succeeded before its reply
                # was lost. Treat Telegram's exact idempotency condition as
                # completed, without repeating the PDF upload or leaking text.
                description = body.get("description")
                if (method == "editMessageCaption" and response.status_code == 400
                        and body.get("error_code") == 400 and isinstance(description, str)
                        and (description == "Bad Request: message is not modified"
                             or description.startswith("Bad Request: message is not modified:"))):
                    return {"message_id": payload.get("message_id"),
                            "_reportbot_edit_already_applied": True}
                # Remote exception text may echo sensitive material; only expose a code.
                code = body.get("error_code")
                code = code if isinstance(code, int) else response.status_code
                raise TelegramError(f"텔레그램 API 오류 ({code}). 토큰·chat ID·봇 차단 여부를 확인하세요.")
            return body["result"]
        raise TelegramError("텔레그램 요청 제한")

    def send(self, text: str):
        if not self.chat_id:
            raise TelegramError("TELEGRAM_CHAT_ID가 필요합니다. python -m reportbot telegram-setup을 실행하세요.")
        return self.call("sendMessage", {
            "chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        })

    def send_report_error(self, report: Report, reason: str):
        return self.send(report_error_message(report, reason))

    def send_report(self, report: Report, decision: Decision):
        if not self.chat_id:
            raise TelegramError("TELEGRAM_CHAT_ID가 필요합니다.")
        if not report.pdf_url:
            raise TelegramError("PDF 원문 주소가 아직 없어 첨부하지 않았습니다.")
        data = download_pdf(report.pdf_url)
        caption = report_caption(report, decision)
        summary_status = "disabled"
        summary_error = None
        summary_stage = "generation"
        try:
            summary = summarize_pdf(report, data, decision.pages)
            if summary is not None:
                summary_stage = "caption"
                caption = report_summary_caption(report, decision, summary)
                summary_status = "complete"
        except Exception as exc:
            summary_status = "pending"
            summary_error = (exc.code if isinstance(exc, SummaryError) else
                             "caption_error" if summary_stage == "caption" else "summary_error")
            # A failed optional summary must never stop or permanently reject
            # the PDF; remote errors may contain credentials or source text.
            if isinstance(exc, SummaryError):
                # SummaryError exposes only bounded, allowlisted diagnostics.
                # Never log its message or a provider response body.
                log.warning(
                    "리포트 요약을 건너뜁니다 (SummaryError; code=%s; HTTP=%s; reason=%s). PDF 원문은 계속 전송합니다.",
                    exc.code, exc.status_code, exc.reason,
                )
            else:
                log.warning("리포트 요약을 건너뜁니다 (%s). PDF 원문은 계속 전송합니다.", type(exc).__name__)
            notice = "요약 생성에 실패해 PDF 원문만 첨부합니다."
            if _utf16_length(caption + "\n" + notice) <= 1024:
                caption += "\n" + notice
        with io.BytesIO(data) as document:
            result = self.call("sendDocument", {
                "chat_id": self.chat_id,
                "caption": caption,
                "parse_mode": "HTML",
            }, files={"document": (pdf_filename(report), document, "application/pdf")})
        if not isinstance(result, dict):
            raise TelegramError("PDF 첨부 완료 응답을 확인하지 못했습니다.")
        # These local receipt fields are never sent to Telegram. The caller can
        # persist a pending summary without treating PDF delivery as unfinished.
        return {**result, "_reportbot_summary_status": summary_status,
                "_reportbot_summary_error": summary_error}

    def update_report_summary(self, report: Report, decision: Decision, message_id: int):
        """Add Gemini's summary to the original PDF message, without resending it."""
        if not self.chat_id:
            raise TelegramError("TELEGRAM_CHAT_ID가 필요합니다.")
        if type(message_id) is not int or message_id <= 0:
            raise TelegramError("수정할 PDF 메시지 번호를 확인하세요.")
        if not report.pdf_url:
            raise TelegramError("PDF 원문 주소가 없어 요약을 추가하지 못했습니다.")
        data = download_pdf(report.pdf_url)
        if decision.pdf_hash and hashlib.sha256(data).hexdigest() != decision.pdf_hash:
            raise TelegramError("기존 발송 PDF와 원문 파일이 일치하지 않습니다.")
        summary = summarize_pdf(report, data, decision.pages)
        if summary is None:
            code = "config_mode" if os.getenv("SUMMARY_MODE", "auto").strip().lower() == "off" else "config_key"
            raise SummaryError("요약 서비스가 비활성 상태입니다.", code=code)
        caption = report_summary_caption(report, decision, summary)
        return self.call("editMessageCaption", {
            "chat_id": self.chat_id, "message_id": message_id,
            "caption": caption, "parse_mode": "HTML",
        })

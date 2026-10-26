from datetime import date
import io

import pytest
import requests

from reportbot.classify import Decision
from reportbot.models import Report
from reportbot.telegram import Telegram, TelegramError, download_pdf, pdf_filename, report_caption


TOKEN = "123:fake-document-token"
CHAT_ID = "123456789"
PDF = b"%PDF-1.7\noriginal binary document\n%%EOF"


def report(**changes):
    values = dict(
        source="hankyung", source_id="123", title="반도체 산업 심층 보고서",
        broker="테스트증권", published_date=date(2026, 10, 23),
        detail_url="https://example.com/detail?id=123", pdf_url="https://example.com/source.pdf",
    )
    values.update(changes)
    return Report(**values)


class ApiResponse:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self.ok = status < 400
        self.body = body if body is not None else {"ok": True, "result": {"message_id": 42}}

    def json(self):
        return self.body


class SourceResponse:
    def __init__(self, chunks, headers=None):
        self.chunks = chunks
        self.headers = headers or {}
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        return iter(self.chunks)


class SourceSession:
    def __init__(self, response):
        self.response = response
        self.requests = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return self.response


def test_report_uploads_pdf_bytes_as_document_with_caption(monkeypatch):
    telegram = Telegram(TOKEN, CHAT_ID)
    monkeypatch.setattr("reportbot.telegram.download_pdf", lambda url: PDF)
    calls = []
    file_handles = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        filename, document, mime = kwargs["files"]["document"]
        file_handles.append(document)
        assert document.read() == PDF
        assert filename.endswith(".pdf")
        assert "2026_10_23" in filename
        assert mime == "application/pdf"
        assert kwargs["data"]["chat_id"] == CHAT_ID
        assert "42쪽" in kwargs["data"]["caption"]
        assert "PDF 원문" in kwargs["data"]["caption"]
        assert "섹터: 반도체" in kwargs["data"]["caption"]
        assert "json" not in kwargs
        return ApiResponse()

    monkeypatch.setattr(telegram.session, "post", post)
    assert telegram.send_report(report(source="naver", sector="반도체"), Decision(True, "In-depth", 42)) == {"message_id": 42}
    assert len(calls) == 1
    assert calls[0][0].endswith("/sendDocument")
    assert calls[0][1]["timeout"] == (10, 120)
    assert file_handles[0].closed


def test_download_uses_source_client_streaming_and_closes_resources(monkeypatch):
    response = SourceResponse([PDF[:8], PDF[8:]], {"Content-Length": str(len(PDF))})
    session = SourceSession(response)
    monkeypatch.setattr("reportbot.telegram.make_session", lambda: session)
    assert download_pdf("https://example.com/original") == PDF
    assert session.requests == [("https://example.com/original", {"stream": True, "timeout": (10, 60)})]
    assert response.closed and session.closed


def test_non_pdf_response_is_not_sent_as_link_or_attachment(monkeypatch):
    response = SourceResponse([b"<html>Access blocked</html>"])
    monkeypatch.setattr("reportbot.telegram.make_session", lambda: SourceSession(response))
    telegram = Telegram(TOKEN, CHAT_ID)
    monkeypatch.setattr(telegram.session, "post", lambda *a, **k: pytest.fail("invalid PDF must not be sent"))
    with pytest.raises(TelegramError, match="PDF 원문이 아닌"):
        telegram.send_report(report(), Decision(True, "심층", 40))


@pytest.mark.parametrize("header", [True, False])
def test_download_size_limit_rejects_header_or_stream_overflow(monkeypatch, header):
    monkeypatch.setattr("reportbot.telegram.MAX_PDF_BYTES", 16)
    response = SourceResponse([PDF], {"Content-Length": str(len(PDF))} if header else {})
    monkeypatch.setattr("reportbot.telegram.make_session", lambda: SourceSession(response))
    with pytest.raises(TelegramError, match="25 MiB"):
        download_pdf("https://example.com/oversized.pdf")
    assert response.closed


def test_document_429_retry_rewinds_original_stream(monkeypatch):
    telegram = Telegram(TOKEN, CHAT_ID)
    monkeypatch.setattr("reportbot.telegram.download_pdf", lambda url: PDF)
    uploaded = []
    waits = []

    def post(url, **kwargs):
        uploaded.append(kwargs["files"]["document"][1].read())
        if len(uploaded) == 1:
            return ApiResponse(429, {"ok": False, "parameters": {"retry_after": 2}})
        return ApiResponse()

    monkeypatch.setattr(telegram.session, "post", post)
    monkeypatch.setattr("reportbot.telegram.time.sleep", waits.append)
    telegram.send_report(report(), Decision(True, "심층", 42))
    assert uploaded == [PDF, PDF]
    assert waits == [2]


def test_uncertain_upload_timeout_is_not_retried_and_redacts_credentials(monkeypatch):
    telegram = Telegram(TOKEN, CHAT_ID)
    monkeypatch.setattr("reportbot.telegram.download_pdf", lambda url: PDF)
    attempts = []

    def post(url, **kwargs):
        attempts.append(1)
        raise requests.ReadTimeout(url + "?chat_id=" + CHAT_ID)

    monkeypatch.setattr(telegram.session, "post", post)
    with pytest.raises(TelegramError) as exc:
        telegram.send_report(report(), Decision(True, "심층", 42))
    assert attempts == [1]
    assert TOKEN not in str(exc.value)
    assert CHAT_ID not in str(exc.value)


def test_filename_and_caption_remain_safe_under_long_unicode_metadata():
    item = report(
        title="<반도체 & 배터리>😀/\\" * 300,
        broker='증권사:"<>/&' * 100,
        sector='<반도체 & 배터리>😀' * 100,
        detail_url='https://example.com/report?a=1&b="x"',
    )
    filename = pdf_filename(item)
    caption = report_caption(item, Decision(True, "심층 & 분석 " * 300, 42))
    assert len(filename.encode("utf-8")) <= 180
    assert filename.endswith(".pdf")
    assert not any(char in filename for char in '<>:"/\\|?*')
    assert len(caption.encode("utf-16-le")) // 2 <= 1024
    assert "&lt;반도체 &amp; 배터리&gt;" in caption
    assert "섹터: &lt;반도체 &amp; 배터리&gt;" in caption
    assert 'a=1&amp;b=&quot;x&quot;' in caption
    assert "42쪽" in caption
    assert "PDF 보기" not in caption


def test_caption_omits_sector_when_not_provided():
    caption = report_caption(report(), Decision(True, "심층", 42))
    assert "섹터:" not in caption


def test_naver_caption_displays_miscellaneous_sector_without_guessing():
    caption = report_caption(report(source="naver", sector="기타"), Decision(True, "심층", 42))
    assert "섹터: 기타" in caption
    assert "출처: 네이버 금융" in caption


def test_long_detail_url_is_not_truncated_into_an_invalid_link():
    caption = report_caption(report(detail_url="https://example.com/" + "a" * 3000), Decision(True, "심층", 42))
    assert len(caption.encode("utf-16-le")) // 2 <= 1024
    assert '<a href=' not in caption
    assert "PDF 원문" in caption


def test_missing_pdf_address_fails_without_link_only_fallback(monkeypatch):
    telegram = Telegram(TOKEN, CHAT_ID)
    monkeypatch.setattr(telegram.session, "post", lambda *a, **k: pytest.fail("missing PDF must remain pending"))
    with pytest.raises(TelegramError, match="PDF 원문 주소"):
        telegram.send_report(report(pdf_url=None), Decision(True, "명시된 심층"))


def test_api_error_fields_cannot_echo_token_or_chat_id(monkeypatch):
    telegram = Telegram(TOKEN, CHAT_ID)
    monkeypatch.setattr(telegram.session, "post", lambda *a, **k: ApiResponse(403, {
        "ok": False, "error_code": TOKEN, "description": CHAT_ID,
    }))
    with pytest.raises(TelegramError) as exc:
        telegram.send("test")
    assert TOKEN not in str(exc.value)
    assert CHAT_ID not in str(exc.value)

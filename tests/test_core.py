from contextlib import nullcontext
from dataclasses import replace
from datetime import date
import io

import pytest
import requests
from pypdf import PdfWriter

from reportbot.classify import Decision, classify, inspect_pdf, title_rejection
from reportbot.config import Config
from reportbot.models import Report
from reportbot.state import State
from reportbot.telegram import Telegram, TelegramError


def report(**changes):
    item = Report(
        source="hankyung", source_id="new-1", title="반도체 산업의 구조적 전환",
        broker="미래증권", published_date=date.today(),
        detail_url="https://example.com/reports/1", pdf_url="https://example.com/r.pdf",
    )
    return replace(item, **changes)


@pytest.fixture
def config(tmp_path):
    return Config(tmp_path / "state.sqlite3", "123:test-secret", "123456", 20, 3, 1800, 14)


@pytest.fixture
def runner(monkeypatch):
    from reportbot import runner

    monkeypatch.setattr(runner, "make_session", lambda: nullcontext(object()))
    return runner


def test_first_run_sends_known_unsent_reports_and_later_only_new_reports(runner, monkeypatch, config):
    old = report(source_id="old")
    new = report(title="에너지 산업의 새로운 투자 사이클")
    listing = [old]
    monkeypatch.setattr(runner, "SOURCES", {"hankyung": lambda *a, **k: list(listing)})
    sent = []
    monkeypatch.setattr(Telegram, "send_report", lambda self, r, d: sent.append(r))
    classifications = []

    def accept(r, session, minimum):
        classifications.append(r)
        return Decision(True, "산업 리포트 25쪽", 25, "pdf-hash-" + r.source_id)

    monkeypatch.setattr(runner, "classify", accept)
    assert runner.check(config) == 0
    assert sent == [old]
    assert classifications == [old]
    listing.append(new)
    assert runner.check(config) == 0
    assert sent == [old, new]
    assert runner.check(config) == 0
    assert sent == [old, new]


def test_source_failure_does_not_initialize_and_other_source_still_processes(runner, monkeypatch, config):
    def unavailable(*args, **kwargs):
        raise requests.ConnectionError("temporary failure")

    naver = report(source="naver")
    monkeypatch.setattr(runner, "SOURCES", {"hankyung": unavailable, "naver": lambda *a, **k: [naver]})
    monkeypatch.setattr(runner, "classify", lambda *a: Decision(True, "심층 표시", 25, "naver-pdf"))
    sent = []
    monkeypatch.setattr(Telegram, "send_report", lambda self, r, d: sent.append(r))
    assert runner.check(config) == 1
    assert sent == [naver]
    state = State(config.state_path)
    try:
        assert not state.initialized("hankyung")
        assert state.initialized("naver")
        assert state.seen(naver)
    finally:
        state.close()


def test_cross_source_title_and_pdf_duplicates(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    first = report(title="[In-depth] 반도체, 산업의 전환")
    identical_title = report(source="naver", source_id="2", title="반도체 산업의 전환", broker="미래")
    different_title = report(source="naver", source_id="3", title="다른 제목으로 게시된 자료")
    try:
        state.record(first, "sent", "same-pdf")
        assert state.duplicate(identical_title)
        assert not state.duplicate(different_title)
        assert state.duplicate(different_title, "same-pdf")
    finally:
        state.close()


def test_pdf_failure_retries_unprocessed_report(runner, monkeypatch, config):
    item = report()
    state = State(config.state_path)
    state.baseline("hankyung", [])
    state.close()
    monkeypatch.setattr(runner, "SOURCES", {"hankyung": lambda *a, **k: [item]})
    attempts = []

    def broken(*args):
        attempts.append(1)
        raise requests.Timeout("download temporarily unavailable")

    monkeypatch.setattr(runner, "classify", broken)
    notices = []
    monkeypatch.setattr(Telegram, "send_report_error", lambda self, r, reason: notices.append(r))
    assert runner.check(config) == 0
    assert notices == [item]
    state = State(config.state_path)
    try:
        assert state.outcome(item).startswith("pending")
        assert item in state.pending()
        assert not state.duplicate(item)
    finally:
        state.close()
    sent = []
    monkeypatch.setattr(runner, "classify", lambda *a: Decision(True, "심층 표시", 10, "hash"))
    monkeypatch.setattr(Telegram, "send_report", lambda self, r, d: sent.append(r))
    assert runner.check(config) == 0
    assert sent == [item]
    assert attempts == [1]


def test_send_failure_keeps_report_pending(runner, monkeypatch, config):
    item = report()
    state = State(config.state_path)
    state.baseline("hankyung", [])
    state.close()
    monkeypatch.setattr(runner, "SOURCES", {"hankyung": lambda *a, **k: [item]})
    monkeypatch.setattr(runner, "classify", lambda *a: Decision(True, "심층 표시", 10, "hash"))

    def failure(*args):
        raise TelegramError("temporary failure")

    monkeypatch.setattr(Telegram, "send_report", failure)
    notices = []
    monkeypatch.setattr(Telegram, "send_report_error", lambda self, r, reason: notices.append(r))
    assert runner.check(config) == 0
    assert notices == [item]
    state = State(config.state_path)
    try:
        assert state.outcome(item).startswith("pending")
        assert item in state.pending()
        assert not state.duplicate(item, "hash")
    finally:
        state.close()


def test_dry_run_requires_no_credentials_and_does_not_create_state(runner, monkeypatch, config):
    monkeypatch.setattr(runner, "SOURCES", {"hankyung": lambda *a, **k: [report()]})
    monkeypatch.setattr(runner, "classify", lambda *a: Decision(True, "심층 표시"))
    assert runner.check(replace(config, token="", chat_id=""), dry_run=True) == 0
    assert not config.state_path.exists()


def test_naver_new_item_resolves_attachment_only_when_needed(runner, monkeypatch, config):
    item = report(source="naver", source_id="46614", pdf_url=None)
    monkeypatch.setattr(runner, "SOURCES", {"naver": lambda *a, **k: [item]})
    resolved = []

    def resolve(session, r):
        resolved.append(r)
        return "https://example.com/industry.pdf"

    monkeypatch.setattr(runner.naver, "resolve_pdf_url", resolve)
    monkeypatch.setattr(runner, "classify", lambda r, *a: Decision(r.pdf_url is not None, "PDF 25쪽", 25, "pdf-" + r.source_id))
    sent = []
    monkeypatch.setattr(Telegram, "send_report", lambda self, r, d: sent.append(r))
    assert runner.check(config) == 0
    assert resolved == [item]
    newer = replace(item, source_id="46615", title="신규 산업 리포트")
    monkeypatch.setattr(runner, "SOURCES", {"naver": lambda *a, **k: [item, newer]})
    assert runner.check(config) == 0
    assert resolved == [item, newer]
    assert len(sent) == 2
    assert sent[0].pdf_url == "https://example.com/industry.pdf"


def test_long_pdf_with_weekly_cover_is_rejected(monkeypatch):
    monkeypatch.setattr("reportbot.classify.inspect_pdf", lambda *a: (40, "주간 리뷰\n유틸리티 업황", "hash"))
    assert not classify(report(), object()).accepted


@pytest.mark.parametrize("title", ["반도체 Weekly", "자동차 주간 동향", "화학 월간 리포트", "은행 실적 프리뷰", "삼성전자(005930) 분석"])
def test_periodic_and_single_company_titles_rejected(title):
    assert title_rejection(report(title=title))


@pytest.mark.parametrize("pages,front,expected", [(19, "산업 분석", False), (20, "산업 분석", True), (7, "Industry In-depth", True)])
def test_depth_label_and_page_threshold(monkeypatch, pages, front, expected):
    monkeypatch.setattr("reportbot.classify.inspect_pdf", lambda *a: (pages, front, "hash"))
    result = classify(report(), object(), 20)
    assert result.accepted is expected
    assert result.pages == pages
    assert result.pdf_hash == "hash"


def test_explicit_depth_without_pdf_is_accepted():
    assert classify(report(title="[In-depth] 반도체 산업", pdf_url=None), object()).accepted
    assert not classify(report(pdf_url=None), object()).accepted


def test_periodic_title_never_downloads_pdf(monkeypatch):
    def forbidden(*args):
        pytest.fail("periodic report should be rejected before PDF download")

    monkeypatch.setattr("reportbot.classify.inspect_pdf", forbidden)
    assert not classify(report(title="In-depth Weekly 반도체"), object()).accepted


class Response:
    def __init__(self, status, body):
        self.status_code = status
        self.ok = status < 400
        self.body = body

    def json(self):
        return self.body


def test_telegram_429_uses_retry_after(monkeypatch):
    telegram = Telegram("123:test-secret", "1234")
    replies = iter([
        Response(429, {"ok": False, "parameters": {"retry_after": 2}}),
        Response(200, {"ok": True, "result": {"message_id": 1}}),
    ])
    sleeps = []
    monkeypatch.setattr(telegram.session, "post", lambda *a, **k: next(replies))
    monkeypatch.setattr("reportbot.telegram.time.sleep", sleeps.append)
    assert telegram.send("test") == {"message_id": 1}
    assert sleeps == [2]


def test_telegram_timeout_does_not_retry_or_expose_token(monkeypatch):
    telegram = Telegram("123:test-secret", "1234")
    attempts = []

    def timeout(*args, **kwargs):
        attempts.append(1)
        raise requests.Timeout("https://api.telegram.org/bot123:test-secret/sendMessage")

    monkeypatch.setattr(telegram.session, "post", timeout)
    with pytest.raises(TelegramError) as error:
        telegram.send("test")
    assert attempts == [1]
    assert "test-secret" not in str(error.value)


def test_report_message_escapes_html_and_links(monkeypatch):
    from reportbot.telegram import report_caption

    caption = report_caption(report(title="<반도체 & 배터리>", detail_url='https://example.com/?a=1&b="x"'), Decision(True, "20쪽"))
    assert "&lt;반도체 &amp; 배터리&gt;" in caption
    assert 'a=1&amp;b=&quot;x&quot;' in caption


def test_inspect_pdf_checks_real_page_count_and_hash():
    writer = PdfWriter()
    for _ in range(20):
        writer.add_blank_page(width=100, height=100)
    output = io.BytesIO()
    writer.write(output)
    data = output.getvalue()

    class PdfResponse:
        headers = {"Content-Length": str(len(data))}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            yield data

    class Session:
        def get(self, *args, **kwargs):
            return PdfResponse()

    pages, text, digest = inspect_pdf(Session(), "https://example.com/r.pdf")
    assert pages == 20
    assert not text.strip()
    assert len(digest) == 64

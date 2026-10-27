from dataclasses import replace
from datetime import date

import pytest

from reportbot.classify import Decision
from reportbot.models import Report
from reportbot.telegram import Telegram, TelegramError, report_caption, report_summary_caption


PDF = b"%PDF-1.7\noriginal report bytes\n%%EOF"
TOKEN = "123:fake-summary-token"
CHAT_ID = "-100424242"
DECISION = Decision(True, "산업 리포트 30쪽", 30)


def report(**changes):
    return replace(Report(
        "naver", "100", "<반도체 & 산업>", "테스트증권", date(2026, 10, 26),
        "https://example.com/report", "https://example.com/report.pdf", sector="반도체",
    ), **changes)


@pytest.fixture
def delivery(monkeypatch):
    telegram = Telegram(TOKEN, CHAT_ID)
    calls, waits, downloads = [], [], []

    def download(url):
        downloads.append(url)
        return PDF

    def call(method, payload, **kwargs):
        if method == "sendDocument":
            assert kwargs["files"]["document"][1].read() == PDF
        calls.append((method, payload))
        return {"message_id": len(calls)}

    monkeypatch.setattr("reportbot.telegram.download_pdf", download)
    monkeypatch.setattr("reportbot.telegram.summarize_pdf", lambda *args: None)
    monkeypatch.setattr("reportbot.telegram.time.sleep", waits.append)
    monkeypatch.setattr(telegram, "call", call)
    return telegram, calls, waits, downloads


def test_summary_and_same_pdf_are_one_document_message(delivery, monkeypatch):
    telegram, calls, waits, downloads = delivery
    item = report()
    summary = [f"산업 전망 & 근거 {index}" for index in range(1, 11)]
    received = []

    def summarize(source, data, pages):
        received.append((source, data, pages))
        return summary

    monkeypatch.setattr("reportbot.telegram.summarize_pdf", summarize)
    assert telegram.send_report(item, DECISION) == {"message_id": 1}
    assert received == [(item, PDF, 30)]
    assert downloads == [item.pdf_url]
    assert [method for method, payload in calls] == ["sendDocument"]
    caption = calls[0][1]["caption"]
    assert "10. 산업 전망 &amp; 근거 10" in caption
    assert "&lt;반도체 &amp; 산업&gt;" in caption
    assert "섹터: 반도체" in caption
    assert "네이버 금융" in caption and "30쪽" in caption
    assert "테스트증권 · 2026-10-26" in caption
    assert all(f"\n{index}. " in caption for index in range(1, 11))
    assert "(p." not in caption
    assert waits == []
    assert len(caption.encode("utf-16-le")) // 2 <= 1024


def test_disabled_summary_preserves_existing_pdf_delivery(delivery):
    telegram, calls, waits, downloads = delivery
    item = report()
    assert telegram.send_report(item, DECISION) == {"message_id": 1}
    assert [method for method, payload in calls] == ["sendDocument"]
    assert calls[0][1]["caption"] == report_caption(item, DECISION)
    assert waits == []


def test_generation_failure_does_not_drop_pdf_or_expose_exception(delivery, monkeypatch, caplog):
    telegram, calls, waits, downloads = delivery

    def fail(*args):
        raise RuntimeError(f"{TOKEN} {CHAT_ID} full-sensitive-PDF-text")

    monkeypatch.setattr("reportbot.telegram.summarize_pdf", fail)
    assert telegram.send_report(report(), DECISION) == {"message_id": 1}
    assert [method for method, payload in calls] == ["sendDocument"]
    assert "요약 생성에 실패해 PDF 원문만 첨부합니다." in calls[0][1]["caption"]
    assert "RuntimeError" in caplog.text
    for sensitive in [TOKEN, CHAT_ID, "full-sensitive-PDF-text"]:
        assert sensitive not in caplog.text


def test_summary_does_not_use_a_separate_text_message(delivery, monkeypatch):
    telegram, calls, waits, downloads = delivery
    monkeypatch.setattr("reportbot.telegram.summarize_pdf", lambda *args: [f"산업 전망 {index}" for index in range(1, 11)])

    def fail_send(text):
        pytest.fail("A report summary belongs in the document caption")

    monkeypatch.setattr(telegram, "send", fail_send)
    assert telegram.send_report(report(), DECISION) == {"message_id": 1}
    assert [method for method, payload in calls] == ["sendDocument"]
    assert "10. 산업 전망 10" in calls[0][1]["caption"]
    assert waits == []


@pytest.mark.parametrize("summary", [
    [], ["전망"] * 9, ["전망"] * 11, ["전망"] * 9 + [" "],
    ["전망"] * 9 + [42], ["전망"] * 9 + ["두\n줄"],
    [f"산업 전망 {i}" for i in range(9)] + ["긴" * 21],
    [f"산업 전망 {i}" for i in range(9)] + ["넓" * 19],
    ["같은 요약"] * 10,
])
def test_invalid_summary_is_skipped_but_pdf_is_delivered(delivery, monkeypatch, summary):
    telegram, calls, waits, downloads = delivery
    monkeypatch.setattr("reportbot.telegram.summarize_pdf", lambda *args: summary)
    telegram.send_report(report(), DECISION)
    assert [method for method, payload in calls] == ["sendDocument"]
    assert "요약 생성에 실패해 PDF 원문만 첨부합니다." in calls[0][1]["caption"]


def test_long_unicode_metadata_is_bounded_without_clipping_summary_lines():
    item = report(title="<긴 제목 & 산업>😀" * 300, broker='증권사 "A"' * 100,
                  sector="<반도체>😀" * 100, detail_url="https://example.com/" + "x" * 3000)
    summary = [f"<수요 & 공급>😀 {i}" for i in range(1, 11)]
    text = report_summary_caption(item, DECISION, summary)
    assert len(text.encode("utf-16-le")) // 2 <= 1024
    assert "&lt;수요 &amp; 공급&gt;😀" in text
    assert all(f"\n{index}. &lt;수요 &amp; 공급&gt;😀 {index}" in text for index in range(1, 11))
    assert "원문 페이지" not in text


def test_caption_overflow_is_rejected_instead_of_truncating_summary():
    summary = ["가" + "&" * 18 + chr(64 + i) for i in range(1, 11)]
    with pytest.raises(TelegramError, match="PDF 설명 길이 제한"):
        report_summary_caption(report(), DECISION, summary)


def test_caption_formatting_failure_still_sends_pdf_and_redacts_error(delivery, monkeypatch, caplog):
    telegram, calls, waits, downloads = delivery
    monkeypatch.setattr("reportbot.telegram.summarize_pdf", lambda *args: [f"산업 전망 {i}" for i in range(1, 11)])

    def fail(*args):
        raise RuntimeError(f"{TOKEN} private-formatting-error")

    monkeypatch.setattr("reportbot.telegram.report_summary_caption", fail)
    telegram.send_report(report(), DECISION)
    assert [method for method, payload in calls] == ["sendDocument"]
    assert "요약 생성에 실패해 PDF 원문만 첨부합니다." in calls[0][1]["caption"]
    assert TOKEN not in caplog.text and "private-formatting-error" not in caplog.text


def test_summary_failure_notice_is_omitted_when_caption_is_full(delivery, monkeypatch):
    telegram, calls, waits, downloads = delivery
    monkeypatch.setattr("reportbot.telegram.report_caption", lambda *args: "x" * 1024)
    monkeypatch.setattr("reportbot.telegram.summarize_pdf", lambda *args: [])
    telegram.send_report(report(), DECISION)
    assert calls[0][1]["caption"] == "x" * 1024

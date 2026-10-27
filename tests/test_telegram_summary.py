from dataclasses import replace
from datetime import date

import pytest

from reportbot.classify import Decision
from reportbot.models import Report
from reportbot.telegram import Telegram, TelegramError, report_caption, report_summary_message


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


def test_summary_uses_same_pdf_bytes_and_precedes_original_pdf(delivery, monkeypatch):
    telegram, calls, waits, downloads = delivery
    item = report()
    summary = [f"산업 전망 & 근거 {index} (p.{index})" for index in range(1, 11)]
    received = []

    def summarize(source, data, pages):
        received.append((source, data, pages))
        return summary

    monkeypatch.setattr("reportbot.telegram.summarize_pdf", summarize)
    assert telegram.send_report(item, DECISION) == {"message_id": 2}
    assert received == [(item, PDF, 30)]
    assert downloads == [item.pdf_url]
    assert [method for method, payload in calls] == ["sendMessage", "sendDocument"]
    assert "10. 산업 전망 &amp; 근거 10 (p.10)" in calls[0][1]["text"]
    assert "&lt;반도체 &amp; 산업&gt;" in calls[0][1]["text"]
    assert "섹터: 반도체" in calls[0][1]["text"]
    assert waits == [1.1]
    assert calls[1][1]["caption"] == report_caption(item, DECISION)


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


def test_summary_message_send_failure_still_uploads_pdf(delivery, monkeypatch, caplog):
    telegram, calls, waits, downloads = delivery
    monkeypatch.setattr("reportbot.telegram.summarize_pdf", lambda *args: ["전망 (p.1)"] * 10)

    def fail_send(text):
        raise TelegramError(f"{TOKEN} summary-send-private-error")

    monkeypatch.setattr(telegram, "send", fail_send)
    assert telegram.send_report(report(), DECISION) == {"message_id": 1}
    assert [method for method, payload in calls] == ["sendDocument"]
    assert "요약 생성에 실패해 PDF 원문만 첨부합니다." in calls[0][1]["caption"]
    assert "summary-send-private-error" not in caplog.text
    assert TOKEN not in caplog.text
    assert waits == []


@pytest.mark.parametrize("summary", [[], ["전망"] * 9, ["전망"] * 11, ["전망"] * 9 + [" "], ["전망"] * 9 + [42], ["전망"] * 9 + ["두\n줄"]])
def test_invalid_summary_is_skipped_but_pdf_is_delivered(delivery, monkeypatch, summary):
    telegram, calls, waits, downloads = delivery
    monkeypatch.setattr("reportbot.telegram.summarize_pdf", lambda *args: summary)
    telegram.send_report(report(), DECISION)
    assert [method for method, payload in calls] == ["sendDocument"]
    assert "요약 생성에 실패해 PDF 원문만 첨부합니다." in calls[0][1]["caption"]


def test_long_unicode_summary_is_safe_html_and_stays_under_message_limit():
    item = report(title="<긴 제목 & 산업>😀" * 300, broker='증권사 "A"' * 100, sector="<반도체>😀" * 100)
    text = report_summary_message(item, ["<수요 & 공급>😀" * 100 for _ in range(10)])
    assert len(text.encode("utf-16-le")) // 2 <= 4096
    assert "&lt;수요 &amp; 공급&gt;" in text
    assert all(f"\n{index}. " in text for index in range(1, 11))


def test_summary_failure_notice_is_omitted_when_caption_is_full(delivery, monkeypatch):
    telegram, calls, waits, downloads = delivery
    monkeypatch.setattr("reportbot.telegram.report_caption", lambda *args: "x" * 1024)
    monkeypatch.setattr("reportbot.telegram.summarize_pdf", lambda *args: [])
    telegram.send_report(report(), DECISION)
    assert calls[0][1]["caption"] == "x" * 1024

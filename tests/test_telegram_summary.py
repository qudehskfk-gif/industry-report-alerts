from dataclasses import replace
from datetime import date

import pytest

from reportbot.classify import Decision
from reportbot.models import Report
from reportbot.summarize import SummaryError
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


def section_summary(count=5, points=3):
    return [
        {"heading": f"산업 핵심 주제 {index}",
         "points": [f"수요 변화의 원인과 의미 {index}-{point}" for point in range(1, points + 1)]}
        for index in range(1, count + 1)
    ]


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


@pytest.mark.parametrize("section_count,point_count", [(5, 3), (3, 5)])
def test_sectioned_summary_and_same_pdf_are_one_document_message(delivery, monkeypatch, section_count, point_count):
    telegram, calls, waits, downloads = delivery
    item = report()
    summary = section_summary(section_count, point_count)
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
    assert "&lt;반도체 &amp; 산업&gt;" in caption
    assert "섹터: 반도체" in caption
    assert "네이버 금융" in caption and "30쪽" in caption
    assert "테스트증권 · 2026-10-26" in caption
    for index in range(1, section_count + 1):
        assert f"<b>{index}. 산업 핵심 주제 {index}</b>" in caption
        for point in range(1, point_count + 1):
            assert f"\n• 수요 변화의 원인과 의미 {index}-{point}" in caption
    assert caption.count("\n• ") == section_count * point_count
    assert "10줄" not in caption and "(p." not in caption
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


@pytest.mark.parametrize("code,status,reason", [
    ("quota_429", 429, None),
    ("http_503", 503, None),
    ("validation_budget", None, None),
    ("transport_error", None, "ReadTimeout"),
    ("http_400", 400, "API_KEY_INVALID"),
])
def test_summary_failure_logs_safe_diagnostics_and_keeps_pdf(delivery, monkeypatch, caplog, code, status, reason):
    telegram, calls, waits, downloads = delivery

    def fail(*args):
        raise SummaryError(
            f"{TOKEN} {CHAT_ID} full-sensitive-PDF-text",
            code=code, status_code=status, reason=reason,
        )

    monkeypatch.setattr("reportbot.telegram.summarize_pdf", fail)
    telegram.send_report(report(), DECISION)
    assert [method for method, payload in calls] == ["sendDocument"]
    assert f"code={code}; HTTP={status}; reason={reason}" in caplog.text
    for sensitive in [TOKEN, CHAT_ID, "full-sensitive-PDF-text"]:
        assert sensitive not in caplog.text


def test_summary_does_not_use_a_separate_text_message(delivery, monkeypatch):
    telegram, calls, waits, downloads = delivery
    monkeypatch.setattr("reportbot.telegram.summarize_pdf", lambda *args: section_summary())
    monkeypatch.setattr(telegram, "send", lambda text: pytest.fail("Summary must stay in the PDF caption"))
    telegram.send_report(report(), DECISION)
    assert [method for method, payload in calls] == ["sendDocument"]
    assert calls[0][1]["caption"].count("\n• ") == 15
    assert waits == []


@pytest.mark.parametrize("summary", [
    None, [], ["짧은 줄"] * 10, section_summary(4, 3), section_summary(3, 2),
    section_summary(5, 6), [{"heading": "주제", "points": ["긴" * 43] * 3}] * 3,
    [{"heading": "주제", "points": ["두\n줄", "다른 설명", "또 다른 설명"]}] * 3,
])
def test_invalid_summary_is_skipped_but_pdf_is_delivered(delivery, monkeypatch, summary):
    if summary is None:
        summary = {"sections": section_summary()}
    telegram, calls, waits, downloads = delivery
    monkeypatch.setattr("reportbot.telegram.summarize_pdf", lambda *args: summary)
    telegram.send_report(report(), DECISION)
    assert [method for method, payload in calls] == ["sendDocument"]
    assert "요약 생성에 실패해 PDF 원문만 첨부합니다." in calls[0][1]["caption"]


def test_long_metadata_is_reduced_before_complete_summary_and_html_is_escaped():
    item = report(title="<긴 제목 & 산업>😀" * 300, broker='증권사 "A"' * 100,
                  sector="<반도체>😀" * 100, detail_url="https://example.com/" + "x" * 3000)
    summary = section_summary()
    for index, section in enumerate(summary, 1):
        section["heading"] = f"<수요 & 공급> {index}"
        section["points"] = [f"<기술 & 수익> 개선의 의미 {index}-{p}" for p in range(1, 4)]
    text = report_summary_caption(item, DECISION, summary)
    assert len(text.encode("utf-16-le")) // 2 <= 1024
    assert "&lt;수요 &amp; 공급&gt;" in text
    assert "&lt;기술 &amp; 수익&gt;" in text
    assert text.count("\n• ") == 15
    for index in range(1, 6):
        for point in range(1, 4):
            assert f"• &lt;기술 &amp; 수익&gt; 개선의 의미 {index}-{point}" in text
    assert "원문 페이지" not in text


def test_caption_overflow_rejects_summary_instead_of_truncating_content():
    summary = section_summary()
    for index, section in enumerate(summary, 1):
        section["points"] = ["가" + "&" * 24 + f"{index}-{p}" for p in range(1, 4)]
    with pytest.raises(TelegramError, match="PDF 설명 길이 제한"):
        report_summary_caption(report(), DECISION, summary)


def test_caption_formatting_failure_still_sends_pdf_and_redacts_error(delivery, monkeypatch, caplog):
    telegram, calls, waits, downloads = delivery
    monkeypatch.setattr("reportbot.telegram.summarize_pdf", lambda *args: section_summary())

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

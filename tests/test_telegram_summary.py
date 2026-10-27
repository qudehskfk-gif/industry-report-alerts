from dataclasses import replace
from datetime import date
import hashlib
import html
import re
from types import SimpleNamespace

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


def visible_units(caption):
    text = html.unescape(re.sub(r"<[^>]*>", "", caption))
    return len(text.encode("utf-16-le")) // 2


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
    result = telegram.send_report(item, DECISION)
    assert result == {"message_id": 1, "_reportbot_summary_status": "complete", "_reportbot_summary_error": None}
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
    assert visible_units(caption) <= 1024


def test_disabled_summary_preserves_existing_pdf_delivery(delivery):
    telegram, calls, waits, downloads = delivery
    item = report()
    assert telegram.send_report(item, DECISION) == {
        "message_id": 1, "_reportbot_summary_status": "disabled", "_reportbot_summary_error": None,
    }
    assert [method for method, payload in calls] == ["sendDocument"]
    assert calls[0][1]["caption"] == report_caption(item, DECISION)
    assert waits == []


def test_generation_failure_does_not_drop_pdf_or_expose_exception(delivery, monkeypatch, caplog):
    telegram, calls, waits, downloads = delivery

    def fail(*args):
        raise RuntimeError(f"{TOKEN} {CHAT_ID} full-sensitive-PDF-text")

    monkeypatch.setattr("reportbot.telegram.summarize_pdf", fail)
    assert telegram.send_report(report(), DECISION) == {
        "message_id": 1, "_reportbot_summary_status": "pending", "_reportbot_summary_error": "summary_error",
    }
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
    result = telegram.send_report(report(), DECISION)
    assert result["_reportbot_summary_status"] == "pending"
    assert result["_reportbot_summary_error"] == code
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
    assert visible_units(text) <= 1024
    assert "&lt;수요 &amp; 공급&gt;" in text
    assert "&lt;기술 &amp; 수익&gt;" in text
    assert text.count("\n• ") == 15
    for index in range(1, 6):
        for point in range(1, 4):
            assert f"• &lt;기술 &amp; 수익&gt; 개선의 의미 {index}-{point}" in text
    assert "원문 페이지" in text
    assert item.detail_url in text


def test_html_entity_expansion_does_not_discard_a_summary_that_fits():
    summary = section_summary()
    for index, section in enumerate(summary, 1):
        section["points"] = ["가" + "&" * 24 + f"{index}-{p}" for p in range(1, 4)]
    caption = report_summary_caption(report(), DECISION, summary)
    assert len(caption.encode("utf-16-le")) // 2 > 1024
    assert visible_units(caption) <= 1024
    for index in range(1, 6):
        for point in range(1, 4):
            assert f"가{'&amp;' * 24}{index}-{point}" in caption


@pytest.mark.parametrize("units", [1024, 1025])
def test_caption_limit_counts_visible_utf16_and_preserves_whole_text(monkeypatch, units):
    # Exercise the formatter's final Telegram guard, including supplementary
    # Unicode characters which consume two UTF-16 units.
    summary = section_summary(3, 3)
    visible_body = "\n\n".join(
        "\n".join([f"{index}. {section['heading']}", *[f"• {point}" for point in section['points']]])
        for index, section in enumerate(summary, 1)
    )
    used = len(visible_body.encode("utf-16-le")) // 2
    padding = "가" * (units - used - 2) + "😀"
    summary[-1]["points"][-1] += padding
    monkeypatch.setattr("reportbot.telegram.validate_summary_sections", lambda sections: sections)
    if units == 1025:
        with pytest.raises(TelegramError, match="PDF 설명 길이 제한"):
            report_summary_caption(report(), DECISION, summary)
    else:
        caption = report_summary_caption(report(), DECISION, summary)
        assert visible_units(caption) == 1024
        assert caption.endswith(padding)
        assert caption.count("\n• ") == 9
        assert "원문 페이지" not in caption
        assert "테스트증권" not in caption


def test_large_valid_summary_keeps_all_points_and_reduces_metadata_first():
    summary = section_summary(5, 3)
    for index, section in enumerate(summary, 1):
        section["heading"] = f"반도체 산업 구조 변화의 원인과 전망을 설명하는 주제 {index}"
        section["points"] = [
            f"AI 수요 확대가 고부가 제품 공급과 기업 수익성에 미치는 영향을 분석 {index}-{point}"
            for point in range(1, 4)
        ]
    item = report(title="매우 긴 산업 전망 보고서 제목 " * 30, broker="매우 긴 증권회사 이름 " * 20,
                  sector="매우 긴 산업 섹터 이름 " * 20)
    caption = report_summary_caption(item, DECISION, summary)
    assert visible_units(caption) <= 1024
    assert caption.count("\n• ") == 15
    for section in summary:
        assert section["heading"] in caption
        for point in section["points"]:
            assert point in caption


def test_927_unit_summary_keeps_full_content_and_compact_identification(delivery, monkeypatch):
    from reportbot.summarize import summary_body_text

    telegram, calls, waits, downloads = delivery
    summary = section_summary(5, 3)
    for index, section in enumerate(summary, 1):
        section["heading"] = f"반도체 산업 구조 변화의 원인과 전망을 설명하는 주제 {index}"
        section["points"] = [
            f"AI 수요 확대가 고부가 제품 공급과 기업 수익성에 미치는 영향을 분석 {index}-{point}"
            for point in range(1, 4)
        ]
    # The earlier 900-unit provider limit discarded a real 927-unit draft.
    # Preserve a complete final explanation while reproducing that size.
    summary[-1]["points"][-1] += " 또한 고객 주문과 신규 증설이 수익성 개선으로 이어지는 시점까지 함께 살펴보아야 한다는 점이 핵심입니다."
    assert len(summary_body_text(summary).encode("utf-16-le")) // 2 == 927
    item = report(title="2027년 산업 전망과 투자 기회를 종합적으로 분석하는 상세 보고서 " * 4,
                  broker="산업분석전문리서치증권회사", sector="반도체패키지기판공급망산업전반")
    monkeypatch.setattr("reportbot.telegram.summarize_pdf", lambda *args: summary)
    telegram.send_report(item, DECISION)
    assert [method for method, payload in calls] == ["sendDocument"]
    caption = calls[0][1]["caption"]
    assert visible_units(caption) <= 1024
    assert item.broker in caption and item.published_date.isoformat() in caption
    assert item.sector in caption and "30쪽" in caption
    assert "요약 생성에 실패" not in caption
    for section in summary:
        assert section["heading"] in caption
        for point in section["points"]:
            assert point in caption


@pytest.mark.parametrize("count,points", [(5, 3), (3, 5)])
def test_provider_summary_budget_counts_the_exact_visible_caption_body(count, points):
    from reportbot.summarize import summary_body_text

    summary = section_summary(count, points)
    summary[0]["heading"] = "AI & 수요 <전망> 😀"
    summary[0]["points"][0] = "공급 < 수요이며 가격 & 이익의 변화도 함께 분석 😀"
    caption = report_summary_caption(report(), DECISION, summary)
    body_html = caption[caption.index("<b>1. "):].split('\n<a href="', 1)[0]
    visible_body = html.unescape(re.sub(r"<[^>]*>", "", body_html))
    assert visible_body == summary_body_text(summary)
    assert visible_units(body_html) == len(summary_body_text(summary).encode("utf-16-le")) // 2


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


def test_pending_summary_receipt_is_local_and_pdf_payload_contains_no_diagnostics(delivery, monkeypatch):
    telegram, calls, waits, downloads = delivery

    def fail(*args):
        raise SummaryError("private response", code="http_503", status_code=503)

    monkeypatch.setattr("reportbot.telegram.summarize_pdf", fail)
    result = telegram.send_report(report(), DECISION)
    assert result["message_id"] == 1
    assert result["_reportbot_summary_status"] == "pending"
    assert result["_reportbot_summary_error"] == "http_503"
    assert all(not key.startswith("_reportbot_") for key in calls[0][1])
    assert "private response" not in str(result)


def test_recovery_edits_original_pdf_caption_without_document_or_text_message(delivery, monkeypatch):
    telegram, calls, waits, downloads = delivery
    summary = section_summary()
    monkeypatch.setattr("reportbot.telegram.summarize_pdf", lambda *args: summary)
    decision = replace(DECISION, pdf_hash=hashlib.sha256(PDF).hexdigest())
    assert telegram.update_report_summary(report(), decision, 75) == {"message_id": 1}
    assert downloads == [report().pdf_url]
    assert [method for method, payload in calls] == ["editMessageCaption"]
    payload = calls[0][1]
    assert payload["chat_id"] == CHAT_ID and payload["message_id"] == 75
    assert payload["parse_mode"] == "HTML"
    assert payload["caption"].count("\n• ") == 15
    assert visible_units(payload["caption"]) <= 1024
    assert "요약 생성에 실패" not in payload["caption"]


@pytest.mark.parametrize("message_id", [None, 0, -1, True, "75"])
def test_recovery_rejects_bad_receipt_before_any_download_or_api_call(delivery, monkeypatch, message_id):
    telegram, calls, waits, downloads = delivery
    monkeypatch.setattr("reportbot.telegram.summarize_pdf", lambda *args: pytest.fail("No summary request"))
    with pytest.raises(TelegramError, match="메시지 번호"):
        telegram.update_report_summary(report(), DECISION, message_id)
    assert downloads == [] and calls == []


def test_recovery_hash_mismatch_does_not_generate_or_edit(delivery, monkeypatch):
    telegram, calls, waits, downloads = delivery
    monkeypatch.setattr("reportbot.telegram.summarize_pdf", lambda *args: pytest.fail("No summary for changed PDF"))
    with pytest.raises(TelegramError, match="일치하지"):
        telegram.update_report_summary(report(), replace(DECISION, pdf_hash="changed-original"), 75)
    assert downloads == [report().pdf_url] and calls == []


@pytest.mark.parametrize("mode,expected", [("off", "config_mode"), ("auto", "config_key")])
def test_recovery_keeps_original_message_when_summary_is_disabled(delivery, monkeypatch, mode, expected):
    telegram, calls, waits, downloads = delivery
    monkeypatch.setenv("SUMMARY_MODE", mode)
    with pytest.raises(SummaryError) as error:
        telegram.update_report_summary(report(), DECISION, 75)
    assert error.value.code == expected
    assert calls == []


def test_recovery_generation_error_does_not_upload_duplicate_pdf(delivery, monkeypatch):
    telegram, calls, waits, downloads = delivery

    def fail(*args):
        raise SummaryError("private response", code="quota_429", status_code=429)

    monkeypatch.setattr("reportbot.telegram.summarize_pdf", fail)
    with pytest.raises(SummaryError) as error:
        telegram.update_report_summary(report(), DECISION, 75)
    assert error.value.code == "quota_429"
    assert calls == []


def test_already_applied_caption_edit_is_completed_without_retry_or_leaking_description(monkeypatch, caplog):
    telegram = Telegram(TOKEN, CHAT_ID)
    posts = []
    description = "Bad Request: message is not modified: " + TOKEN

    def post(url, **kwargs):
        posts.append(kwargs)
        return SimpleNamespace(status_code=400, ok=False, json=lambda: {
            "ok": False, "error_code": 400, "description": description,
        })

    monkeypatch.setattr(telegram.session, "post", post)
    result = telegram.call("editMessageCaption", {"chat_id": CHAT_ID, "message_id": 75, "caption": "요약"})
    assert result == {"message_id": 75, "_reportbot_edit_already_applied": True}
    assert len(posts) == 1
    assert TOKEN not in caplog.text and description not in str(result)


@pytest.mark.parametrize("method,description", [
    ("editMessageCaption", "Bad Request: message to edit not found"),
    ("sendDocument", "Bad Request: message is not modified"),
])
def test_other_telegram_errors_are_not_treated_as_completed(monkeypatch, method, description):
    telegram = Telegram(TOKEN, CHAT_ID)
    monkeypatch.setattr(telegram.session, "post", lambda *args, **kwargs: SimpleNamespace(
        status_code=400, ok=False, json=lambda: {"ok": False, "error_code": 400, "description": description},
    ))
    with pytest.raises(TelegramError, match="400"):
        telegram.call(method, {"chat_id": CHAT_ID, "message_id": 75})

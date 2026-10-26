from dataclasses import replace
from datetime import date

import pytest

from reportbot.models import Report
from reportbot.telegram import Telegram, TelegramError, report_error_message


def report(**changes):
    item = Report(
        source="naver", source_id="46573", title="역래깅 이후, 시선은 구조조정으로",
        broker="신한투자증권", published_date=date(2026, 10, 21),
        detail_url="https://stock.naver.com/research/industry/46573",
        pdf_url="https://example.com/unavailable.pdf", sector="화학",
    )
    return replace(item, **changes)


def test_error_notice_contains_report_page_and_retry_information():
    item = report()
    message = report_error_message(item, "PDF 원문 확인")
    assert item.title in message
    assert "신한투자증권 · 2026-10-21" in message
    assert "섹터: 화학" in message
    assert "출처: 네이버 금융" in message
    assert "PDF 원문 확인" in message
    assert f'href="{item.detail_url}"' in message
    assert "리포트 원문 페이지 열기" in message
    assert item.pdf_url not in message
    assert "다음 일일 실행에서 다시 시도" in message


def test_error_notice_escapes_metadata_and_preserves_clickable_url():
    message = report_error_message(report(
        title="<화학 & 정유>", broker='증권사 "A"', sector="<소재>",
        detail_url='https://example.com/report?id=1&name="sector"',
    ), "PDF 다운로드·분석")
    assert "&lt;화학 &amp; 정유&gt;" in message
    assert "증권사 &quot;A&quot;" in message
    assert "섹터: &lt;소재&gt;" in message
    assert 'href="https://example.com/report?id=1&amp;name=&quot;sector&quot;"' in message


def test_error_notice_omits_unavailable_sector_and_bounds_long_unicode_metadata():
    message = report_error_message(report(
        title="<긴 제목 & 분석>😀" * 1000,
        broker="긴 증권사😀" * 1000,
        sector=None,
    ), "PDF 첨부 전송" * 1000)
    assert "섹터:" not in message
    assert len(message.encode("utf-16-le")) // 2 <= 4096
    assert 'href="https://stock.naver.com/research/industry/46573"' in message


def test_error_notice_rejects_oversized_url_instead_of_truncating_it():
    with pytest.raises(TelegramError, match="길이 제한"):
        report_error_message(report(detail_url="https://example.com/" + "a" * 5000), "PDF 원문 확인")


def test_send_report_error_uses_text_message_with_link_preview_disabled(monkeypatch):
    telegram = Telegram("123:fake-error-notice-token", "123456")
    calls = []

    def call(method, payload, **kwargs):
        calls.append((method, payload, kwargs))
        return {"message_id": 99}

    monkeypatch.setattr(telegram, "call", call)
    assert telegram.send_report_error(report(), "PDF 원문 확인") == {"message_id": 99}
    [(method, payload, kwargs)] = calls
    assert method == "sendMessage"
    assert payload["chat_id"] == "123456"
    assert payload["parse_mode"] == "HTML"
    assert payload["link_preview_options"] == {"is_disabled": True}
    assert "리포트 원문 페이지 열기" in payload["text"]
    assert kwargs == {}


def test_error_notice_delivery_failure_propagates(monkeypatch):
    telegram = Telegram("123:fake-error-notice-token", "123456")

    def fail(*args, **kwargs):
        raise TelegramError("텔레그램 연결 실패")

    monkeypatch.setattr(telegram, "call", fail)
    with pytest.raises(TelegramError, match="텔레그램 연결 실패"):
        telegram.send_report_error(report(), "PDF 원문 확인")

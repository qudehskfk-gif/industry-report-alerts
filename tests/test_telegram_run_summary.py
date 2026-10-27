import pytest

from reportbot.telegram import Telegram, TelegramError


def test_completion_report_uses_html_messages_and_paces_multiple_parts(monkeypatch):
    telegram = Telegram("123:fake-token", "-123456")
    report = {"totals": {"sent": 0, "linked": 0}}
    chunks = ["<b>실행 결과</b>", "제외 보고서 목록 &amp; 이유"]
    formatted, calls, waits = [], [], []

    def format_report(data):
        formatted.append(data)
        return chunks

    def call(method, payload):
        calls.append((method, payload))
        return {"message_id": len(calls)}

    monkeypatch.setattr("reportbot.run_summary.format_run_summary", format_report)
    monkeypatch.setattr(telegram, "call", call)
    monkeypatch.setattr("reportbot.telegram.time.sleep", waits.append)
    assert telegram.send_run_summary(report) == [{"message_id": 1}, {"message_id": 2}]
    assert formatted == [report]
    assert [method for method, payload in calls] == ["sendMessage", "sendMessage"]
    assert [payload["text"] for method, payload in calls] == chunks
    assert all(payload["parse_mode"] == "HTML" for method, payload in calls)
    assert all(payload["link_preview_options"] == {"is_disabled": True} for method, payload in calls)
    assert waits == [3.1]


def test_completion_report_leaves_a_gap_after_native_pdf_delivery(monkeypatch):
    telegram = Telegram("123:fake-token", "-123456")
    waits, sent = [], []
    monkeypatch.setattr("reportbot.run_summary.format_run_summary", lambda data: ["완료"])
    monkeypatch.setattr("reportbot.telegram.time.sleep", waits.append)
    monkeypatch.setattr(telegram, "send", sent.append)
    telegram.send_run_summary({"totals": {"sent": 3}})
    assert waits == [3.1]
    assert sent == ["완료"]


def test_uncertain_footer_delivery_stops_without_resending_parts(monkeypatch):
    telegram = Telegram("123:fake-token", "-123456")
    sent = []
    monkeypatch.setattr("reportbot.run_summary.format_run_summary", lambda data: ["첫 부분", "둘째 부분"])
    monkeypatch.setattr("reportbot.telegram.time.sleep", lambda seconds: None)

    def fail(text):
        sent.append(text)
        raise TelegramError("전송 결과를 확인하지 못했습니다.")

    monkeypatch.setattr(telegram, "send", fail)
    with pytest.raises(TelegramError):
        telegram.send_run_summary({"totals": {"sent": 0}})
    assert sent == ["첫 부분"]


def test_empty_completion_report_is_not_silently_ignored(monkeypatch):
    telegram = Telegram("123:fake-token", "-123456")
    monkeypatch.setattr("reportbot.run_summary.format_run_summary", lambda data: [])
    monkeypatch.setattr(telegram, "send", lambda text: pytest.fail("No complete report was produced"))
    with pytest.raises(TelegramError, match="실행 결과"):
        telegram.send_run_summary({"totals": {"sent": 0}})

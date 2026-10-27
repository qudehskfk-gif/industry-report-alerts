from contextlib import nullcontext
from datetime import date
import io
import sqlite3

from pypdf import PdfWriter
import pytest

from reportbot import summary_test
from reportbot.config import Config
from reportbot.models import Report
from reportbot.state import State


@pytest.fixture
def probe(tmp_path, monkeypatch):
    config = Config(tmp_path / "state.sqlite3", "fake-token", "-100424242", 20, 100, 86400, 14)
    state = State(config.state_path)
    reports = {}
    for source_id, outcome, timestamp in [
        ("old", "sent", "2026-10-25 04:00:00"),
        ("latest-sent", "sent", "2026-10-26 04:00:00"),
        ("pending", "pending", "2026-10-27 04:00:00"),
        ("notice", "error_notified", "2026-10-27 05:00:00"),
    ]:
        report = Report("naver", source_id, f"산업 분석 {source_id}", "테스트증권", date(2026, 10, 21),
                        f"https://example.com/{source_id}", f"https://example.com/{source_id}.pdf")
        reports[source_id] = report
        state.record(report, outcome, "hash-" + source_id)
        with state.db:
            state.db.execute("UPDATE reports SET processed_at=? WHERE source_id=?", (timestamp, source_id))
    state.close()
    original = config.state_path.read_bytes()

    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_blank_page(width=100, height=100)
    buffer = io.BytesIO()
    writer.write(buffer)
    pdf = buffer.getvalue()
    calls = []

    class Telegram:
        def __init__(self, token, chat_id):
            assert (token, chat_id) == (config.token, config.chat_id)

        def send(self, text):
            calls.append(text)
            return {"message_id": 1, "chat": {"type": "group"}}

        def send_report(self, *args):
            pytest.fail("A summary test must not re-send the PDF")

    monkeypatch.setattr(summary_test, "Telegram", Telegram)
    monkeypatch.setattr(summary_test, "make_session", lambda: nullcontext(object()))
    monkeypatch.setattr(summary_test, "resolve", lambda report, session: report)
    monkeypatch.setattr(summary_test, "download_pdf", lambda url: pdf)
    return config, reports, pdf, calls, original


def test_latest_actual_delivery_is_summarized_with_read_only_state(probe, monkeypatch, capsys):
    config, reports, pdf, calls, original = probe
    received = []
    connections = []
    connect = sqlite3.connect

    def read_only_connect(database_uri, **kwargs):
        connections.append((database_uri, kwargs))
        assert database_uri.endswith("?mode=ro")
        assert kwargs == {"uri": True}
        return connect(database_uri, **kwargs)

    def summarize(report, data, pages):
        received.append((report, data, pages))
        return [f"산업 전망 {index} (p.1)" for index in range(1, 11)]

    monkeypatch.setattr(summary_test.sqlite3, "connect", read_only_connect)
    monkeypatch.setattr(summary_test, "summarize_pdf", summarize)
    summary_test.send_summary_test(config)

    assert len(connections) == 1
    assert received == [(reports["latest-sent"], pdf, 2)]
    assert len(calls) == 1
    assert "산업 분석 latest-sent" in calls[0]
    assert "10. 산업 전망 10 (p.1)" in calls[0]
    assert "그룹방으로 10줄 요약 테스트를 보냈습니다." in capsys.readouterr().out
    assert config.state_path.read_bytes() == original


def test_disabled_or_missing_key_does_not_send_or_change_delivery_history(probe, monkeypatch):
    config, reports, pdf, calls, original = probe
    monkeypatch.setattr(summary_test, "summarize_pdf", lambda *args: None)
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        summary_test.send_summary_test(config)
    assert calls == []
    assert config.state_path.read_bytes() == original


def test_no_delivered_reports_does_not_download_or_send(probe, monkeypatch):
    config, reports, pdf, calls, original = probe
    with sqlite3.connect(config.state_path) as database:
        database.execute("UPDATE reports SET outcome='pending' WHERE outcome='sent'")
    before = config.state_path.read_bytes()
    monkeypatch.setattr(summary_test, "download_pdf", lambda *args: pytest.fail("No delivered report to download"))
    with pytest.raises(ValueError, match="기존 발송 자료"):
        summary_test.send_summary_test(config)
    assert calls == []
    assert config.state_path.read_bytes() == before


def test_main_redacts_api_errors(probe, monkeypatch, capsys):
    config, reports, pdf, calls, original = probe
    monkeypatch.setattr(summary_test.Config, "from_env", lambda: config)

    def fail(*args):
        raise RuntimeError("sensitive-api-key-and-response")

    monkeypatch.setattr(summary_test, "summarize_pdf", fail)
    assert summary_test.main() == 1
    output = capsys.readouterr().out
    assert "RuntimeError" in output
    assert "sensitive-api-key-and-response" not in output
    assert calls == []
    assert config.state_path.read_bytes() == original

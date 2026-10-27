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
            pytest.fail("The summary must be part of the PDF caption, not a separate message")

        def call(self, method, payload, *, files):
            assert method == "sendDocument"
            filename, document, mime = files["document"]
            calls.append((method, payload, document.read(), filename, mime))
            return {"message_id": 1, "chat": {"type": "group"}, "document": {"file_id": "test-pdf"}}

    monkeypatch.setattr(summary_test, "Telegram", Telegram)
    monkeypatch.setattr(summary_test, "make_session", lambda: nullcontext(object()))
    monkeypatch.setattr(summary_test, "resolve", lambda report, session: report)
    monkeypatch.setattr(summary_test, "download_pdf", lambda url: pdf)
    monkeypatch.setattr(summary_test, "check_provider_connection", lambda: None)
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
        return [{"heading": f"핵심 주제 {index}", "points": [f"수요 변화의 근거 {index}-{point}" for point in range(1, 4)]} for index in range(1, 6)]

    monkeypatch.setattr(summary_test.sqlite3, "connect", read_only_connect)
    monkeypatch.setattr(summary_test, "summarize_pdf", summarize)
    summary_test.send_summary_test(config)

    assert len(connections) == 1
    assert received == [(reports["latest-sent"], pdf, 2)]
    assert len(calls) == 1
    method, payload, attachment, filename, mime = calls[0]
    assert method == "sendDocument"
    assert payload["chat_id"] == config.chat_id
    assert payload["parse_mode"] == "HTML"
    assert "산업 분석 latest-sent" in payload["caption"]
    assert all(f"<b>{index}. 핵심 주제 {index}</b>" in payload["caption"] for index in range(1, 6))
    assert len([line for line in payload["caption"].splitlines() if line.startswith("• ")]) == 15
    assert "• 수요 변화의 근거 5-3" in payload["caption"]
    assert "(p." not in payload["caption"]
    assert attachment == pdf
    assert filename.endswith(".pdf")
    assert mime == "application/pdf"
    assert "그룹방으로 주제별 요약과 PDF 원문을 한 메시지로 보냈습니다." in capsys.readouterr().out
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


def test_main_prints_only_fixed_summary_diagnostics(probe, monkeypatch, capsys):
    config, reports, pdf, calls, original = probe
    monkeypatch.setattr(summary_test.Config, "from_env", lambda: config)

    def fail(*args):
        raise summary_test.SummaryError(
            "sensitive-api-key-and-response", code="http_400", status_code=400,
            reason="API_KEY_INVALID",
        )

    monkeypatch.setattr(summary_test, "summarize_pdf", fail)
    assert summary_test.main() == 1
    output = capsys.readouterr().out
    assert "SummaryError: http_400; HTTP 400; API_KEY_INVALID" in output
    assert "sensitive-api-key-and-response" not in output
    assert calls == []
    assert config.state_path.read_bytes() == original


def test_failed_connection_check_stops_before_pdf_download(probe, monkeypatch, capsys):
    config, reports, pdf, calls, original = probe
    monkeypatch.setattr(summary_test.Config, "from_env", lambda: config)
    monkeypatch.setattr(summary_test, "download_pdf", lambda *args: pytest.fail("Connection check failed before PDF download"))

    def fail():
        raise summary_test.SummaryError("sensitive-error", code="transport_error", reason="ReadTimeout")

    monkeypatch.setattr(summary_test, "check_provider_connection", fail)
    assert summary_test.main() == 1
    output = capsys.readouterr().out
    assert "transport_error; ReadTimeout" in output
    assert "sensitive-error" not in output
    assert calls == []
    assert config.state_path.read_bytes() == original

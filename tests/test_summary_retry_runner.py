from contextlib import nullcontext
from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pytest

from reportbot.classify import Decision
from reportbot.config import Config
from reportbot.models import Report
from reportbot.state import State, summary_destination_key
from reportbot.summarize import SummaryError


DIGEST = "a" * 64


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    from reportbot import runner

    config = Config(tmp_path / "state.sqlite3", "123:fake-bot-token", "-123456", 20, 100, 86400, 14, 45)
    item = Report("hankyung", "100", "산업 구조의 변화", "테스트증권", date(2026, 10, 27),
                  "https://example.com/report", "https://example.com/report.pdf", sector="반도체")
    destination = summary_destination_key(config.token, config.chat_id)
    events = []
    controls = SimpleNamespace(
        delivery={"message_id": 71, "_reportbot_summary_status": "pending",
                  "_reportbot_summary_error": "http_503"},
        edit_error=None, inspection_hash=DIGEST,
    )

    class Telegram:
        def __init__(self, token, chat_id):
            assert token == config.token
            assert chat_id == config.chat_id

        def send_report(self, report, decision):
            events.append(("upload", report.source_id))
            return controls.delivery

        def update_report_summary(self, report, decision, message_id):
            events.append(("edit", report.source_id, message_id, decision.pdf_hash))
            if controls.edit_error:
                raise controls.edit_error
            return {"message_id": message_id}

        def send_report_error(self, *args):
            pytest.fail("Summary failures must never create PDF-error notices")

    def inspect(*args):
        events.append(("inspect",))
        return 25, "PDF original", controls.inspection_hash

    monkeypatch.setenv("GEMINI_API_KEY", "fake-summary-key")
    monkeypatch.setenv("SUMMARY_MODE", "auto")
    monkeypatch.setattr(runner, "Telegram", Telegram)
    monkeypatch.setattr(runner, "make_session", lambda: nullcontext(object()))
    monkeypatch.setattr(runner, "classify", lambda *args: Decision(True, "산업 리포트 25쪽", 25, DIGEST))
    monkeypatch.setattr(runner, "inspect_pdf", inspect)
    monkeypatch.setattr(runner, "SOURCES", {"hankyung": lambda *args, **kwargs: [item]})
    return runner, config, item, destination, events, controls


def queue(state, item, destination, message_id=71):
    state.record(item, "sent", DIGEST, summary_job={
        "message_id": message_id, "destination_key": destination, "error_code": "http_503",
    })


def job_row(state):
    return state.db.execute("SELECT state,attempts,last_error_code FROM summary_jobs").fetchone()


def test_pdf_upload_is_recorded_with_pending_summary_then_next_run_edits_same_message(recovery):
    runner, config, item, destination, events, controls = recovery
    assert runner.check(config) == 0
    assert events == [("upload", "100")]
    state = State(config.state_path)
    before = state.db.execute("SELECT processed_at FROM reports").fetchone()
    fingerprints = state.db.execute("SELECT * FROM delivered ORDER BY kind,fingerprint").fetchall()
    assert state.outcome(item) == "sent"
    assert job_row(state) == ("pending", 0, "http_503")
    state.close()

    assert runner.check(config) == 0
    assert events == [("upload", "100"), ("inspect",), ("edit", "100", 71, DIGEST)]
    state = State(config.state_path)
    assert job_row(state) == ("completed", 0, "http_503")
    assert state.outcome(item) == "sent"
    assert state.db.execute("SELECT processed_at FROM reports").fetchone() == before
    assert state.db.execute("SELECT * FROM delivered ORDER BY kind,fingerprint").fetchall() == fingerprints
    state.close()
    assert runner.check(config) == 0
    assert sum(event[0] == "upload" for event in events) == 1
    assert sum(event[0] == "edit" for event in events) == 1


def test_failed_summary_recovery_never_reuploads_pdf_and_stops_after_three_attempts(recovery, caplog):
    runner, config, item, destination, events, controls = recovery
    state = State(config.state_path)
    queue(state, item, destination)
    state.close()
    controls.edit_error = SummaryError("private-provider-response", code="http_503", status_code=503)

    for attempt in range(1, 4):
        assert runner.check(config) == 0
        state = State(config.state_path)
        assert job_row(state) == ("failed" if attempt == 3 else "pending", attempt, "http_503")
        assert state.outcome(item) == "sent"
        if attempt != 3:
            with state.db:
                state.db.execute("UPDATE summary_jobs SET next_attempt_at='2000-01-01 00:00:00'")
        state.close()
    assert runner.check(config) == 0
    assert not any(event[0] == "upload" for event in events)
    assert sum(event[0] == "edit" for event in events) == 3
    assert "private-provider-response" not in caplog.text


def test_not_due_summary_is_not_attempted(recovery):
    runner, config, item, destination, events, controls = recovery
    state = State(config.state_path)
    queue(state, item, destination)
    with state.db:
        state.db.execute("UPDATE summary_jobs SET next_attempt_at='2999-01-01 00:00:00'")
    state.close()
    assert runner.check(config) == 0
    assert events == []


def test_summary_for_another_destination_is_never_edited(recovery):
    runner, config, item, destination, events, controls = recovery
    state = State(config.state_path)
    queue(state, item, summary_destination_key(config.token, "-different-group"))
    state.close()
    assert runner.check(config) == 0
    assert events == []


@pytest.mark.parametrize("kwargs", [{"preview_only": True}, {"dry_run": True}])
def test_preview_and_dry_run_never_edit_existing_messages(recovery, kwargs):
    runner, config, item, destination, events, controls = recovery
    state = State(config.state_path)
    queue(state, item, destination)
    state.close()
    assert runner.check(config, **kwargs) == 0
    assert not any(event[0] in {"upload", "edit"} for event in events)
    state = State(config.state_path)
    assert job_row(state) == ("pending", 0, "http_503")
    state.close()


@pytest.mark.parametrize("mode,has_key", [("off", True), ("auto", False)])
def test_disabled_summary_configuration_keeps_recovery_pending_without_attempts(recovery, monkeypatch, mode, has_key):
    runner, config, item, destination, events, controls = recovery
    state = State(config.state_path)
    queue(state, item, destination)
    state.close()
    monkeypatch.setenv("SUMMARY_MODE", mode)
    if not has_key:
        monkeypatch.delenv("GEMINI_API_KEY")
    assert runner.check(config) == 0
    assert events == []
    state = State(config.state_path)
    assert job_row(state) == ("pending", 0, "http_503")
    state.close()


def test_changed_original_pdf_cannot_update_the_delivered_message(recovery):
    runner, config, item, destination, events, controls = recovery
    state = State(config.state_path)
    queue(state, item, destination)
    state.close()
    controls.inspection_hash = "b" * 64
    assert runner.check(config) == 0
    assert events == [("inspect",)]
    state = State(config.state_path)
    assert job_row(state) == ("pending", 1, "pdf_hash_mismatch")
    assert state.outcome(item) == "sent"
    state.close()


@pytest.mark.parametrize("delivery", [None, {"message_id": 71},
    {"message_id": 71, "_reportbot_summary_status": "complete"},
    {"message_id": 71, "_reportbot_summary_status": "disabled"},
    {"message_id": True, "_reportbot_summary_status": "pending"},
])
def test_legacy_or_nonpending_deliveries_do_not_create_recovery_jobs(recovery, delivery):
    runner, config, item, destination, events, controls = recovery
    controls.delivery = delivery
    assert runner.check(config) == 0
    state = State(config.state_path)
    assert state.outcome(item) == "sent"
    assert state.db.execute("SELECT COUNT(*) FROM summary_jobs").fetchone()[0] == 0
    state.close()


def test_quota_failure_does_not_consume_every_queued_reports_attempts(recovery, monkeypatch):
    runner, config, item, destination, events, controls = recovery
    other = replace(item, source_id="101", title="다른 산업의 변화")
    state = State(config.state_path)
    queue(state, item, destination, 71)
    queue(state, other, destination, 72)
    state.close()
    monkeypatch.setattr(runner, "SOURCES", {"hankyung": lambda *args, **kwargs: [item, other]})
    controls.edit_error = SummaryError("private-quota-response", code="quota_429", status_code=429)
    assert runner.check(config) == 0
    assert sum(event[0] == "edit" for event in events) == 1
    state = State(config.state_path)
    assert state.db.execute("SELECT attempts FROM summary_jobs ORDER BY source_id").fetchall() == [(1,), (0,)]
    state.close()

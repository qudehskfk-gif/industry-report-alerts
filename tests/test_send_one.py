from contextlib import nullcontext
from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pytest
import requests

from reportbot.classify import Decision
from reportbot.config import Config
from reportbot.models import Report
from reportbot.state import State, summary_destination_key


DIGEST = "b" * 64


@pytest.fixture
def selected(tmp_path, monkeypatch):
    from reportbot import send_one

    config = Config(tmp_path / "state.sqlite3", "123:fake-token", "-123456", 10, 100, 86400, 14, 45)
    item = Report("naver", "46691", "조선에서 해운으로 바통터치", "대신증권", date(2026, 10, 28),
                  "https://example.com/46691", "https://example.com/46691.pdf", sector="조선")
    other = replace(item, source_id="46690", title="인프라를 가진 지주")
    state = State(config.state_path)
    state.record(item, "duplicate")
    state.record(other, "pending")
    state.close()
    events = SimpleNamespace(sent=[], inspected=[])
    controls = SimpleNamespace(digest=DIGEST, accepted=True, failure=None,
                               delivery={"message_id": 71, "_reportbot_summary_status": "complete"})

    class Telegram:
        def __init__(self, token, chat_id):
            assert (token, chat_id) == (config.token, config.chat_id)

        def send_report(self, report, decision):
            if controls.failure:
                raise controls.failure
            events.sent.append(report)
            return controls.delivery

    def classify(report, session, minimum):
        events.inspected.append(report)
        assert minimum == 10
        return Decision(controls.accepted, "산업 리포트 15쪽", 15, controls.digest)

    monkeypatch.setattr(send_one, "Telegram", Telegram)
    monkeypatch.setattr(send_one, "make_session", lambda: nullcontext(object()))
    monkeypatch.setattr(send_one, "classify", classify)
    return send_one, config, item, other, events, controls


def test_sends_only_selected_report_and_preserves_other_rows(selected):
    module, config, item, other, events, _ = selected
    state = State(config.state_path)
    before = state.db.execute("SELECT * FROM reports WHERE source_id=?", (other.source_id,)).fetchone()
    state.close()
    result = module.send_one(config, "naver", "46691", DIGEST)
    assert result == {"status": "sent", "source": "naver", "source_id": "46691", "summary_status": "complete"}
    assert events.sent == events.inspected == [item]
    state = State(config.state_path)
    assert state.outcome(item) == "sent"
    assert state.db.execute("SELECT * FROM reports WHERE source_id=?", (other.source_id,)).fetchone() == before
    state.close()
    assert module.send_one(config, "naver", "46691", DIGEST)["status"] == "already_sent"
    assert events.sent == [item]


def test_same_file_already_sent_with_other_title_is_not_uploaded(selected):
    module, config, item, other, events, _ = selected
    state = State(config.state_path)
    state.record(other, "sent", DIGEST)
    state.close()
    assert module.send_one(config, "naver", "46691")["status"] == "duplicate"
    assert events.sent == []


def test_same_title_wrong_attachment_does_not_hide_selected_pdf(selected):
    module, config, item, other, events, _ = selected
    state = State(config.state_path)
    state.record(replace(other, title=item.title), "sent", "a" * 64)
    state.close()
    assert module.send_one(config, "naver", "46691")["status"] == "sent"
    assert events.sent == [item]


@pytest.mark.parametrize("change", ["wrong_hash", "rejected", "invalid_digest"])
def test_validation_failure_does_not_upload_or_mark_sent(selected, change):
    module, config, item, _, events, controls = selected
    if change == "rejected":
        controls.accepted = False
    if change == "invalid_digest":
        controls.digest = "not-a-pdf-digest"
    expected = "c" * 64 if change == "wrong_hash" else DIGEST
    with pytest.raises(ValueError):
        module.send_one(config, "naver", "46691", expected)
    assert events.sent == []
    state = State(config.state_path)
    assert state.outcome(item) == "duplicate"
    state.close()


def test_pending_summary_receipt_persists_without_other_report_processing(selected):
    module, config, item, _, events, controls = selected
    controls.delivery = {"message_id": 82, "_reportbot_summary_status": "pending",
                         "_reportbot_summary_error": "http_503"}
    result = module.send_one(config, "naver", "46691")
    assert result["summary_status"] == "pending"
    state = State(config.state_path)
    assert state.outcome(item) == "sent"
    jobs = state.pending_summaries(summary_destination_key(config.token, config.chat_id))
    assert len(jobs) == 1
    assert jobs[0]["message_id"] == 82
    assert jobs[0]["report"] == item
    state.close()
    assert events.sent == [item]


@pytest.mark.parametrize("outcome", ["error_notified", "pending: error notice", "rejected: 정기 자료", "historical_pre_start"])
def test_terminal_pdf_policy_cannot_be_overridden(selected, outcome):
    module, config, item, _, events, _ = selected
    state = State(config.state_path)
    state.record(item, outcome)
    state.close()
    with pytest.raises(ValueError):
        module.send_one(config, "naver", "46691")
    assert events.inspected == events.sent == []


def test_upload_failure_leaves_report_unsent(selected):
    module, config, item, _, events, controls = selected
    controls.failure = requests.Timeout("private provider detail")
    with pytest.raises(requests.Timeout):
        module.send_one(config, "naver", "46691")
    state = State(config.state_path)
    assert state.outcome(item) == "duplicate"
    assert state.duplicate_pdf_match(DIGEST) is None
    state.close()
    assert events.sent == []


@pytest.mark.parametrize("source,report_id", [("other", "46691"), ("naver", "../46691"), ("naver", "")])
def test_invalid_selection_is_rejected_without_upload(selected, source, report_id):
    module, config, _, _, events, _ = selected
    with pytest.raises(ValueError):
        module.send_one(config, source, report_id)
    assert events.inspected == events.sent == []

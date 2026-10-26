from contextlib import nullcontext
from dataclasses import replace
from datetime import date, timedelta

import pytest
import requests

from reportbot.classify import Decision
from reportbot.config import Config
from reportbot.models import Report
from reportbot.state import State
from reportbot.telegram import Telegram, TelegramError


def report(source="hankyung", source_id="100", **changes):
    item = Report(
        source=source, source_id=source_id,
        title=f"산업 구조의 변화 {source_id}", broker="미래증권",
        published_date=date.today(),
        detail_url=f"https://example.com/{source}/{source_id}",
        pdf_url=f"https://example.com/{source}/{source_id}.pdf",
    )
    return replace(item, **changes)


@pytest.fixture
def config(tmp_path):
    return Config(tmp_path / "state.sqlite3", "123:dummy-token", "123456", 20, 100, 86400, 14, 45)


@pytest.fixture
def notices(monkeypatch):
    captured = []
    monkeypatch.setattr(Telegram, "send_report_error", lambda self, item, reason: captured.append((item, reason)), raising=False)
    return captured


@pytest.fixture
def runner(monkeypatch, notices):
    from reportbot import runner

    monkeypatch.setattr(runner, "make_session", lambda: nullcontext(object()))
    monkeypatch.setattr(runner, "classify", lambda r, *a: Decision(True, "산업 리포트 25쪽", 25, f"{r.source}:{r.source_id}"))
    return runner


def capture_sends(monkeypatch):
    sent = []
    monkeypatch.setattr(Telegram, "send_report", lambda self, r, decision: sent.append(r))
    return sent


def test_cutover_sends_known_baseline_and_new_upload_but_not_older_untracked_archive(runner, monkeypatch, config):
    known = report(source_id="100")
    archive = report(source_id="90")
    late = report(source_id="101", published_date=date.today() - timedelta(days=30))
    state = State(config.state_path)
    state.baseline("hankyung", [known])
    state.close()
    monkeypatch.setattr(runner, "SOURCES", {"hankyung": lambda *a, **kw: [archive, known, late]})
    sent = capture_sends(monkeypatch)

    assert runner.check(config) == 0
    assert set(sent) == {known, late}
    state = State(config.state_path)
    try:
        assert state.outcome(archive) == "historical_pre_start"
        assert state.outcome(known) == "sent"
        assert state.outcome(late) == "sent"
        assert state.pending() == []
    finally:
        state.close()
    assert runner.check(config) == 0
    assert len(sent) == 2


def test_old_publication_date_does_not_exclude_a_later_registered_report(runner, monkeypatch, config):
    first = report(source_id="100")
    late = report(source_id="101", published_date=date.today() - timedelta(days=30))
    listing = [first]
    monkeypatch.setattr(runner, "SOURCES", {"hankyung": lambda *a, **kw: list(listing)})
    sent = capture_sends(monkeypatch)
    assert runner.check(config) == 0
    listing.append(late)
    assert runner.check(config) == 0
    assert sent == [first, late]


@pytest.mark.parametrize("failure_stage", ["pdf", "upload"])
def test_pending_failure_retries_after_source_outage_and_listing_disappearance(runner, monkeypatch, config, failure_stage, notices):
    queued = report(source_id="100")
    monkeypatch.setattr(runner, "SOURCES", {"hankyung": lambda *a, **kw: [queued]})
    sent = capture_sends(monkeypatch)
    original_classify = runner.classify

    def fail(*a, **kw):
        raise requests.Timeout("temporary failure")

    if failure_stage == "pdf":
        monkeypatch.setattr(runner, "classify", fail)
    else:
        monkeypatch.setattr(Telegram, "send_report", fail)
    assert runner.check(config) == 0
    assert sent == []
    assert [item for item, reason in notices] == [queued]
    assert notices[0][1]
    state = State(config.state_path)
    try:
        assert state.pending() == [queued]
        assert not state.duplicate(queued)
    finally:
        state.close()

    monkeypatch.setattr(runner, "classify", original_classify)
    monkeypatch.setattr(Telegram, "send_report", lambda self, r, d: sent.append(r))
    monkeypatch.setattr(runner, "SOURCES", {"hankyung": fail})
    # Collection still reports the outage, while the previously saved PDF is delivered.
    assert runner.check(config) == 1
    assert sent == [queued]
    assert len(notices) == 1
    state = State(config.state_path)
    try:
        assert state.pending() == []
        assert state.outcome(queued) == "sent"
    finally:
        state.close()


def test_preview_saves_pending_but_does_not_turn_qualified_reports_into_deliveries(runner, monkeypatch, config):
    first = report(source="hankyung", source_id="100")
    mirror = replace(first, source="naver", source_id="200")
    monkeypatch.setattr(runner, "SOURCES", {
        "hankyung": lambda *a, **kw: [first],
        "naver": lambda *a, **kw: [mirror],
    })
    monkeypatch.setattr(runner, "classify", lambda *a: Decision(True, "산업 리포트 25쪽", 25, "same-pdf"))
    sent = capture_sends(monkeypatch)
    assert runner.check(config, preview_only=True) == 0
    assert sent == []
    state = State(config.state_path)
    try:
        assert set(state.pending()) == {first, mirror}
        assert not state.duplicate(first, "same-pdf")
        assert not state.duplicate(mirror, "same-pdf")
    finally:
        state.close()
    # Actual delivery suppresses the mirror only after the first PDF was accepted.
    assert runner.check(config) == 0
    assert len(sent) == 1
    state = State(config.state_path)
    try:
        assert state.pending() == []
        assert {state.outcome(first), state.outcome(mirror)} == {"sent", "duplicate"}
    finally:
        state.close()


def test_daily_delivery_has_no_five_report_limit(runner, monkeypatch, config):
    items = [report(source_id=str(100 + index)) for index in range(8)]
    monkeypatch.setattr(runner, "SOURCES", {"hankyung": lambda *a, **kw: list(items)})
    sent = capture_sends(monkeypatch)
    assert runner.check(config) == 0
    assert set(sent) == set(items)
    assert len(sent) == 8


def test_report_error_notice_allows_success_and_keeps_retry_and_delivery_history(runner, monkeypatch, config, notices):
    failed = report(source_id="100")
    healthy = report(source_id="101")
    monkeypatch.setattr(runner, "SOURCES", {"hankyung": lambda *a, **kw: [failed, healthy]})
    original_classify = runner.classify

    def partial(item, *args):
        if item == failed:
            raise requests.Timeout("sensitive remote exception must not enter notice")
        return original_classify(item, *args)

    monkeypatch.setattr(runner, "classify", partial)
    sent = capture_sends(monkeypatch)
    assert runner.check(config) == 0
    assert sent == [healthy]
    assert [item for item, reason in notices] == [failed]
    assert "sensitive" not in notices[0][1]
    state = State(config.state_path)
    try:
        assert state.outcome(failed) == "pending: retry"
        assert state.pending() == [failed]
        assert not state.duplicate(failed)
        assert state.outcome(healthy) == "sent"
        assert state.duplicate(healthy)
    finally:
        state.close()

    monkeypatch.setattr(runner, "classify", original_classify)
    assert runner.check(config) == 0
    assert sent == [healthy, failed]
    assert len(notices) == 1


def test_no_error_notices_for_healthy_rejected_or_duplicate_reports(runner, monkeypatch, config, notices):
    healthy = report(source_id="100")
    periodic = report(source_id="101", title="반도체 Weekly")
    duplicate = report(source_id="99")
    state = State(config.state_path)
    try:
        state.record(duplicate, "sent", "previously-delivered-pdf")
    finally:
        state.close()
    monkeypatch.setattr(runner, "SOURCES", {"hankyung": lambda *a, **kw: [healthy, periodic, duplicate]})
    sent = capture_sends(monkeypatch)
    assert runner.check(config) == 0
    assert sent == [healthy]
    assert notices == []


@pytest.mark.parametrize("mode", ["dry_run", "preview_only"])
def test_preview_errors_send_neither_pdf_nor_error_notice(runner, monkeypatch, config, notices, mode):
    failed = report()
    monkeypatch.setattr(runner, "SOURCES", {"hankyung": lambda *a, **kw: [failed]})

    def fail(*a, **kw):
        raise requests.Timeout("PDF unavailable")

    monkeypatch.setattr(runner, "classify", fail)
    sent = capture_sends(monkeypatch)
    assert runner.check(config, **{mode: True}) == 0
    assert sent == []
    assert notices == []
    if mode == "preview_only":
        state = State(config.state_path)
        try:
            assert state.pending() == [failed]
            assert not state.duplicate(failed)
        finally:
            state.close()
    else:
        assert not config.state_path.exists()


def test_notice_delivery_failure_remains_visible_and_does_not_stop_other_notices(runner, monkeypatch, config, caplog):
    items = [report(source_id="100"), report(source_id="101")]
    monkeypatch.setattr(runner, "SOURCES", {"hankyung": lambda *a, **kw: items})

    def fail_pdf(*a, **kw):
        raise requests.Timeout("private-pdf-exception")

    attempts = []

    def send_notice(self, item, reason):
        attempts.append(item)
        if len(attempts) == 1:
            raise TelegramError("123:private-telegram-exception")

    monkeypatch.setattr(runner, "classify", fail_pdf)
    monkeypatch.setattr(Telegram, "send_report_error", send_notice)
    sent = capture_sends(monkeypatch)
    assert runner.check(config) == 1
    assert attempts == items
    assert sent == []
    assert "private-pdf-exception" not in caplog.text
    assert "private-telegram-exception" not in caplog.text
    state = State(config.state_path)
    try:
        assert set(state.pending()) == set(items)
        assert all(not state.duplicate(item) for item in items)
    finally:
        state.close()


def test_every_failed_report_gets_a_separate_link_notice(runner, monkeypatch, config, notices):
    items = [report(source_id=str(index)) for index in range(100, 107)]
    monkeypatch.setattr(runner, "SOURCES", {"hankyung": lambda *a, **kw: items})

    def fail(*a, **kw):
        raise requests.Timeout("PDF unavailable")

    monkeypatch.setattr(runner, "classify", fail)
    sent = capture_sends(monkeypatch)
    assert runner.check(config) == 0
    assert sent == []
    assert [item.detail_url for item, reason in notices] == [item.detail_url for item in items]


def test_repair_resends_exact_previous_five_then_records_them_as_delivered(runner, monkeypatch, config):
    identities = [("hankyung", "653252"), ("naver", "46614"), ("naver", "46612"), ("naver", "46598"), ("naver", "46589")]
    items = [report(source=source, source_id=source_id) for source, source_id in identities]
    extra = report(source="naver", source_id="46615")
    monkeypatch.setattr(runner, "SOURCES", {
        "hankyung": lambda *a, **kw: [items[0]],
        "naver": lambda *a, **kw: items[1:] + [extra],
    })
    sent = capture_sends(monkeypatch)
    assert runner.repair_five(config) == 0
    assert sent == items
    state = State(config.state_path)
    try:
        assert state.pending() == [extra]
        for item in items:
            assert state.outcome(item) == "sent"
            assert state.duplicate(item)
    finally:
        state.close()
    assert runner.repair_five(config) == 0
    assert sent == items


def test_repair_checkpoints_success_before_a_later_send_failure(runner, monkeypatch, config):
    identities = [("hankyung", "653252"), ("naver", "46614"), ("naver", "46612"), ("naver", "46598"), ("naver", "46589")]
    items = [report(source=source, source_id=source_id) for source, source_id in identities]
    monkeypatch.setattr(runner, "SOURCES", {
        "hankyung": lambda *a, **kw: [items[0]],
        "naver": lambda *a, **kw: items[1:],
    })
    sent = []

    def partial(self, item, decision):
        if len(sent) == 2:
            raise TelegramError("upload result uncertain")
        sent.append(item)

    monkeypatch.setattr(Telegram, "send_report", partial)
    assert runner.repair_five(config) == 1
    assert sent == items[:2]
    monkeypatch.setattr(Telegram, "send_report", lambda self, r, d: sent.append(r))
    assert runner.repair_five(config) == 0
    assert sent == items

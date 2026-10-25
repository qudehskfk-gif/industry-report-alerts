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
def runner(monkeypatch):
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
def test_pending_failure_retries_after_source_outage_and_listing_disappearance(runner, monkeypatch, config, failure_stage):
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
    assert runner.check(config) == 1
    assert sent == []
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

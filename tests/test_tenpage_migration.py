from dataclasses import replace
from datetime import date, timedelta
import json

import pytest

from reportbot.models import Report
from reportbot.state import State


TODAY = date(2026, 10, 27)


def report(source_id="100", **changes):
    item = Report(
        "naver", source_id, f"산업 전망 {source_id}", "테스트증권", TODAY,
        f"https://example.com/{source_id}", f"https://example.com/{source_id}.pdf",
    )
    return replace(item, **changes)


def rejected(state, item, pages):
    state.record(item, f"rejected: 심층 표시 없음·{pages}쪽", f"pdf-{item.source_id}")


def test_recent_ten_to_nineteen_page_reports_reopen_without_replaying_short_reports(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    items = [report(str(pages), source="hankyung" if pages == 17 else "naver")
             for pages in [9, 10, 15, 17, 19]]
    for item, pages in zip(items, [9, 10, 15, 17, 19]):
        rejected(state, item, pages)
    assert state.requeue_page_threshold(10, today=TODAY) == 4
    assert {item.source_id for item in state.pending()} == {"10", "15", "17", "19"}
    assert state.outcome(items[0]) == "rejected: 심층 표시 없음·9쪽"
    assert state.requeue_page_threshold(10, today=TODAY) == 0
    state.close()


def test_only_publication_dates_inside_the_current_scan_window_reopen(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    on_boundary = report("boundary", published_date=TODAY - timedelta(days=45))
    outside = report("old", published_date=TODAY - timedelta(days=46))
    future = report("future", published_date=TODAY + timedelta(days=1))
    for item in [on_boundary, outside, future]:
        rejected(state, item, 17)
    assert state.requeue_page_threshold(10, today=TODAY) == 1
    assert state.pending() == [on_boundary]
    assert state.outcome(outside).startswith("rejected:")
    assert state.outcome(future).startswith("rejected:")
    state.close()


@pytest.mark.parametrize("outcome", [
    "sent", "duplicate", "error_notified", "pending: error notice", "pending",
    "historical_pre_start", "rejected: 정기 동향·실적 점검 자료",
    "rejected: 표지의 정기 동향 자료 표시", "rejected: 개별 종목 리포트 표시",
    "rejected: 산업 분류가 아님", "skipped: 심층 표시 없음·17쪽",
    "rejected: 심층 표시 없음·17쪽 extra",
])
def test_other_processing_outcomes_are_never_reopened(tmp_path, outcome):
    state = State(tmp_path / "state.sqlite3")
    item = report()
    state.record(item, outcome, "original-hash")
    before = state.db.execute("SELECT * FROM reports").fetchall()
    assert state.requeue_page_threshold(10, today=TODAY) == 0
    assert state.db.execute("SELECT * FROM reports").fetchall() == before
    state.close()


def test_requeue_preserves_original_timestamps_metadata_hashes_and_other_ledgers(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    candidate, sent = report("candidate"), report("sent")
    rejected(state, candidate, 17)
    state.record(sent, "sent", "sent-hash", summary_job={
        "message_id": 52, "destination_key": "a" * 24, "error_code": "http_503",
    })
    with state.db:
        state.db.execute("UPDATE reports SET processed_at='2026-10-26 01:02:03'")
    candidate_before = state.db.execute("""
        SELECT title,processed_at,metadata_json,pdf_hash FROM reports WHERE source_id='candidate'
    """).fetchone()
    other_before = {
        table: state.db.execute(f"SELECT * FROM {table}").fetchall()
        for table in ["delivered", "summary_jobs", "sources"]
    }
    sent_before = state.db.execute("SELECT * FROM reports WHERE source_id='sent'").fetchone()
    assert state.requeue_page_threshold(10, today=TODAY) == 1
    assert state.db.execute("""
        SELECT title,processed_at,metadata_json,pdf_hash FROM reports WHERE source_id='candidate'
    """).fetchone() == candidate_before
    assert state.db.execute("SELECT * FROM reports WHERE source_id='sent'").fetchone() == sent_before
    assert all(state.db.execute(f"SELECT * FROM {table}").fetchall() == rows
               for table, rows in other_before.items())
    assert state.outcome(candidate) == "pending"
    state.close()


def test_policy_marker_tracks_last_active_threshold_and_handles_threshold_toggles(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    item = report()
    rejected(state, item, 15)
    assert state.requeue_page_threshold(10, today=TODAY) == 1
    state.record(item, "rejected: 최소 20쪽 미만·15쪽", "original-hash")
    assert state.requeue_page_threshold(20, today=TODAY) == 0
    marker = json.loads(state.get_setting("page_threshold_recheck_v1"))
    assert marker == {"min_pages": 20, "lookback_days": 45}
    assert state.requeue_page_threshold(10, today=TODAY) == 1
    assert json.loads(state.get_setting("page_threshold_recheck_v1"))["min_pages"] == 10
    state.close()


def test_later_metadata_hydration_is_not_blocked_by_an_existing_policy_marker(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    item = report()
    rejected(state, item, 17)
    with state.db:
        state.db.execute("UPDATE reports SET metadata_json=NULL")
    assert state.requeue_page_threshold(10, today=TODAY) == 0
    state.ingest("naver", [item])
    assert state.requeue_page_threshold(10, today=TODAY) == 1
    assert state.pending() == [item]
    state.close()


def test_a_wider_lookback_policy_can_reopen_additional_known_page_rejections(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    item = report(published_date=TODAY - timedelta(days=60))
    rejected(state, item, 17)
    assert state.requeue_page_threshold(10, today=TODAY) == 0
    assert state.requeue_page_threshold(10, lookback_days=90, today=TODAY) == 1
    assert json.loads(state.get_setting("page_threshold_recheck_v1"))["lookback_days"] == 90
    state.close()


@pytest.mark.parametrize("raw", ["not-json", "{}", '"not-an-object"'])
def test_malformed_legacy_metadata_does_not_break_the_recheck(tmp_path, raw):
    state = State(tmp_path / "state.sqlite3")
    rejected(state, report(), 17)
    with state.db:
        state.db.execute("UPDATE reports SET metadata_json=?", (raw,))
    assert state.requeue_page_threshold(10, today=TODAY) == 0
    state.close()


@pytest.mark.parametrize("kwargs", [
    {"min_pages": 0}, {"min_pages": -1}, {"min_pages": True},
    {"min_pages": 10, "lookback_days": 0},
    {"min_pages": 10, "lookback_days": True},
    {"min_pages": 10, "today": "2026-10-27"},
])
def test_invalid_policy_does_not_mutate_state(tmp_path, kwargs):
    state = State(tmp_path / "state.sqlite3")
    before = state.db.execute("SELECT * FROM state_meta").fetchall()
    with pytest.raises(ValueError):
        state.requeue_page_threshold(**kwargs)
    assert state.db.execute("SELECT * FROM state_meta").fetchall() == before
    state.close()

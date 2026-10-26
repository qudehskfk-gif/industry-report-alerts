from dataclasses import replace
from datetime import date
import json
import sqlite3

import pytest

from reportbot.models import Report
from reportbot.state import State, title_fingerprint


def report(**changes):
    item = Report(
        source="naver", source_id="101", title="반도체 산업의 구조적 전환",
        broker="미래증권", published_date=date(2026, 10, 23),
        detail_url="https://example.com/industry/101",
        pdf_url="https://example.com/industry/101.pdf",
    )
    return replace(item, **changes)


def legacy_database(path, rows, delivered):
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE sources(name TEXT PRIMARY KEY);
            CREATE TABLE reports(
                source TEXT NOT NULL, source_id TEXT NOT NULL,
                title TEXT NOT NULL, outcome TEXT NOT NULL,
                processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(source,source_id)
            );
            CREATE TABLE delivered(kind TEXT NOT NULL,fingerprint TEXT NOT NULL,
                PRIMARY KEY(kind,fingerprint));
        """)
        db.executemany("INSERT INTO reports(source,source_id,title,outcome) VALUES (?,?,?,?)", rows)
        db.executemany("INSERT INTO delivered VALUES (?,?)", delivered)


def test_pending_retry_survives_disappearing_listing_and_process_restart(tmp_path):
    path = tmp_path / "state.sqlite3"
    # Old publication dates must not discard newly discovered uploads.
    item = report(published_date=date(2024, 1, 5))
    resolved = replace(item, pdf_url="https://example.com/resolved.pdf")
    state = State(path)
    state.ingest("naver", [item])
    state.record(resolved, "pending: upload timeout")
    state.close()

    state = State(path)
    try:
        state.ingest("naver", [])
        state.ingest("hankyung", [])
        assert state.pending() == [resolved]
        assert state.outcome(resolved) == "pending: upload timeout"
        assert not state.duplicate(resolved)
    finally:
        state.close()


def test_legacy_baseline_is_not_delivery_and_actual_pdf_delivery_is_preserved(tmp_path):
    path = tmp_path / "state.sqlite3"
    baseline = report()
    sent = report(source_id="102", title="실제로 발송한 산업 보고서")
    legacy_database(path, [
        (baseline.source, baseline.source_id, baseline.title, "baseline"),
        (sent.source, sent.source_id, sent.title, "sent"),
    ], [
        ("title", title_fingerprint(baseline)),
        ("title", title_fingerprint(sent)),
        ("pdf", "actually-sent-pdf"),
    ])

    state = State(path)
    try:
        assert not state.duplicate(baseline)
        assert state.duplicate(report(source_id="other"), "actually-sent-pdf")
        state.ingest("naver", [baseline, sent])
        assert state.pending() == [baseline]
        assert state.outcome(sent) == "sent"
        assert state.duplicate(sent)
    finally:
        state.close()


def test_delivery_migration_runs_once_and_reopen_keeps_real_title_keys(tmp_path):
    path = tmp_path / "state.sqlite3"
    item = report()
    state = State(path)
    state.record(item, "sent", "pdf-hash")
    state.close()

    state = State(path)
    try:
        assert state.duplicate(item)
        assert state.duplicate(report(title="다른 제목"), "pdf-hash")
        assert state.pending() == []
        assert state.outcome(item) == "sent"
    finally:
        state.close()


def test_ingest_requeues_baseline_and_old_age_skip_but_retains_true_outcomes(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    baseline = report(source_id="101")
    age_skipped = report(source_id="102", title="늦게 등록된 옛 발간일 자료")
    periodic = report(source_id="103", title="주간 동향")
    sent = report(source_id="104", title="이미 보낸 자료")
    duplicate = report(source_id="105", title="같은 PDF의 중복 게시")
    try:
        state.baseline("naver", [baseline])
        state.record(age_skipped, "skipped: 오래된 자료")
        state.record(periodic, "skipped: 정기 동향·실적 점검 자료")
        state.record(sent, "sent", "already-delivered")
        state.record(duplicate, "duplicate")
        state.ingest("naver", [baseline, age_skipped, periodic, sent, duplicate])
        assert {item.source_id for item in state.pending()} == {"101", "102"}
        assert state.outcome(periodic).startswith("skipped:")
        assert state.outcome(sent) == "sent"
        assert state.outcome(duplicate) == "duplicate"
        assert not state.duplicate(baseline)
    finally:
        state.close()


def test_initial_wider_scan_keeps_existing_backlog_without_flooding_old_history(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    known = report(source_id="101")
    old_catalogue = report(source_id="70", title="기존 시작 범위 밖의 역사 자료")
    new_backdated = report(source_id="102", title="추가 등록된 자료", published_date=date(2020, 1, 1))
    try:
        state.baseline("naver", [known])
        floor = state.highwater("naver")
        assert floor == 101
        state.ingest("naver", [old_catalogue, known, new_backdated], initial_id_floor=floor)
        assert {item.source_id for item in state.pending()} == {"101", "102"}
        assert state.outcome(old_catalogue) == "historical_pre_start"
        # The cutover floor is a one-time discovery boundary, not a publication-date filter.
        later = report(source_id="103", title="오늘 업로드된 오래된 발간일", published_date=date(2019, 1, 1))
        state.ingest("naver", [later])
        assert later in state.pending()
    finally:
        state.close()


def test_only_actual_sent_records_suppress_cross_source_duplicates(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    item = report()
    mirror = replace(item, source="hankyung", source_id="900")
    try:
        state.baseline("naver", [item])
        assert not state.duplicate(mirror)
        state.ingest("naver", [item])
        state.record(item, "pending")
        assert not state.duplicate(mirror)
        state.record(item, "sent", "same-pdf")
        assert state.duplicate(mirror)
        assert state.duplicate(replace(mirror, title="다른 제목"), "same-pdf")
    finally:
        state.close()


def test_persisted_metadata_contains_public_report_fields_only(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    item = report(periodic=True, explicit_in_depth=True)
    try:
        state.ingest("naver", [item])
        raw = state.db.execute("SELECT metadata_json FROM reports").fetchone()[0]
        data = json.loads(raw)
        assert set(data) == {
            "source", "source_id", "title", "broker", "published_date", "detail_url",
            "pdf_url", "category", "explicit_in_depth", "periodic", "sector",
        }
        assert data["published_date"] == "2026-10-23"
        assert state.pending() == [item]
    finally:
        state.close()


def test_source_cutover_settings_and_numeric_highwater(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    try:
        assert state.highwater("naver") is None
        assert state.get_setting("daily_backlog_started") is None
        assert state.get_setting("unknown", "default") == "default"
        state.ingest("naver", [report(source_id="9"), report(source_id="101"), report(source_id="url-9999")])
        assert state.highwater("naver") == 101
        state.set_setting("daily_backlog_started", "1")
        assert state.get_setting("daily_backlog_started") == "1"
    finally:
        state.close()


def test_mismatched_source_is_rejected_before_mutation(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    try:
        with pytest.raises(ValueError):
            state.ingest("hankyung", [report()])
        assert state.pending() == []
        assert not state.initialized("hankyung")
        assert not state.initialized("naver")
    finally:
        state.close()

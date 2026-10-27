from dataclasses import replace
from datetime import date
import json
import sqlite3

import pytest

from reportbot.models import Report
from reportbot.state import State, summary_destination_key


TOKEN = "123:private-bot-credential"
CHAT = "-100987654321"
DEST = summary_destination_key(TOKEN, CHAT)
OTHER_DEST = summary_destination_key(TOKEN, "-100123456789")
NOW = "2030-01-01 10:00:00"


def report(**changes):
    return replace(Report("naver", "101", "산업 전반의 구조적 변화", "테스트증권",
                          date(2026, 10, 27), "https://example.com/101",
                          "https://example.com/101.pdf", sector="반도체"), **changes)


def receipt(**changes):
    return {"message_id": 1234, "destination_key": DEST,
            "error_code": "http_503", **changes}


def delivery_snapshot(state):
    return (state.db.execute("SELECT * FROM reports ORDER BY source,source_id").fetchall(),
            state.db.execute("SELECT * FROM delivered ORDER BY kind,fingerprint").fetchall())


def test_queue_survives_process_restart_and_keeps_original_pdf_delivered(tmp_path):
    path = tmp_path / "state.sqlite3"
    item = report()
    state = State(path)
    state.record(item, "sent", "original-pdf-hash", summary_job=receipt())
    before = delivery_snapshot(state)
    state.close()
    state = State(path)
    try:
        [job] = state.pending_summaries(DEST, now=NOW)
        assert job["report"] == item
        assert job["message_id"] == 1234 and job["attempts"] == 0
        assert job["pdf_hash"] == "original-pdf-hash"
        assert job["error_code"] == "http_503"
        assert state.outcome(item) == "sent" and state.duplicate(item, "original-pdf-hash")
        assert state.pending() == [] and delivery_snapshot(state) == before
    finally:
        state.close()


def test_pending_jobs_are_isolated_by_recipient_and_due_time(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    try:
        state.record(report(), "sent", summary_job=receipt())
        assert state.pending_summaries(OTHER_DEST, now=NOW) == []
        state.defer_summary("naver", "101", DEST, "http_503", now=NOW)
        assert state.pending_summaries(DEST, now="2030-01-01 10:59:59") == []
        assert state.pending_summaries(DEST, now="2030-01-01 11:00:00")[0]["attempts"] == 1
    finally:
        state.close()


def test_retry_exhaustion_is_bounded_and_does_not_change_pdf_history(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    try:
        state.record(report(), "sent", "hash", summary_job=receipt())
        before = delivery_snapshot(state)
        assert state.defer_summary("naver", "101", DEST, "http_503", now=NOW) == "pending"
        assert state.defer_summary("naver", "101", DEST, "quota_429", now=NOW) == "pending"
        assert state.defer_summary("naver", "101", DEST, "transport_error", now=NOW) == "failed"
        assert state.pending_summaries(DEST, now="2040-01-01") == []
        assert state.defer_summary("naver", "101", DEST, "http_503", now=NOW) == "failed"
        assert state.db.execute("SELECT attempts FROM summary_jobs").fetchone() == (3,)
        assert delivery_snapshot(state) == before
        state.ingest("naver", [report(pdf_url=None)])
        assert state.db.execute("SELECT state,attempts,message_id FROM summary_jobs").fetchone() == ("failed", 3, 1234)
    finally:
        state.close()


def test_completion_preserves_original_delivery_and_never_reopens(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    try:
        state.record(report(), "sent", "hash", summary_job=receipt())
        before = delivery_snapshot(state)
        assert state.complete_summary("naver", "101", DEST)
        assert not state.complete_summary("naver", "101", DEST)
        assert state.pending_summaries(DEST, now=NOW) == []
        assert delivery_snapshot(state) == before
        state.ingest("naver", [report()])
        state.record(report(), "sent", "hash", summary_job=receipt(message_id=9999))
        assert state.db.execute("SELECT state,message_id FROM summary_jobs").fetchone() == ("completed", 1234)
    finally:
        state.close()


def test_pdf_delivery_and_summary_receipt_are_atomic(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    try:
        state.db.execute("""CREATE TRIGGER block_summary BEFORE INSERT ON summary_jobs
                           BEGIN SELECT RAISE(ABORT,'queue unavailable'); END""")
        with pytest.raises(sqlite3.IntegrityError):
            state.record(report(), "sent", "hash", summary_job=receipt())
        assert state.outcome(report()) is None
        assert not state.duplicate(report(), "hash")
        assert state.db.execute("SELECT COUNT(*) FROM summary_jobs").fetchone() == (0,)
    finally:
        state.close()


@pytest.mark.parametrize("changes", [
    {"message_id": 0}, {"message_id": -1}, {"message_id": True},
    {"message_id": "123"}, {"destination_key": CHAT},
    {"destination_key": "x" * 24}, {"destination_key": "a" * 25},
])
def test_invalid_receipts_do_not_mark_pdf_as_delivered(tmp_path, changes):
    state = State(tmp_path / "state.sqlite3")
    try:
        with pytest.raises(ValueError):
            state.record(report(), "sent", "hash", summary_job=receipt(**changes))
        assert state.outcome(report()) is None
    finally:
        state.close()


def test_unsent_reports_cannot_get_caption_recovery_jobs(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    try:
        with pytest.raises(ValueError):
            state.record(report(), "pending", summary_job=receipt())
        assert state.pending_summaries(DEST, now=NOW) == []
    finally:
        state.close()


def test_state_never_stores_tokens_chat_ids_or_unsanitized_error_text(tmp_path):
    path = tmp_path / "state.sqlite3"
    state = State(path)
    try:
        state.record(report(), "sent", summary_job=receipt(error_code=TOKEN,
                     summary_text="private generated summary must not be persisted"))
        state.defer_summary("naver", "101", DEST, CHAT, now=NOW)
        assert state.db.execute("SELECT last_error_code FROM summary_jobs").fetchone() == ("summary_error",)
        dump = "\n".join(state.db.iterdump())
        for private in [TOKEN, CHAT, "private generated summary must not be persisted"]:
            assert private not in dump
        assert DEST in dump
    finally:
        state.close()


def test_destination_hmac_is_stable_and_not_a_public_chat_id():
    assert DEST == summary_destination_key(TOKEN, CHAT)
    assert len(DEST) == 24 and DEST != OTHER_DEST
    assert DEST != summary_destination_key("different-token", CHAT)
    assert CHAT not in DEST and TOKEN not in DEST


def test_existing_database_migrates_without_resetting_delivery_state(tmp_path):
    path = tmp_path / "state.sqlite3"
    item = report()
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE sources(name TEXT PRIMARY KEY);
            CREATE TABLE reports(source TEXT NOT NULL,source_id TEXT NOT NULL,
                title TEXT NOT NULL,outcome TEXT NOT NULL,
                processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                metadata_json TEXT,pdf_hash TEXT,PRIMARY KEY(source,source_id));
            CREATE TABLE delivered(kind TEXT NOT NULL,fingerprint TEXT NOT NULL,
                PRIMARY KEY(kind,fingerprint));
            CREATE TABLE state_meta(name TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT INTO state_meta VALUES('delivery_only_v2','1');
            INSERT INTO state_meta VALUES('existing-setting','keep');
        """)
        db.execute("INSERT INTO reports VALUES(?,?,?,?,?,?,?)", (
            item.source,item.source_id,item.title,"sent","2026-10-27 09:24:51",State._encode(item),"hash"))
        db.execute("INSERT INTO delivered VALUES('pdf','hash')")
    state = State(path)
    try:
        assert state.outcome(item) == "sent"
        assert state.db.execute("SELECT processed_at FROM reports").fetchone() == ("2026-10-27 09:24:51",)
        assert state.get_setting("existing-setting") == "keep"
        assert state.duplicate(item, "hash")
        assert state.pending_summaries(DEST, now=NOW) == []
    finally:
        state.close()


def test_retry_now_accepts_timezone_aware_iso_values(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    try:
        state.record(report(), "sent", summary_job=receipt())
        state.defer_summary("naver", "101", DEST, "http_503", now="2030-01-01T19:00:00+09:00")
        assert state.pending_summaries(DEST, now="2030-01-01T10:59:59Z") == []
        assert len(state.pending_summaries(DEST, now="2030-01-01T11:00:00Z")) == 1
    finally:
        state.close()

from dataclasses import replace
from datetime import date, timedelta

import pytest

from reportbot.models import Report
from reportbot.state import State


TODAY = date(2026, 10, 28)
FIRST_HASH = "a" * 64
SECOND_HASH = "b" * 64


def report(source_id="100", **changes):
    item = Report(
        "naver", source_id, "산업 전망", "테스트증권", TODAY,
        f"https://example.com/{source_id}", f"https://example.com/{source_id}.pdf",
    )
    return replace(item, **changes)


def test_same_title_with_a_different_pdf_is_not_proof_of_pdf_delivery(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    sent, another = report("sent"), report("another")
    state.record(sent, "sent", FIRST_HASH)
    assert state.duplicate(another)  # Keep the legacy API compatible.
    assert state.duplicate_pdf_match(SECOND_HASH) is None
    state.close()


def test_identical_pdf_under_a_different_title_has_original_delivery_evidence(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    sent = report("original", source="hankyung", title="등록 사이트의 다른 제목")
    state.record(sent, "sent", FIRST_HASH)
    with state.db:
        state.db.execute("UPDATE reports SET processed_at='2026-10-28 03:21:12'")
    match = state.duplicate_pdf_match(FIRST_HASH)
    assert match == {"source": "hankyung", "source_id": "original",
                     "title": sent.title, "processed_at": "2026-10-28 03:21:12",
                     "pdf_hash": FIRST_HASH}
    state.close()


def test_pdf_receipt_does_not_require_historical_metadata_json(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    state.record(report(), "sent", FIRST_HASH)
    with state.db:
        state.db.execute("UPDATE reports SET metadata_json=NULL")
    assert state.duplicate_pdf_match(FIRST_HASH)["source_id"] == "100"
    state.close()


def test_sent_pdf_receipt_remains_authoritative_without_a_delivery_index(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    state.record(report(), "sent", FIRST_HASH)
    with state.db:
        state.db.execute("DELETE FROM delivered")
    assert state.duplicate_pdf_match(FIRST_HASH)["source_id"] == "100"
    state.close()


def test_only_actual_sent_rows_provide_a_named_receipt(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    for source_id, outcome in [("rejected", "rejected: 최소 10쪽 미만·8쪽"),
                               ("duplicate", "duplicate"), ("error", "error_notified")]:
        state.record(report(source_id), outcome, FIRST_HASH)
    assert state.duplicate_pdf_match(FIRST_HASH) is None
    state.close()


def test_legacy_pdf_only_fingerprint_does_not_fabricate_a_delivery_receipt(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    with state.db:
        state.db.execute("INSERT INTO delivered VALUES ('pdf', ?)", (FIRST_HASH,))
    assert state.duplicate_pdf_match(FIRST_HASH) == {
        "source": None, "source_id": None, "title": None,
        "processed_at": None, "pdf_hash": FIRST_HASH,
    }
    state.close()


@pytest.mark.parametrize("digest", [None, "", "not-a-hash", "A" * 64, "a" * 63,
                                         "a" * 65, 42, True])
def test_invalid_pdf_hash_is_never_delivery_evidence(tmp_path, digest):
    state = State(tmp_path / "state.sqlite3")
    assert state.duplicate_pdf_match(digest) is None
    state.close()


def test_requeue_repairs_title_only_skip_but_preserves_known_identical_pdf(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    wrong_listing = report("wrong", source="hankyung", title="조선에서 해운으로 바통터치")
    infra_listing = report("infra", title="인프라를 가진 지주")
    actual_shipping = report("shipping", title=wrong_listing.title)
    state.record(wrong_listing, "sent", FIRST_HASH)
    state.record(infra_listing, "duplicate", FIRST_HASH)
    state.record(actual_shipping, "duplicate")
    assert state.requeue_unverified_duplicates(today=TODAY) == 1
    assert state.pending() == [actual_shipping]
    assert state.outcome(infra_listing) == "duplicate"
    assert state.outcome(wrong_listing) == "sent"
    assert state.requeue_unverified_duplicates(today=TODAY) == 0
    state.close()


def test_duplicate_with_unconfirmed_pdf_hash_is_reconsidered(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    candidate = report()
    state.record(candidate, "duplicate", SECOND_HASH)
    assert state.requeue_unverified_duplicates(today=TODAY) == 1
    assert state.pending() == [candidate]
    state.close()


def test_legacy_pdf_match_remains_duplicate_without_a_named_receipt(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    candidate = report()
    state.record(candidate, "duplicate", FIRST_HASH)
    with state.db:
        state.db.execute("INSERT INTO delivered VALUES ('pdf', ?)", (FIRST_HASH,))
    assert state.requeue_unverified_duplicates(today=TODAY) == 0
    assert state.outcome(candidate) == "duplicate"
    state.close()


def test_requeue_is_limited_to_valid_recent_industry_metadata(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    candidates = [report("boundary", published_date=TODAY - timedelta(days=45)),
                  report("old", published_date=TODAY - timedelta(days=46)),
                  report("future", published_date=TODAY + timedelta(days=1)),
                  report("company", category="company")]
    for item in candidates:
        state.record(item, "duplicate")
    assert state.requeue_unverified_duplicates(today=TODAY) == 1
    assert state.pending() == candidates[:1]
    state.close()


@pytest.mark.parametrize("outcome", ["sent", "error_notified", "pending: error notice",
                                     "historical_pre_start", "rejected: 정기 자료", "pending"])
def test_nonduplicate_outcomes_are_untouched(tmp_path, outcome):
    state = State(tmp_path / "state.sqlite3")
    state.record(report(), outcome)
    before = state.db.execute("SELECT * FROM reports").fetchall()
    assert state.requeue_unverified_duplicates(today=TODAY) == 0
    assert state.db.execute("SELECT * FROM reports").fetchall() == before
    state.close()


def test_requeue_preserves_timestamps_metadata_and_other_delivery_ledgers(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    state.record(report("candidate"), "duplicate")
    state.record(report("sent"), "sent", FIRST_HASH, summary_job={
        "message_id": 10, "destination_key": "c" * 24, "error_code": "http_503",
    })
    with state.db:
        state.db.execute("UPDATE reports SET processed_at='2026-10-27 01:02:03'")
    candidate_before = state.db.execute("""
        SELECT title,processed_at,metadata_json,pdf_hash FROM reports WHERE source_id='candidate'
    """).fetchone()
    other_before = {table: state.db.execute(f"SELECT * FROM {table}").fetchall()
                    for table in ["delivered", "summary_jobs", "sources", "state_meta"]}
    sent_before = state.db.execute("SELECT * FROM reports WHERE source_id='sent'").fetchone()
    assert state.requeue_unverified_duplicates(today=TODAY) == 1
    assert state.db.execute("""
        SELECT title,processed_at,metadata_json,pdf_hash FROM reports WHERE source_id='candidate'
    """).fetchone() == candidate_before
    assert state.db.execute("SELECT * FROM reports WHERE source_id='sent'").fetchone() == sent_before
    assert all(state.db.execute(f"SELECT * FROM {table}").fetchall() == rows
               for table, rows in other_before.items())
    state.close()


def test_later_metadata_hydration_can_reopen_an_unverified_duplicate(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    candidate = report()
    state.record(candidate, "duplicate")
    with state.db:
        state.db.execute("UPDATE reports SET metadata_json=NULL")
    assert state.requeue_unverified_duplicates(today=TODAY) == 0
    state.ingest(candidate.source, [candidate])
    assert state.requeue_unverified_duplicates(today=TODAY) == 1
    assert state.pending() == [candidate]
    state.close()


@pytest.mark.parametrize("raw", ["not-json", "{}", '"not-an-object"'])
def test_malformed_legacy_metadata_is_ignored(tmp_path, raw):
    state = State(tmp_path / "state.sqlite3")
    state.record(report(), "duplicate")
    with state.db:
        state.db.execute("UPDATE reports SET metadata_json=?", (raw,))
    assert state.requeue_unverified_duplicates(today=TODAY) == 0
    state.close()


def test_mismatched_metadata_identity_is_not_reopened(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    state.record(report(), "duplicate")
    with state.db:
        state.db.execute("UPDATE reports SET metadata_json=?", (State._encode(report("other")),))
    assert state.requeue_unverified_duplicates(today=TODAY) == 0
    state.close()


@pytest.mark.parametrize("kwargs", [{"lookback_days": 0}, {"lookback_days": True},
                                   {"today": "2026-10-28"}])
def test_invalid_requeue_policy_does_not_mutate_rows(tmp_path, kwargs):
    state = State(tmp_path / "state.sqlite3")
    state.record(report(), "duplicate")
    before = state.db.execute("SELECT * FROM reports").fetchall()
    with pytest.raises(ValueError):
        state.requeue_unverified_duplicates(**kwargs)
    assert state.db.execute("SELECT * FROM reports").fetchall() == before
    state.close()

from dataclasses import replace
from datetime import date
import json

from reportbot.models import Report
from reportbot.state import State


def report():
    return Report(
        source="naver", source_id="101", title="반도체 산업의 구조적 전환",
        broker="미래증권", published_date=date(2026, 10, 23),
        detail_url="https://example.com/industry/101",
    )


def test_sector_persists_when_pending_report_outlives_source_listing(tmp_path):
    path = tmp_path / "state.sqlite3"
    item = replace(report(), sector="반도체")
    state = State(path)
    state.ingest("naver", [item])
    state.close()

    state = State(path)
    try:
        state.ingest("naver", [])
        assert state.pending() == [item]
        assert state.pending()[0].sector == "반도체"
    finally:
        state.close()


def test_old_pending_metadata_without_sector_remains_readable(tmp_path):
    path = tmp_path / "state.sqlite3"
    item = report()
    state = State(path)
    state.ingest("naver", [item])
    data = json.loads(state.db.execute("SELECT metadata_json FROM reports").fetchone()[0])
    data.pop("sector", None)
    with state.db:
        state.db.execute("UPDATE reports SET metadata_json=?", (json.dumps(data),))
    state.close()

    state = State(path)
    try:
        assert state.pending() == [item]
        assert state.pending()[0].sector is None
        state.ingest("naver", [replace(item, sector="반도체")])
        assert state.pending()[0].sector == "반도체"
    finally:
        state.close()


def test_sector_enrichment_keeps_actual_delivery_keys_and_outcome(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    original = report()
    enriched = replace(original, sector="반도체")
    try:
        state.record(original, "sent", "delivered-pdf-hash")
        delivered_before = state.db.execute("SELECT * FROM delivered ORDER BY kind").fetchall()
        state.ingest("naver", [enriched])
        assert state.outcome(enriched) == "sent"
        assert state.pending() == []
        assert state.duplicate(enriched)
        assert state.duplicate(replace(enriched, title="다른 제목"), "delivered-pdf-hash")
        assert state.db.execute("SELECT * FROM delivered ORDER BY kind").fetchall() == delivered_before
        stored = json.loads(state.db.execute("SELECT metadata_json FROM reports").fetchone()[0])
        assert stored["sector"] == "반도체"
    finally:
        state.close()

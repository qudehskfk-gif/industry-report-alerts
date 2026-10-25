from dataclasses import replace
from datetime import date

from reportbot.http import make_session
from reportbot.models import Report
from reportbot.state import State


def test_ingestion_preserves_resolved_url_but_revalidates_changed_metadata(tmp_path):
    paper = Report("naver", "46592", "산업 구조 변화", "신한투자증권", date(2026, 10, 22), "https://example.com/research")
    resolved = replace(paper, pdf_url="https://bbs2.shinhansec.com/board/message/file.pdf.do?attachmentId=354429")
    state = State(tmp_path / "state.sqlite3")
    try:
        state.record(resolved, "pending")
        state.ingest("naver", [paper])
        assert state.pending() == [resolved]
        changed = replace(paper, title="수정된 산업 분석")
        state.ingest("naver", [changed])
        assert state.pending() == [changed]
    finally:
        state.close()


def test_public_publisher_channel_never_automatically_retries_rate_limit():
    with make_session() as session:
        assert session.get_adapter("https://t.me/s/shinhanresearch").max_retries.total == 0
        assert session.get_adapter("https://stock.naver.com/api/").max_retries.total == 3

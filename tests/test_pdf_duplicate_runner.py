"""A listing title cannot prove that the same PDF reached Telegram."""

import hashlib
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from reportbot.classify import Decision
from reportbot.config import Config
from reportbot.models import Report
from reportbot.state import State


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def report(source, source_id, title):
    return Report(
        source=source, source_id=source_id, title=title, broker="대신증권",
        published_date=datetime.now(ZoneInfo("Asia/Seoul")).date(),
        detail_url=f"https://example.test/{source}/{source_id}",
        pdf_url=f"https://example.test/{source}/{source_id}.pdf",
    )


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    from reportbot import runner

    config = Config(tmp_path / "state.sqlite3", "dummy-token", "dummy-chat", 10, 100, 86400, 14, 45)
    sent, notices, footers, inspected = [], [], [], []

    class Telegram:
        def __init__(self, *args):
            pass

        def send_report(self, item, decision):
            sent.append((item, decision))

        def send_report_error(self, item, reason):
            notices.append((item, reason))

        def send_run_summary(self, data):
            footers.append(data)

    monkeypatch.setattr(runner, "Telegram", Telegram)
    monkeypatch.setattr(runner, "make_session", lambda: nullcontext(object()))
    monkeypatch.setattr(runner, "_summary_recovery_enabled", lambda: False)

    def setup(items, hashes):
        monkeypatch.setattr(runner, "SOURCES", {
            source: (lambda *args, source=source, **kwargs:
                     [item for item in items if item.source == source])
            for source in {item.source for item in items}
        })

        def classify(item, session, min_pages):
            inspected.append(item)
            return Decision(True, "산업 리포트 15쪽", 15, hashes[(item.source, item.source_id)])

        monkeypatch.setattr(runner, "classify", classify)

    return runner, config, setup, sent, notices, footers, inspected


def test_same_title_different_actual_pdfs_both_send(fixture):
    runner, config, setup, sent, notices, footers, inspected = fixture
    wrong_attachment = report("hankyung", "653362", "조선에서해운으로바통터치")
    correct_attachment = report("naver", "46691", "조선에서 해운으로 바통터치")
    setup([wrong_attachment, correct_attachment], {
        ("hankyung", "653362"): digest("actual holding company PDF"),
        ("naver", "46691"): digest("actual shipping PDF"),
    })

    assert runner.check(config) == 0
    assert [item for item, decision in sent] == [wrong_attachment, correct_attachment]
    assert inspected == [wrong_attachment, correct_attachment]
    assert notices == []
    assert footers[-1]["totals"]["sent"] == 2
    assert footers[-1]["totals"]["duplicates"] == 0


def test_different_titles_same_pdf_send_once_with_matching_delivery_details(fixture):
    runner, config, setup, sent, notices, footers, inspected = fixture
    original = report("hankyung", "653362", "조선에서해운으로바통터치")
    mirror = report("naver", "46690", "인프라를 가진 지주, 데이터를 가진 지주")
    same_pdf = digest("same holding company PDF")
    setup([original, mirror], {(item.source, item.source_id): same_pdf for item in [original, mirror]})

    assert runner.check(config) == 0
    assert [item for item, decision in sent] == [original]
    assert inspected == [original, mirror]
    assert notices == []
    duplicate, reason = footers[-1]["duplicates"][0]
    assert duplicate == mirror
    assert "파일 내용이 완전히 같습니다" in reason
    assert original.title in reason
    assert "발송 시각:" in reason
    assert "(한국시간)" in reason


def test_previously_skipped_title_only_duplicate_is_rechecked_and_sent(fixture):
    runner, config, setup, sent, notices, footers, inspected = fixture
    wrong = report("hankyung", "653362", "조선에서해운으로바통터치")
    actual = report("naver", "46691", "조선에서 해운으로 바통터치")
    wrong_hash, actual_hash = digest("holdings"), digest("shipping")
    state = State(config.state_path)
    state.record(wrong, "sent", wrong_hash)
    state.record(actual, "duplicate")
    state.close()
    setup([wrong, actual], {("hankyung", "653362"): wrong_hash, ("naver", "46691"): actual_hash})

    assert runner.check(config) == 0
    assert [item for item, decision in sent] == [actual]
    assert inspected == [actual]
    assert runner.check(config) == 0
    assert [item for item, decision in sent] == [actual]
    state = State(config.state_path)
    assert state.outcome(actual) == "sent"
    assert state.duplicate_pdf_match(actual_hash)["source_id"] == "46691"
    state.close()


@pytest.mark.parametrize("preview_argument", [{"preview_only": True}, {"dry_run": True}])
def test_preview_keeps_different_pdfs_even_when_listing_titles_match(fixture, caplog, preview_argument):
    runner, config, setup, sent, notices, footers, inspected = fixture
    first = report("hankyung", "100", "같은 산업 보고서 제목")
    second = report("naver", "200", "같은 산업 보고서 제목")
    setup([first, second], {("hankyung", "100"): digest("first"), ("naver", "200"): digest("second")})

    with caplog.at_level("INFO"):
        assert runner.check(config, **preview_argument) == 0
    assert len(inspected) == 2
    assert len([r for r in caplog.records if "[발송 대기 PDF]" in r.message]) == 2
    assert sent == notices == footers == []


def test_preview_identical_pdf_under_different_titles_is_one_candidate(fixture, caplog):
    runner, config, setup, sent, notices, footers, inspected = fixture
    first = report("hankyung", "100", "첫 번째 제목")
    second = report("naver", "200", "두 번째 제목")
    setup([first, second], {(item.source, item.source_id): digest("same") for item in [first, second]})

    with caplog.at_level("INFO"):
        assert runner.check(config, dry_run=True) == 0
    assert len(inspected) == 2
    assert len([r for r in caplog.records if "[발송 대기 PDF]" in r.message]) == 1
    assert sent == notices == footers == []


def test_duplicate_reason_converts_saved_utc_to_korean_time():
    from reportbot.runner import _duplicate_pdf_reason

    reason = _duplicate_pdf_reason({"title": "다른 발송 제목", "processed_at": "2026-10-28 03:21:12"})
    assert "다른 발송 제목" in reason
    assert "2026-10-28 12:21 (한국시간)" in reason


@pytest.mark.parametrize("evidence", [
    {"title": None, "processed_at": None},
    {"title": None, "processed_at": "not a timestamp"},
])
def test_duplicate_reason_does_not_invent_legacy_title_or_time(evidence):
    from reportbot.runner import _duplicate_pdf_reason

    reason = _duplicate_pdf_reason(evidence)
    assert "파일 내용이 완전히 같습니다" in reason
    assert "발송 제목:" not in reason
    assert "발송 시각:" not in reason

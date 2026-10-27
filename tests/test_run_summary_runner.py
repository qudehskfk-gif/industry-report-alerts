from contextlib import nullcontext
from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pytest
import requests

from reportbot.classify import Decision
from reportbot.config import Config
from reportbot.models import Report
from reportbot.sources.shinhan import SingleCompanyReport
from reportbot.state import State


def report(source_id="100", source="hankyung", **changes):
    return replace(Report(
        source, source_id, f"산업 구조의 변화 {source_id}", "테스트증권", date.today(),
        f"https://example.com/{source}/{source_id}",
        f"https://example.com/{source}/{source_id}.pdf",
    ), **changes)


@pytest.fixture
def run_report(tmp_path, monkeypatch):
    from reportbot import runner

    config = Config(tmp_path / "state.sqlite3", "123:fake-token", "-12345", 10, 100, 86400, 14, 45)
    events = SimpleNamespace(sent=[], notices=[], summaries=[])
    controls = SimpleNamespace(footer_error=None)

    class Telegram:
        def __init__(self, token, chat_id):
            assert (token, chat_id) == (config.token, config.chat_id)

        def send_report(self, item, decision):
            events.sent.append(item)
            return {"message_id": len(events.sent), "_reportbot_summary_status": "complete"}

        def send_report_error(self, item, reason):
            events.notices.append((item, reason))

        def send_run_summary(self, data):
            events.summaries.append(data)
            if controls.footer_error:
                raise controls.footer_error

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(runner, "Telegram", Telegram)
    monkeypatch.setattr(runner, "make_session", lambda: nullcontext(object()))
    monkeypatch.setattr(runner, "classify", lambda item, *args: Decision(
        True, "산업 리포트 15쪽 (분량 기준)", 15, f"pdf-{item.source}-{item.source_id}",
    ))
    return runner, config, events, controls


def test_listing_count_is_distinct_from_newly_reviewed_reports(run_report, monkeypatch):
    runner, config, events, _ = run_report
    hankyung = [report(str(1000 + i)) for i in range(281)]
    naver = [report(str(2000 + i), "naver") for i in range(660)]
    state = State(config.state_path)
    state.baseline("hankyung", hankyung[:-1])
    state.baseline("naver", naver[:-2])
    with state.db:
        state.db.execute("UPDATE reports SET outcome='historical_pre_start'")
    state.close()
    monkeypatch.setattr(runner, "SOURCES", {
        "hankyung": lambda *args, **kw: hankyung,
        "naver": lambda *args, **kw: naver,
    })

    def classify(item, *args):
        if item == naver[-1]:
            return Decision(False, "최소 10쪽 미만·9쪽", 9, "short-pdf")
        return Decision(True, "산업 리포트 15쪽", 15, f"pdf-{item.source_id}")

    monkeypatch.setattr(runner, "classify", classify)
    assert runner.check(config) == 0
    assert len(events.summaries) == 1
    summary = events.summaries[0]
    assert summary["source_counts"] == {"hankyung": 281, "naver": 660}
    assert sum(summary["source_counts"].values()) == 941
    assert summary["reviewed"] == 3
    assert summary["totals"]["sent"] == 2
    assert summary["totals"]["rejected"] == 1
    assert summary["rejected"] == [(naver[-1], "최소 10쪽 미만·9쪽")]
    assert summary["lookback_days"] == 45
    assert summary["started_at"].utcoffset().total_seconds() == 9 * 3600


def test_all_rejection_paths_are_in_the_summary(run_report, monkeypatch):
    runner, config, events, _ = run_report
    weekly = report("100", title="산업 Weekly")
    short = report("101", title="짧은 산업 전망")
    company = report("102", source="naver", title="발행사 분류 확인 필요")
    items = [weekly, short, company]
    monkeypatch.setattr(runner, "SOURCES", {
        "hankyung": lambda *args, **kw: items[:2],
        "naver": lambda *args, **kw: [company],
    })

    def classify(item, *args):
        if item == company:
            raise SingleCompanyReport("private-publisher-response")
        return Decision(False, "최소 10쪽 미만·8쪽", 8, "short")

    monkeypatch.setattr(runner, "classify", classify)
    assert runner.check(config) == 0
    summary = events.summaries[0]
    assert summary["reviewed"] == 3
    assert summary["totals"]["sent"] == 0
    assert summary["totals"]["rejected"] == 3
    assert dict((item.source_id, reason) for item, reason in summary["rejected"]) == {
        "100": "정기 동향·실적 점검 자료",
        "101": "최소 10쪽 미만·8쪽",
        "102": "발행사에서 확인된 개별 기업 리포트",
    }
    assert events.sent == []
    assert events.notices == []


@pytest.mark.parametrize("duplicate_kind", ["title", "pdf"])
def test_duplicates_are_reported_separately_from_condition_rejections(run_report, monkeypatch, duplicate_kind):
    runner, config, events, _ = run_report
    first = report("100", title="같은 산업 보고서")
    mirror = report("101", title=first.title if duplicate_kind == "title" else "다른 제목의 같은 PDF")
    state = State(config.state_path)
    state.record(first, "sent", "shared-pdf")
    state.close()
    monkeypatch.setattr(runner, "SOURCES", {"hankyung": lambda *args, **kw: [first, mirror]})
    monkeypatch.setattr(runner, "classify", lambda *args: Decision(True, "산업 리포트 15쪽", 15, "shared-pdf"))
    assert runner.check(config) == 0
    summary = events.summaries[0]
    assert summary["reviewed"] == 1
    assert summary["totals"]["duplicates"] == 1
    assert summary["duplicates"] == [(mirror, "이미 발송한 제목 또는 동일 PDF")]
    assert summary["rejected"] == []
    assert events.sent == []


def test_zero_new_reports_still_sends_a_completion_summary(run_report, monkeypatch):
    runner, config, events, _ = run_report
    item = report()
    state = State(config.state_path)
    state.record(item, "sent", "already-sent")
    state.close()
    monkeypatch.setattr(runner, "SOURCES", {"hankyung": lambda *args, **kw: [item]})
    assert runner.check(config) == 0
    assert len(events.summaries) == 1
    summary = events.summaries[0]
    assert summary["reviewed"] == 0
    assert summary["totals"]["sent"] == 0
    assert summary["totals"]["rejected"] == 0
    assert summary["rejected"] == []


def test_partial_source_failure_is_visible_without_discarding_successful_work(run_report, monkeypatch, caplog):
    runner, config, events, _ = run_report
    item = report()

    def unavailable(*args, **kw):
        raise requests.ReadTimeout("private-source-error")

    monkeypatch.setattr(runner, "SOURCES", {
        "hankyung": lambda *args, **kw: [item], "naver": unavailable,
    })
    assert runner.check(config) == 1
    summary = events.summaries[0]
    assert summary["source_counts"] == {"hankyung": 1}
    assert summary["source_errors"] == 1
    assert summary["totals"]["sent"] == 1
    assert summary["reviewed"] == 1
    assert "private-source-error" not in repr(summary)
    assert "private-source-error" not in caplog.text


def test_pdf_errors_are_reported_with_fixed_stage_reasons(run_report, monkeypatch):
    runner, config, events, _ = run_report
    item = report()
    monkeypatch.setattr(runner, "SOURCES", {"hankyung": lambda *args, **kw: [item]})

    def fail(*args):
        raise requests.ReadTimeout("private-file-error")

    monkeypatch.setattr(runner, "classify", fail)
    assert runner.check(config) == 0
    summary = events.summaries[0]
    assert summary["reviewed"] == 1
    assert summary["totals"]["sent"] == 0
    assert summary["totals"]["linked"] == 1
    assert summary["errors"] == [(item, "PDF를 다운로드하거나 분석하지 못했습니다.")]
    assert "private-file-error" not in repr(summary)


def test_old_error_notice_retry_is_not_counted_as_a_new_review(run_report, monkeypatch):
    runner, config, events, _ = run_report
    item = report()
    state = State(config.state_path)
    state.record(item, "pending: error notice")
    state.close()
    monkeypatch.setattr(runner, "SOURCES", {"hankyung": lambda *args, **kw: [item]})
    assert runner.check(config) == 0
    summary = events.summaries[0]
    assert summary["reviewed"] == 0
    assert summary["totals"]["linked"] == 1
    assert len(summary["errors"]) == 1


def test_failed_completion_notification_preserves_sent_history_and_deduplication(run_report, monkeypatch, caplog):
    runner, config, events, controls = run_report
    item = report()
    monkeypatch.setattr(runner, "SOURCES", {"hankyung": lambda *args, **kw: [item]})
    controls.footer_error = RuntimeError("private-notification-error")
    assert runner.check(config) == 1
    state = State(config.state_path)
    assert state.outcome(item) == "sent"
    assert state.duplicate(item)
    state.close()
    controls.footer_error = None
    assert runner.check(config) == 0
    assert events.sent == [item]
    assert len(events.summaries) == 2
    assert events.summaries[-1]["reviewed"] == 0
    assert "private-notification-error" not in caplog.text


@pytest.mark.parametrize("mode", ["preview_only", "dry_run"])
def test_preview_modes_do_not_send_a_completion_summary(run_report, monkeypatch, mode):
    runner, config, events, _ = run_report
    monkeypatch.setattr(runner, "SOURCES", {"hankyung": lambda *args, **kw: [report()]})
    assert runner.check(config, **{mode: True}) == 0
    assert events.summaries == []
    assert events.sent == []

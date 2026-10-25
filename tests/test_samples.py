from contextlib import nullcontext
from datetime import date

import pytest
import requests

from reportbot.classify import Decision
from reportbot.config import Config
from reportbot.models import Report
from reportbot.telegram import Telegram, TelegramError


def report(number, **changes):
    values = dict(
        source="hankyung", source_id=str(number), title=f"산업 구조 변화 {number}",
        broker="미래증권", published_date=date.today(),
        detail_url=f"https://example.com/report/{number}",
        pdf_url=f"https://example.com/report/{number}.pdf",
    )
    values.update(changes)
    return Report(**values)


@pytest.fixture
def samples():
    from reportbot import samples

    return samples


@pytest.fixture
def config(tmp_path):
    return Config(tmp_path / "scheduled-state.sqlite3", "123:dummy-token", "12345", 20, 3, 1800, 14)


def accept(item, *args):
    return Decision(True, "산업 리포트 25쪽", 25, "pdf-" + item.source_id)


def test_selection_rejects_non_industry_periodic_and_both_duplicate_kinds(samples, monkeypatch):
    first = report(1, title="반도체 산업의 구조 변화")
    title_duplicate = report(101, source="naver", title=first.title)
    pdf_duplicate = report(2, title="동일 PDF의 다른 제목")
    rejected = [
        report(3, title="반도체 Weekly"),
        report(4, title="삼성전자(005930) 기업 분석"),
        report(5, category="company"),
    ]
    remaining = [report(n) for n in range(6, 10)]
    inspected = []

    def classify(item, *args):
        inspected.append(item.source_id)
        digest = "same-pdf" if item.source_id in {"1", "101", "2"} else "pdf-" + item.source_id
        return Decision(True, "산업 리포트 25쪽", 25, digest)

    monkeypatch.setattr(samples, "classify", classify)
    selected = samples.select_reports([first, title_duplicate, pdf_duplicate, *rejected, *remaining], object())
    assert len(selected) == 5
    assert len({decision.pdf_hash for _, decision in selected}) == 5
    assert not {"3", "4", "5"}.intersection(inspected)
    selected_ids = {item.source_id for item, _ in selected}
    assert len(selected_ids.intersection({"1", "101", "2"})) == 1
    assert {"6", "7", "8", "9"}.issubset(selected_ids)


def test_pdf_download_failure_can_be_skipped_before_selection(samples, monkeypatch):
    bad = report(100)
    good = [report(n) for n in range(1, 6)]

    def classify(item, *args):
        if item.source_id == "100":
            raise requests.Timeout("temporary PDF failure")
        return accept(item)

    monkeypatch.setattr(samples, "classify", classify)
    selected = samples.select_reports([bad, *good], object())
    assert len(selected) == 5
    assert {item.source_id for item, _ in selected} == {str(n) for n in range(1, 6)}


def test_naver_attachment_is_resolved_before_pdf_classification(samples, monkeypatch):
    item = report(100, source="naver", pdf_url=None)
    resolved = []

    def resolve(session, candidate):
        resolved.append(candidate.source_id)
        return "https://example.com/resolved.pdf"

    def classify(candidate, *args):
        assert candidate.pdf_url == "https://example.com/resolved.pdf"
        return accept(candidate)

    monkeypatch.setattr(samples.naver, "resolve_pdf_url", resolve)
    monkeypatch.setattr(samples, "classify", classify)
    selected = samples.select_reports([item], object(), count=1)
    assert len(selected) == 1
    assert resolved == ["100"]


def test_fewer_than_requested_is_rejected_before_any_send(samples, monkeypatch, config):
    monkeypatch.setattr(samples, "make_session", lambda: nullcontext(object()))
    monkeypatch.setattr(samples, "SOURCES", {"hankyung": lambda *a, **k: [report(1)]})
    monkeypatch.setattr(samples, "classify", accept)
    sent = []
    monkeypatch.setattr(Telegram, "send_report", lambda self, r, d: sent.append(r))
    try:
        result = samples.send_samples(config)
    except ValueError:
        pass
    else:
        assert result != 0
    assert sent == []
    assert not config.state_path.exists()


def test_success_sends_exactly_five_without_modifying_scheduled_state(samples, monkeypatch, config):
    candidates = [report(n) for n in range(1, 9)]
    monkeypatch.setattr(samples, "make_session", lambda: nullcontext(object()))
    monkeypatch.setattr(samples, "SOURCES", {"hankyung": lambda *a, **k: candidates})
    monkeypatch.setattr(samples, "classify", accept)
    original_state = b"scheduled state sentinel"
    config.state_path.write_bytes(original_state)
    sent = []
    monkeypatch.setattr(Telegram, "send_report", lambda self, r, d: sent.append(r))
    assert samples.send_samples(config) == 0
    assert len(sent) == 5
    assert len({item.source_id for item in sent}) == 5
    assert config.state_path.read_bytes() == original_state


def test_uncertain_send_failure_stops_without_replaying_prior_messages(samples, monkeypatch, config):
    candidates = [report(n) for n in range(1, 6)]
    monkeypatch.setattr(samples, "make_session", lambda: nullcontext(object()))
    monkeypatch.setattr(samples, "SOURCES", {"hankyung": lambda *a, **k: candidates})
    monkeypatch.setattr(samples, "classify", accept)
    attempted = []

    def send(self, item, decision):
        attempted.append(item.source_id)
        if len(attempted) == 2:
            raise TelegramError("uncertain delivery result")

    monkeypatch.setattr(Telegram, "send_report", send)
    try:
        result = samples.send_samples(config)
    except TelegramError:
        pass
    else:
        assert result != 0
    assert len(attempted) == 2
    assert len(set(attempted)) == 2
    assert not config.state_path.exists()

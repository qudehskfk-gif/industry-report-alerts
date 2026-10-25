from contextlib import nullcontext
from dataclasses import replace
from datetime import date

from reportbot.config import Config
from reportbot.models import Report
from reportbot.sources import naver
from reportbot.sources.shinhan import SingleCompanyReport
from reportbot.state import State


def paper():
    return Report("naver", "46592", "K-제약 제2막", "신한투자증권", date(2026, 10, 22), "https://stock.naver.com/research/industry/46592")


def test_naver_popup_requires_verified_publisher_file(monkeypatch):
    placeholder = "https://www.shinhansec.com/siw/insights/research/list/view-popup.do"

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"nid": "46592", "attachUrl": placeholder, "attachName": "bbs_1792621791875.pdf"}

    class Session:
        def get(self, *args, **kwargs):
            return Response()

    received = []

    def resolve(session, report, name):
        received.append((report, name))
        return "https://bbs2.shinhansec.com/board/message/file.pdf.do?attachmentId=354429"

    monkeypatch.setattr("reportbot.sources.shinhan.resolve_attachment", resolve)
    assert "attachmentId=354429" in naver.resolve_pdf_url(Session(), paper())
    assert received == [(paper(), "bbs_1792621791875.pdf")]


def test_confirmed_single_company_is_rejected_without_sending(tmp_path, monkeypatch):
    from reportbot import runner

    item = paper()
    monkeypatch.setattr(runner, "SOURCES", {"naver": lambda *a, **k: [item]})
    monkeypatch.setattr(runner, "make_session", lambda: nullcontext(object()))

    def company(*args):
        raise SingleCompanyReport("publisher confirms company")

    monkeypatch.setattr(naver, "resolve_pdf_url", company)
    config = Config(tmp_path / "state.sqlite3", "123:dummy", "999", 20, 100, 86400, 14)
    assert runner.check(config, preview_only=True) == 0
    state = State(config.state_path)
    try:
        assert state.outcome(item).startswith("rejected:")
        assert state.pending() == []
        assert not state.duplicate(item)
    finally:
        state.close()

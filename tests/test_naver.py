from datetime import date, datetime, timedelta
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from reportbot.sources.naver import API_URL, fetch_reports, parse_api_page, parse_page, resolve_pdf_url


FIXTURES = Path(__file__).parent / "fixtures"


EXAMPLE = """
<table class="type_1"><tr>
<td class="first">반도체</td>
<td><a href="industry_read.naver?nid=10&amp;page=1">반도체 산업의 구조적 변화</a></td>
<td>미래증권</td>
<td class="file"><a href="https://stock.pstatic.net/industry/report.pdf">PDF</a></td>
<td class="date">26.10.23</td><td class="cnt">50</td>
</tr></table>
"""


def test_industry_row_maps_metadata_and_pdf():
    [report] = parse_page(EXAMPLE)
    assert report.source == "naver"
    assert report.source_id == "10"
    assert report.title == "반도체 산업의 구조적 변화"
    assert report.broker == "미래증권"
    assert report.published_date == date(2026, 10, 23)
    assert report.category == "industry"
    assert report.pdf_url == "https://stock.pstatic.net/industry/report.pdf"
    assert report.detail_url == "https://finance.naver.com/research/industry_read.naver?nid=10&page=1"


def test_company_link_is_never_collected():
    company = EXAMPLE.replace("industry_read.naver", "company_read.naver")
    with pytest.raises(ValueError, match="empty|layout"):
        parse_page(company)


@pytest.mark.parametrize("body", ["<html>blocked</html>", EXAMPLE.replace("26.10.23", "unknown")])
def test_missing_or_malformed_listing_fails_closed(body):
    with pytest.raises(ValueError):
        parse_page(body)


def test_listing_can_omit_attachment():
    [report] = parse_page(EXAMPLE.replace('<a href="https://stock.pstatic.net/industry/report.pdf">PDF</a>', ""))
    assert report.pdf_url is None


def test_pagination_repetition_fails_instead_of_hiding_incomplete_scan():
    class Response:
        def json(self):
            return json.loads((FIXTURES / "naver_industry.json").read_text())

        def raise_for_status(self):
            pass

    class Session:
        def __init__(self):
            self.pages = []

        def get(self, url, *, params, timeout):
            self.pages.append(params["index"])
            assert url == API_URL
            return Response()

    session = Session()
    with pytest.raises(ValueError, match="repeated"):
        fetch_reports(session, max_pages=3)
    assert session.pages == [0, 1]


def test_live_fixture():
    reports, has_next = parse_api_page(json.loads((FIXTURES / "naver_industry.json").read_text()))
    assert len(reports) == 20
    assert has_next
    assert reports[0].source_id == "46614"
    assert reports[0].published_date == date(2026, 10, 23)
    assert all(r.source == "naver" and r.category == "industry" for r in reports)
    assert next(r for r in reports if r.source_id == "46610").periodic
    assert not next(r for r in reports if r.source_id == "46612").periodic


def test_lazy_detail_resolves_real_pdf_url():
    reports, _ = parse_api_page(json.loads((FIXTURES / "naver_industry.json").read_text()))
    report = next(r for r in reports if r.source_id == "46612")
    detail = json.loads((FIXTURES / "naver_detail.json").read_text())

    class Response:
        def json(self):
            return detail

        def raise_for_status(self):
            pass

    class Session:
        def get(self, url, *, timeout):
            assert url == API_URL + "/46612"
            return Response()

    assert resolve_pdf_url(Session(), report) == detail["attachUrl"]


@pytest.mark.parametrize("body", [{}, {"items": [], "hasNext": "false"}, {"items": [{}], "hasNext": False}, {"isSuccess": False}])
def test_api_layout_failure_is_never_empty_success(body):
    with pytest.raises(ValueError):
        parse_api_page(body)


def test_periodic_words_in_body_do_not_reject_in_depth():
    body = json.loads((FIXTURES / "naver_industry.json").read_text())
    body["items"] = [dict(body["items"][0], content='<p><strong>산업 구조 변화</strong></p><p>주간 데이터와 monthly 판매량을 장기 분석합니다.</p>')]
    [report], _ = parse_api_page(body)
    assert not report.periodic


def test_single_backdated_upload_does_not_end_overlap_scan():
    today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    original = json.loads((FIXTURES / "naver_industry.json").read_text())["items"][0]
    pages = [
        {"items": [dict(original, nid="100", writeDate=today.isoformat()),
                   dict(original, nid="101", writeDate=(today - timedelta(days=100)).isoformat())], "hasNext": True},
        {"items": [dict(original, nid="99", writeDate=(today - timedelta(days=46)).isoformat())], "hasNext": True},
    ]
    calls = []

    class Session:
        def get(self, *args, **kwargs):
            index = kwargs["params"]["index"]
            calls.append(index)
            return type("Response", (), {"raise_for_status": lambda self: None, "json": lambda self: pages[index]})()

    reports = fetch_reports(Session(), max_pages=3, lookback_days=45)
    assert {r.source_id for r in reports} == {"100", "101", "99"}
    assert calls == [0, 1]


def test_limit_with_more_current_results_raises():
    body = json.loads((FIXTURES / "naver_industry.json").read_text())

    class Session:
        def get(self, *args, **kwargs):
            return type("Response", (), {"raise_for_status": lambda self: None, "json": lambda self: body})()

    with pytest.raises(ValueError, match="MAX_LIST_PAGES"):
        fetch_reports(Session(), max_pages=1)


def test_invalid_scan_bounds():
    with pytest.raises(ValueError):
        fetch_reports(None, max_pages=0)
    with pytest.raises(ValueError):
        fetch_reports(None, lookback_days=0)

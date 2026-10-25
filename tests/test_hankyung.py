"""Parser contract tests and regressions from reduced real Hankyung HTML."""

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from reportbot.sources.hankyung import LIST_URL, fetch_reports, parse_reports

FIXTURES = Path(__file__).parent / "fixtures"


SYNTHETIC_LIST = """
<input id="report_type" value="IN"><table><thead><tr><th>작성일</th><th>제목</th><th>분석가</th>
<th>제공출처</th><th>첨부파일</th></tr></thead><tbody>
<tr><td>2026/10/23</td><td class="text_l"><a href="/analysis/downpdf?report_idx=123">
[In-depth] 반도체 산업의 변화</a></td><td>홍길동</td><td>테스트증권</td>
<td><a href="/analysis/downpdf?report_idx=123"><img alt="PDF"></a></td></tr>
</tbody></table>
"""


def test_parse_industry_report():
    [report] = parse_reports(SYNTHETIC_LIST)
    assert report.source == "hankyung"
    assert report.source_id == "123"
    assert report.category == "industry"
    assert report.title == "[In-depth] 반도체 산업의 변화"
    assert report.broker == "테스트증권"
    assert report.published_date == date(2026, 10, 23)
    assert report.pdf_url == "https://consensus.hankyung.com/analysis/downpdf?report_idx=123"
    assert report.explicit_in_depth


@pytest.mark.parametrize("html", [
    "Block access. 0001", "<html>Access denied</html>",
    "<table><tbody><tr><td>unexpected layout</td></tr></tbody></table>",
])
def test_fail_closed_on_block_or_changed_layout(html):
    with pytest.raises(ValueError):
        parse_reports(html)


def test_known_empty_listing():
    assert parse_reports("<table><tbody><tr><td>검색 결과가 없습니다</td></tr></tbody></table>") == []


def test_fetch_requests_industry_only_and_deduplicates():
    calls = []

    class Session:
        def get(self, url, *, params, timeout):
            calls.append((url, params, timeout))
            return SimpleNamespace(text=SYNTHETIC_LIST, url=LIST_URL, raise_for_status=lambda: None)

    reports = fetch_reports(Session(), max_pages=2)
    assert len(reports) == 1
    assert [call[1]["now_page"] for call in calls] == [1, 2]
    assert all(call[1]["report_type"] == "IN" for call in calls)
    assert all(call[1]["skinType"] == "industry" for call in calls)


def test_reject_wrong_category_even_if_http_success():
    with pytest.raises(ValueError, match="category other than industry"):
        parse_reports(SYNTHETIC_LIST.replace('value="IN"', 'value="CO"'))


def test_real_industry_fixture():
    reports = parse_reports((FIXTURES / "hankyung_industry_real.html").read_text())
    assert len(reports) == 3
    assert reports[0].source_id == "653252"
    assert reports[0].title == "[카지노 업황 점검] 정책 불확실성 해소가 관건"
    assert reports[0].broker == "iM증권"
    assert reports[0].published_date == date(2026, 10, 23)
    assert reports[0].pdf_url.endswith("/analysis/downpdf?report_idx=653252")


def test_real_empty_next_page_without_message():
    html = (FIXTURES / "hankyung_empty_real.html").read_text()
    assert parse_reports(html) == []
    with pytest.raises(ValueError):
        parse_reports(html.replace('value="IN"', 'value="CO"'))
    with pytest.raises(ValueError):
        parse_reports(html.replace("제공출처", "changed_column"))


def test_real_company_fixture_rejected():
    with pytest.raises(ValueError):
        parse_reports((FIXTURES / "hankyung_company_real.html").read_text())


def test_real_last_page_stops_fetching():
    pages = [
        (FIXTURES / "hankyung_industry_real.html").read_text(),
        (FIXTURES / "hankyung_empty_real.html").read_text(),
    ]
    calls = []

    class Session:
        def get(self, url, *, params, timeout):
            calls.append(params["now_page"])
            return SimpleNamespace(text=pages[len(calls) - 1], url=LIST_URL, raise_for_status=lambda: None)

    assert len(fetch_reports(Session(), max_pages=3)) == 3
    assert calls == [1, 2]


def test_invalid_pagination():
    with pytest.raises(ValueError):
        fetch_reports(None, max_pages=0)

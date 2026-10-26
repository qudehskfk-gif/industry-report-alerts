from dataclasses import replace
from datetime import date
import json
from urllib.parse import quote

import pytest

from reportbot.models import Report
from reportbot.sources import naver


PDF_URL = "https://stock.pstatic.net/stock-research/industry/21/20261021_industry_856550000.pdf"
POPUP_URL = "https://www.shinhansec.com/siw/insights/research/list/view-popup.do"


def report():
    return Report(
        "naver", "46573", "역래깅 이후, 시선은 구조조정으로", "신한투자증권",
        date(2026, 10, 21), "https://stock.naver.com/research/industry/46573",
    )


def mobile_page(*, props_changes=None, content_changes=None, extra_queries=None):
    content = {
        "researchId": 46573, "title": report().title, "brokerName": report().broker,
        "writeDate": "2026-10-21", "category": "석유화학", "attachUrl": PDF_URL,
    }
    content.update(content_changes or {})
    props = {
        "category": "industry", "researchId": "46573",
        "dehydratedState": {"queries": list(extra_queries or []) + [{
            "queryKey": [{"url": "/research/end", "method": "get", "params": {"category": "industry", "researchId": str(content["researchId"])}}],
            "state": {"data": {"result": {"researchContent": content, "researchSummaries": []}}},
        }]},
    }
    props.update(props_changes or {})
    body = {"props": {"pageProps": props}, "page": "/investment/research/[category]/[researchId]"}
    return '<html><script id="__NEXT_DATA__" type="application/json">' + json.dumps(body, ensure_ascii=False) + "</script></html>"


def test_mobile_pdf_matches_original_report_identity_and_public_cdn():
    assert naver.parse_mobile_pdf_page(mobile_page(), report()) == PDF_URL


def test_mobile_pdf_resolution_is_not_specific_to_one_report():
    item = replace(report(), source_id="46612", title="높아지는 패키지 기판의 위상", broker="하나증권", published_date=date(2026, 10, 23))
    url = "https://stock.pstatic.net/stock-research/industry/57/20261023_industry_928294000.pdf"
    html = mobile_page(props_changes={"researchId": "46612"}, content_changes={
        "researchId": 46612, "title": item.title, "brokerName": item.broker,
        "writeDate": "2026-10-23", "attachUrl": url,
    })
    assert naver.parse_mobile_pdf_page(html, item) == url


@pytest.mark.parametrize("changes", [
    {"researchId": "99999"}, {"category": "company"},
])
def test_mobile_page_rejects_different_report_route(changes):
    with pytest.raises(ValueError):
        naver.parse_mobile_pdf_page(mobile_page(props_changes=changes), report())


@pytest.mark.parametrize("changes", [
    {"researchId": 99999}, {"title": "다른 리포트"}, {"brokerName": "다른증권"},
    {"writeDate": "2026-10-22"}, {"writeDate": "unknown"},
])
def test_mobile_metadata_must_match_discovered_report(changes):
    with pytest.raises(ValueError):
        naver.parse_mobile_pdf_page(mobile_page(content_changes=changes), report())


@pytest.mark.parametrize("url", [
    POPUP_URL,
    "https://example.com/stock-research/industry/21/report.pdf",
    "https://stock.pstatic.net/stock-research/company/21/report.pdf",
    "https://stock.pstatic.net/stock-research/industry/21/report.html",
])
def test_mobile_pdf_requires_naver_industry_cdn_attachment(url):
    with pytest.raises(ValueError):
        naver.parse_mobile_pdf_page(mobile_page(content_changes={"attachUrl": url}), report())


@pytest.mark.parametrize("html", ["<html>blocked</html>", '<script id="__NEXT_DATA__">invalid json</script>', '<script id="__NEXT_DATA__">{}</script>'])
def test_missing_or_changed_mobile_layout_stays_unresolved(html):
    with pytest.raises(ValueError):
        naver.parse_mobile_pdf_page(html, report())


def test_mobile_parser_ignores_unrelated_prefetched_queries():
    unrelated = [{"queryKey": [{"url": "/news/category"}], "state": {"data": {"result": {"title": "뉴스", "attachUrl": "https://example.com/news.pdf"}}}}]
    assert naver.parse_mobile_pdf_page(mobile_page(extra_queries=unrelated), report()) == PDF_URL


def test_mobile_attachment_can_itself_use_naver_pdf_viewer():
    wrapper = "https://m.stock.naver.com/pdf?url=" + quote(PDF_URL, safe="")
    assert naver.parse_mobile_pdf_page(mobile_page(content_changes={"attachUrl": wrapper}), report()) == PDF_URL


@pytest.mark.parametrize("host", ["m.stock.naver.com", "stock.naver.com"])
def test_unwrap_naver_pdf_viewer_preserves_exact_original_url(host):
    assert naver.unwrap_pdf_url(f"https://{host}/pdf?url=" + quote(PDF_URL, safe="")) == PDF_URL
    assert naver.unwrap_pdf_url(PDF_URL) == PDF_URL
    assert naver.unwrap_pdf_url(None) is None


@pytest.mark.parametrize("query", ["", "url=", "url=javascript%3Aalert%281%29", "url=ftp%3A%2F%2Fexample.com%2Freport.pdf", "url=" + quote(PDF_URL, safe="") + "&url=" + quote(PDF_URL, safe="")])
def test_malformed_or_ambiguous_pdf_viewer_url_is_rejected(query):
    with pytest.raises(ValueError):
        naver.unwrap_pdf_url("https://m.stock.naver.com/pdf?" + query)


class Response:
    def __init__(self, *, body=None, text=""):
        self.body = body
        self.text = text

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


class Session:
    def __init__(self, *, mobile_html, attachment=POPUP_URL):
        self.mobile_html = mobile_html
        self.attachment = attachment
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        if url == naver.API_URL + "/46573":
            return Response(body={"nid": "46573", "attachUrl": self.attachment, "attachName": "bbs_1792536205312.pdf"})
        assert url == naver.MOBILE_DETAIL_BASE + "46573"
        return Response(text=self.mobile_html)


def test_resolver_uses_verified_mobile_pdf_before_publisher_search(monkeypatch):
    session = Session(mobile_html=mobile_page())
    monkeypatch.setattr("reportbot.sources.shinhan.resolve_attachment", lambda *args: pytest.fail("mobile metadata already provides the report PDF"))
    assert naver.resolve_pdf_url(session, report()) == PDF_URL
    assert len(session.calls) == 2


def test_unresolved_mobile_page_preserves_existing_publisher_fallback(monkeypatch):
    session = Session(mobile_html="<html>unavailable</html>")
    expected = "https://bbs2.shinhansec.com/board/message/file.pdf.do?attachmentId=354429"
    calls = []
    monkeypatch.setattr("reportbot.sources.shinhan.resolve_attachment", lambda s, r, name: calls.append((r.source_id, name)) or expected)
    assert naver.resolve_pdf_url(session, report()) == expected
    assert calls == [("46573", "bbs_1792536205312.pdf")]


def test_direct_desktop_pdf_does_not_need_mobile_lookup():
    session = Session(mobile_html="", attachment=PDF_URL)
    assert naver.resolve_pdf_url(session, report()) == PDF_URL
    assert session.calls == [naver.API_URL + "/46573"]


def test_desktop_attachment_viewer_is_unwrapped_without_mobile_lookup():
    session = Session(mobile_html="", attachment="https://m.stock.naver.com/pdf?url=" + quote(PDF_URL, safe=""))
    assert naver.resolve_pdf_url(session, report()) == PDF_URL
    assert session.calls == [naver.API_URL + "/46573"]


def test_missing_desktop_attachment_uses_mobile_metadata():
    session = Session(mobile_html=mobile_page(), attachment=None)
    assert naver.resolve_pdf_url(session, report()) == PDF_URL


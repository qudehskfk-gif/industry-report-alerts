"""Hankyung Consensus industry research listings.

Only the source's industry category is requested. Unexpected HTML raises an
error, so an upstream redesign cannot silently look like an empty report list.
"""

import re
from datetime import datetime
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

from reportbot.models import Report


BASE_URL = "https://consensus.hankyung.com"
LIST_URL = BASE_URL + "/analysis/list"
DATE_RE = re.compile(r"\b(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})\b")
IN_DEPTH_RE = re.compile(r"in[\s_-]*depth|인[\s-]*뎁스|심층", re.I)


def parse_reports(html: str, *, page_url: str = LIST_URL) -> list[Report]:
    if re.search(r"block\s+access|access\s+denied|captcha|접근이\s*차단", html, re.I):
        raise ValueError("Hankyung blocked automated access; no report state was advanced")
    soup = BeautifulSoup(html, "html.parser")
    category_input = soup.select_one("input#report_type")
    if category_input is not None and category_input.get("value") != "IN":
        raise ValueError("Hankyung returned a category other than industry (IN)")
    if soup.select_one("li.tab02.on") is not None:
        raise ValueError("Hankyung returned the company listing instead of industry")
    tables = soup.select("table")
    if not tables:
        raise ValueError("Hankyung industry listing: expected report table missing")
    reports = []
    headers = [node.get_text(" ", strip=True) for node in soup.select("table thead th")]
    broker_index = next((i for i, text in enumerate(headers) if "제공출처" in text or "증권사" in text or "제공처" in text), None)
    for row in soup.select("table tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) < 4:
            continue
        date_match = DATE_RE.search(row.get_text(" ", strip=True))
        if not date_match:
            continue
        anchors = row.find_all("a", href=True)
        title_link = row.select_one("td.text_l > a[href*='report_idx=']")
        if title_link is None:
            title_link = next((a for a in anchors if a.get_text(" ", strip=True) and any(
                marker in a["href"] for marker in ("analysis/read", "analysis.detail", "analysis/view")
            )), None)
        if title_link is None:
            title_link = next((a for a in anchors if a.get_text(" ", strip=True) and not a["href"].lower().endswith(".pdf") and "down" not in a["href"].lower()), None)
        if title_link is None:
            raise ValueError("Hankyung industry listing: dated row has no report title link")
        title = title_link.get_text(" ", strip=True)
        detail_url = urljoin(page_url, title_link["href"])
        query = parse_qs(urlparse(detail_url).query)
        source_id = next((query[key][0] for key in ("report_idx", "report_id", "idx", "id") if key in query), detail_url)
        pdf_link = next((a for a in anchors if any(x in a["href"].lower() for x in (".pdf", "down", "file"))), None)
        pdf_url = urljoin(page_url, pdf_link["href"]) if pdf_link else None
        # Resolve the brokerage by its header instead of relying on a position:
        # the company and industry listings have different column counts.
        broker = cells[broker_index].get_text(" ", strip=True) if broker_index is not None and broker_index < len(cells) else ""
        reports.append(Report(
            source="hankyung", source_id=str(source_id), title=title,
            broker=broker,
            published_date=datetime.strptime("-".join(date_match.groups()), "%Y-%m-%d").date(),
            detail_url=detail_url, pdf_url=pdf_url,
            explicit_in_depth=bool(IN_DEPTH_RE.search(title)),
        ))
    if not reports:
        known_empty = any(text in soup.get_text(" ", strip=True) for text in (
            "검색 결과가 없습니다", "등록된 자료가 없습니다", "등록된 데이터가 없습니다",
        ))
        # Pages after the final result contain an empty tbody and no text marker.
        # Accept that only when both the IN category and real report headers are
        # present. A generic empty table remains an upstream layout failure.
        empty_industry_table = category_input is not None and category_input.get("value") == "IN" and any(
            {"작성일", "제목", "제공출처"}.issubset({th.get_text(" ", strip=True) for th in table.select("thead th")})
            and table.find("tbody") is not None
            and table.find("tbody").find("tr") is None
            for table in tables
        )
        if not known_empty and not empty_industry_table:
            raise ValueError("Hankyung industry listing: report rows missing or layout changed")
    return reports


def fetch_reports(session, *, max_pages: int = 3) -> list[Report]:
    if max_pages < 1:
        raise ValueError("max_pages must be at least 1")
    reports = {}
    for page in range(1, max_pages + 1):
        response = session.get(LIST_URL, params={
            "skinType": "industry", "report_type": "IN", "now_page": page,
            "pagenum": 80,
        }, timeout=30)
        response.raise_for_status()
        response.encoding = "utf-8"
        page_reports = parse_reports(response.text, page_url=response.url)
        for report in page_reports:
            reports[report.source_id] = report
        if not page_reports:
            break
    return list(reports.values())

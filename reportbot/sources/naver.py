"""NAVER Finance's industry-only research listing.

Corporate research uses a separate company_list endpoint and is never collected.
"""
from datetime import datetime, timedelta
import re
from urllib.parse import parse_qs, urljoin, urlparse
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from ..models import Report


LIST_URL = "https://finance.naver.com/research/industry_list.naver"
API_URL = "https://stock.naver.com/api/stockSecurity/researches/v2/industry"
DETAIL_BASE = "https://stock.naver.com/research/industry/"


def is_placeholder_pdf_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    return parsed.hostname in {"www.shinhansec.com", "shinhansec.com"} and parsed.path.endswith("/research/list/view-popup.do")


def _api_result(body):
    if not isinstance(body, dict):
        raise ValueError("NAVER industry API returned an invalid response")
    if body.get("isSuccess") is False:
        raise ValueError("NAVER industry API returned an error")
    result = body.get("result", body)
    if not isinstance(result, dict):
        raise ValueError("NAVER industry API result is invalid")
    return result


def parse_api_page(body) -> tuple[list[Report], bool]:
    result = _api_result(body)
    items = result.get("items")
    has_next = result.get("hasNext")
    if not isinstance(items, list) or not isinstance(has_next, bool):
        raise ValueError("NAVER industry API listing layout changed")
    reports = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("NAVER industry API report is invalid")
        source_id = str(item.get("nid", ""))
        title = item.get("title")
        broker = item.get("brokerName")
        published = item.get("writeDate")
        if not source_id.isdigit() or not all(isinstance(s, str) and s.strip() for s in [title, broker, published]):
            raise ValueError("NAVER industry API report metadata is incomplete")
        if item.get("researchType", "industry").lower() != "industry":
            raise ValueError("NAVER industry API returned a different research category")
        try:
            published_date = datetime.fromisoformat(published.replace("Z", "+00:00")).date()
        except ValueError as exc:
            raise ValueError("NAVER industry API report date changed") from exc
        pdf_url = item.get("attachUrl") or None
        if pdf_url and (not isinstance(pdf_url, str) or urlparse(pdf_url).scheme not in ("http", "https")):
            raise ValueError("NAVER industry PDF has an invalid URL")
        # The title can hide a regular bulletin. Inspect only the opening strong
        # heading, rather than discussion of weekly/monthly data in the body.
        content = item.get("content") or ""
        heading = BeautifulSoup(content[:200], "html.parser").find("strong") if isinstance(content, str) else None
        periodic = bool(heading and re.search(
            r"\b(?:weekly|daily|monthly)\b|주간|월간|일간", heading.get_text(" ", strip=True), re.I,
        ))
        reports.append(Report(
            source="naver", source_id=source_id, title=title.strip(), broker=broker.strip(),
            published_date=published_date, detail_url=DETAIL_BASE + source_id, pdf_url=pdf_url,
            periodic=periodic,
        ))
    return reports, has_next


def resolve_pdf_url(session, report: Report) -> str | None:
    """Resolve an attachment only when a new industry report needs inspection."""
    if report.source != "naver" or not report.source_id.isdigit():
        raise ValueError("Expected a NAVER industry report")
    response = session.get(API_URL + "/" + report.source_id, timeout=(10, 30))
    response.raise_for_status()
    result = _api_result(response.json())
    if str(result.get("nid", "")) != report.source_id:
        raise ValueError("NAVER industry detail report identity changed")
    if "attachUrl" not in result:
        raise ValueError("NAVER industry detail attachment metadata changed")
    pdf_url = result["attachUrl"] or None
    if pdf_url and (not isinstance(pdf_url, str) or urlparse(pdf_url).scheme not in ("http", "https")):
        raise ValueError("NAVER industry PDF has an invalid URL")
    if is_placeholder_pdf_url(pdf_url):
        from .shinhan import resolve_attachment

        pdf_url = resolve_attachment(session, report, result.get("attachName", ""))
    return pdf_url


def parse_page(html: bytes | str, *, base_url: str = LIST_URL) -> list[Report]:
    soup = BeautifulSoup(html, "html.parser")
    reports = []
    for link in soup.select('a[href*="industry_read.naver"]'):
        href = link.get("href", "")
        query = parse_qs(urlparse(href).query)
        source_id = query.get("nid", [""])[0]
        row = link.find_parent("tr")
        if row is None or not source_id:
            raise ValueError("NAVER industry listing has an unrecognized report link")
        cells = row.find_all("td", recursive=False)
        title_cell = link.find_parent("td")
        try:
            title_index = next(i for i, cell in enumerate(cells) if cell is title_cell)
            broker = cells[title_index + 1].get_text(" ", strip=True)
        except (StopIteration, IndexError) as exc:
            raise ValueError("NAVER industry listing columns changed") from exc
        title = link.get_text(" ", strip=True)
        date_cell = row.select_one("td.date")
        date_text = date_cell.get_text(" ", strip=True) if date_cell else row.get_text(" ", strip=True)
        date_match = re.search(r"(?<!\d)(\d{2,4})[.\-/](\d{2})[.\-/](\d{2})(?!\d)", date_text)
        if not date_match or not title or not broker:
            raise ValueError("NAVER industry report metadata is incomplete")
        date_text = ".".join(date_match.groups())
        published_date = datetime.strptime(date_text, "%Y.%m.%d" if len(date_match.group(1)) == 4 else "%y.%m.%d").date()
        pdf = row.select_one('td.file a[href], a[href*=".pdf"]')
        pdf_url = urljoin(base_url, pdf["href"]) if pdf else None
        if pdf_url and urlparse(pdf_url).scheme not in ("http", "https"):
            raise ValueError("NAVER industry PDF has an invalid URL")
        reports.append(Report(
            source="naver", source_id=source_id, title=title, broker=broker,
            published_date=published_date, detail_url=urljoin(base_url, href),
            pdf_url=pdf_url,
        ))
    if not reports:
        raise ValueError("NAVER industry listing is empty or its layout changed")
    return reports


def fetch_reports(session, *, max_pages: int = 100, lookback_days: int = 45) -> list[Report]:
    if max_pages < 1:
        raise ValueError("max_pages must be at least 1")
    if lookback_days < 1:
        raise ValueError("lookback_days must be at least 1")
    cutoff = datetime.now(ZoneInfo("Asia/Seoul")).date() - timedelta(days=lookback_days)
    reports: dict[str, Report] = {}
    for page in range(max_pages):
        response = session.get(API_URL, params={"index": page, "size": 20}, timeout=(10, 30))
        response.raise_for_status()
        page_reports, has_next = parse_api_page(response.json())
        if page == 0 and not page_reports:
            raise ValueError("NAVER industry API listing is unexpectedly empty")
        if not page_reports and has_next:
            raise ValueError("NAVER industry API returned an empty page with more results")
        new = [r for r in page_reports if r.source_id not in reports]
        if page_reports and not new:
            raise ValueError("NAVER industry pagination repeated a page; scan is incomplete")
        reports.update((r.source_id, r) for r in new)
        # Scan an overlapping publication window so late additions are found.
        # A single backdated upload must not terminate a still-current page.
        if not has_next or (page_reports and max(r.published_date for r in page_reports) < cutoff):
            return list(reports.values())
    raise ValueError("NAVER industry scan reached MAX_LIST_PAGES before the lookback boundary; increase the limit")

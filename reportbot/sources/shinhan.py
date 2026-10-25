"""Resolve broken NAVER Shinhan links from the publisher's public channel.

No signed-in brokerage pages are used. A public post must match the report
title/date and its published attachment's filename must match NAVER metadata.
"""

from datetime import datetime
from email.message import Message
import re
import unicodedata
from urllib.parse import parse_qs, unquote, urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ..models import Report


CHANNEL_URL = "https://t.me/s/shinhanresearch"
FILE_HOST = "bbs2.shinhansec.com"
SHORT_HOSTS = {"buly.kr", "bit.ly", "zrr.kr", "url.kr", "alie.kr"}


class SingleCompanyReport(ValueError):
    """The verified publisher post identifies one company, not an industry."""


def _normalize(text: str) -> str:
    return re.sub(r"[^\w]", "", unicodedata.normalize("NFKC", text).lower())


def _queries(title: str) -> list[str]:
    words = re.findall(r"[\w가-힣]+", title)
    options = [title.strip(), " ".join(words[:2]), words[0] if words else ""]
    return list(dict.fromkeys(query for query in options if query))


def _official_file_url(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname != FILE_HOST:
        return None
    if parsed.path not in {"/board/message/file.pdf.do", "/board/message/file.do"}:
        return None
    ids = parse_qs(parsed.query).get("attachmentId", [])
    if len(ids) != 1 or not ids[0].isdigit():
        return None
    return f"https://{FILE_HOST}{parsed.path}?attachmentId={ids[0]}"


def _published_links(html: str, report: Report) -> list[tuple[str, bool]]:
    soup = BeautifulSoup(html, "html.parser")
    wanted = _normalize(report.title)
    if not wanted:
        return []
    matches = []
    for post in soup.select('.tgme_widget_message[data-post^="shinhanresearch/"]'):
        content = post.select_one(".tgme_widget_message_text")
        timestamp = post.select_one("time[datetime]")
        if content is None or timestamp is None:
            continue
        try:
            published = datetime.fromisoformat(timestamp["datetime"].replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Seoul")).date()
        except (TypeError, ValueError):
            continue
        # A one-day posting lag is possible, but the exact attachment filename
        # remains mandatory before accepting a URL or company classification.
        if abs((published - report.published_date).days) > 1:
            continue
        # Search highlighting and bold tags are inline; only actual <br> nodes
        # delimit title lines, otherwise a highlighted title gets fragmented.
        for br in content.find_all("br"):
            br.replace_with("\n")
        text = content.get_text("", strip=False)
        if wanted not in _normalize(text):
            continue
        title_lines = [line for line in text.splitlines() if wanted in _normalize(line)]
        single_company = any(re.search(r"\(\s*(?:비상장|\d{6})\s*\)|(?:IPO|기업)\s*분석\s*[:;]", line, re.I) for line in title_lines)
        for link in content.select("a[href]"):
            href = link["href"]
            parsed = urlparse(href)
            official = _official_file_url(href)
            if official:
                matches.append((official, single_company))
            elif parsed.scheme in {"http", "https"} and parsed.hostname in SHORT_HOSTS:
                matches.append((href, single_company))
    return list(dict.fromkeys(matches))


def _filename(headers) -> str | None:
    disposition = headers.get("Content-Disposition", "")
    message = Message()
    message["Content-Disposition"] = disposition
    name = message.get_filename()
    if not name:
        return None
    return unquote(name).replace("\\", "/").rsplit("/", 1)[-1]


def _expand_public_link(session, url: str) -> str | None:
    for _ in range(4):
        official = _official_file_url(url)
        if official:
            return official
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in SHORT_HOSTS:
            return None
        response = session.head(url, timeout=(10, 20), allow_redirects=False)
        try:
            if response.status_code not in {301, 302, 303, 307, 308}:
                return None
            location = response.headers.get("Location")
            if not location:
                return None
            url = urljoin(url, location)
        finally:
            response.close()
    return None


def _matches_filename(session, url: str, expected: str) -> bool:
    response = session.head(url, timeout=(10, 30), allow_redirects=True)
    try:
        if response.status_code in {405, 501}:
            response.close()
            response = session.get(url, timeout=(10, 30), stream=True)
        response.raise_for_status()
        if _official_file_url(response.url) is None:
            return False
        return _filename(response.headers) == expected
    finally:
        response.close()


def resolve_attachment(session, report: Report, attach_name: str) -> str:
    if report.source != "naver" or "신한" not in report.broker:
        raise ValueError("Expected a NAVER Shinhan report")
    if not isinstance(attach_name, str) or not attach_name.lower().endswith(".pdf") or "/" in attach_name or "\\" in attach_name:
        raise ValueError("Shinhan attachment filename metadata is invalid")
    checked = set()
    for query in _queries(report.title):
        response = session.get(CHANNEL_URL, params={"q": query}, timeout=(10, 30))
        response.raise_for_status()
        for link, single_company in _published_links(response.text, report):
            try:
                url = _expand_public_link(session, link)
                if url is None or url in checked:
                    continue
                checked.add(url)
                matched = _matches_filename(session, url, attach_name)
            except requests.RequestException:
                continue
            if not matched:
                continue
            if single_company:
                raise SingleCompanyReport("Verified publisher attachment is a single-company report")
            return url
    raise ValueError("No public Shinhan attachment matched the report title, date, and filename")

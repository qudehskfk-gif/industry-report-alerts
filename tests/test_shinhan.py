from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pytest

from reportbot.models import Report
from reportbot.sources.shinhan import (
    CHANNEL_URL, SingleCompanyReport, _filename, _published_links, resolve_attachment,
)


def report(**changes):
    base = Report("naver", "46592", "K-제약 제2막: RNA 치료제 & 미국 직판", "신한투자증권",
                  date(2026, 10, 22), "https://stock.naver.com/research/industry/46592")
    return replace(base, **changes)


def post(title=None, url="https://bbs2.shinhansec.com/board/message/file.pdf.do?attachmentId=354429", timestamp="2026-10-21T22:37:19+00:00"):
    title = title or report().title
    return f'''<div class="tgme_widget_message" data-post="shinhanresearch/53219">
      <div class="tgme_widget_message_text">『제약바이오(비중확대); {title}』<br>
      기업분석부 윤석현<br><a href="{url}">원문</a></div><time datetime="{timestamp}"></time></div>'''


def response(*, text="", url="https://bbs2.shinhansec.com/board/message/file.pdf.do?attachmentId=354429", filename="bbs_1792621791875.pdf", status=200, location=None):
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    if location:
        headers["Location"] = location
    return SimpleNamespace(text=text, url=url, headers=headers, status_code=status,
                           raise_for_status=lambda: None, close=lambda: None)


def test_public_post_matches_normalized_title_and_korean_date():
    html = post(title="K - <mark>제약</mark> 제2막: RNA 치료제 & 미국 직판")
    assert _published_links(html, report()) == [("https://bbs2.shinhansec.com/board/message/file.pdf.do?attachmentId=354429", False)]
    assert _published_links(post(timestamp="2025-10-21T22:37:19+00:00"), report()) == []


def test_resolver_requires_exact_filename_not_title_alone():
    wrong = "https://bbs2.shinhansec.com/board/message/file.pdf.do?attachmentId=1"
    right = "https://bbs2.shinhansec.com/board/message/file.pdf.do?attachmentId=354429"
    checks = []

    class Session:
        def get(self, url, *, params, timeout):
            assert url == CHANNEL_URL
            return response(text=post(url=wrong) + post(url=right))

        def head(self, url, **kwargs):
            checks.append(url)
            return response(url=url, filename="wrong-year.pdf" if url == wrong else "bbs_1792621791875.pdf")

    assert resolve_attachment(Session(), report(), "bbs_1792621791875.pdf") == right
    assert checks == [wrong, right]


def test_company_title_rejected_only_after_attachment_identity_matches():
    title = "첨단무기의 눈, K-방산 항법장치 핵심 광학부품"
    item = report(title=title)
    html = post(title=f"옵토닉스(비상장): {title}")

    class Session:
        def get(self, *args, **kwargs):
            return response(text=html)

        def head(self, url, **kwargs):
            return response(url=url)

    with pytest.raises(SingleCompanyReport):
        resolve_attachment(Session(), item, "bbs_1792621791875.pdf")
    with pytest.raises(ValueError) as exc:
        resolve_attachment(Session(), item, "different-file.pdf")
    assert not isinstance(exc.value, SingleCompanyReport)


def test_rfc5987_filename_is_supported():
    assert _filename({"Content-Disposition": "attachment; filename*=UTF-8''bbs_1792621447235.pdf"}) == "bbs_1792621447235.pdf"


def test_ipo_market_industry_title_is_not_a_company_label():
    item = report(title="비상장 시장과 IPO 시장의 변화")
    assert _published_links(post(title=item.title), item)[0][1] is False


def test_published_short_url_must_redirect_to_official_file_host():
    short = "https://buly.kr/NmpGED"
    official = "https://bbs2.shinhansec.com/board/message/file.pdf.do?attachmentId=123"
    calls = []

    class Session:
        def get(self, *args, **kwargs):
            return response(text=post(url=short))

        def head(self, url, **kwargs):
            calls.append(url)
            if url == short:
                return response(url=url, status=302, location=official)
            return response(url=url)

    assert resolve_attachment(Session(), report(), "bbs_1792621791875.pdf") == official
    assert calls == [short, official]


def test_no_follow_to_unrelated_host_from_shortener():
    short = "https://buly.kr/NmpGED"

    class Session:
        def get(self, *args, **kwargs):
            return response(text=post(url=short))

        def head(self, url, **kwargs):
            assert url == short
            return response(url=url, status=302, location="https://unrelated.example/private")

    with pytest.raises(ValueError, match="No public"):
        resolve_attachment(Session(), report(), "bbs_1792621791875.pdf")


def test_filename_must_be_valid_and_broker_must_match():
    with pytest.raises(ValueError):
        resolve_attachment(None, report(broker="다른증권"), "file.pdf")
    with pytest.raises(ValueError):
        resolve_attachment(None, report(), "../file.pdf")


def test_same_title_post_without_verified_attachment_stays_pending():
    class Session:
        def get(self, *args, **kwargs):
            return response(text=post())

        def head(self, url, **kwargs):
            return response(url=url, filename="different-file.pdf")

    with pytest.raises(ValueError, match="No public"):
        resolve_attachment(Session(), report(), "bbs_1792621791875.pdf")

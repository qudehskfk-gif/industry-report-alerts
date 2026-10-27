from datetime import date, datetime, timezone
import html
from html.parser import HTMLParser

from reportbot.models import Report
from reportbot.run_summary import format_run_summary


def report(index=1, **changes):
    values = dict(source="naver", source_id=str(index), title=f"산업 구조 변화 {index}",
                  broker="테스트증권", published_date=date(2026, 10, 27),
                  detail_url=f"https://example.com/{index}")
    values.update(changes)
    return Report(**values)


def run(**changes):
    values = dict(
        started_at=datetime(2026, 10, 27, 8, 5, tzinfo=timezone.utc),
        source_counts={"hankyung": 281, "naver": 660}, source_errors=0,
        reviewed=33,
        totals={"sent": 3, "rejected": 30, "duplicates": 0, "linked": 0, "pending": 0},
        rejected=[], duplicates=[], errors=[],
    )
    values.update(changes)
    return values


def visible(messages):
    return "".join(html.unescape(message) for message in messages)


class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, attrs))


def assert_bounded(messages):
    assert messages
    for message in messages:
        assert len(html.unescape(message).encode("utf-16-le")) // 2 <= 4096
        parser = Tags()
        parser.feed(message)
        assert parser.tags == []


def test_lists_discovery_review_and_actual_pdf_deliveries_separately():
    messages = format_run_summary(run())
    text = visible(messages)
    assert "목록 조회: 941건 (최근 45일)" in text
    assert "한경 컨센서스 281건 · 네이버 금융 660건" in text
    assert "이번 검토: 33건 (미처리·재검사 자료)" in text
    assert "PDF 발송: 3건" in text
    assert "실행: 2026-10-27 17:05 (한국시간)" in text
    assert "중복 게재도 포함" in text
    assert_bounded(messages)


def test_zero_new_reports_still_explains_that_run_finished():
    messages = format_run_summary(run(reviewed=0, totals={}))
    text = visible(messages)
    assert "이번 검토: 0건" in text
    assert "PDF 발송: 0건" in text
    assert "조건에 맞지 않아 제외된 리포트\n없음" in text
    assert_bounded(messages)


def test_all_rejected_duplicate_and_error_titles_show_source_broker_and_reason():
    messages = format_run_summary(run(
        totals={"sent": 1, "rejected": 2, "duplicates": 1, "linked": 1, "pending": 0},
        rejected=[(report(1), "최소 10쪽 미만·9쪽"), (report(2), "정기 동향 자료")],
        duplicates=[(report(3), "이미 발송한 제목 또는 PDF")],
        errors=[(report(4), "PDF 원문 주소를 확인하지 못했습니다.")],
    ))
    text = visible(messages)
    for index in range(1, 5):
        assert f"산업 구조 변화 {index}" in text
    assert "네이버 금융 · 테스트증권" in text
    assert "최소 10쪽 미만·9쪽" in text
    assert "중복으로 제외된 리포트" in text
    assert "PDF 오류·링크 안내 대상" in text
    assert_bounded(messages)


def test_partial_source_failure_is_not_presented_as_complete_collection():
    messages = format_run_summary(run(source_counts={"naver": 660}, source_errors=1))
    text = visible(messages)
    assert "목록 조회: 660건" in text
    assert "목록 수집 오류 1건: 확인된 자료 기준 결과" in text
    assert "한경 컨센서스 0건" not in text


def test_summary_recovery_counts_are_distinct_from_pdf_delivery_count():
    messages = format_run_summary(run(totals={
        "sent": 2, "summaries_updated": 3, "summaries_deferred": 1, "summaries_failed": 1,
    }))
    text = visible(messages)
    assert "PDF 발송: 2건" in text
    assert "기존 PDF 메시지 요약 추가: 3건" in text
    assert "이번 요약 보류: 1건" in text
    assert "요약 자동 복구 중단: 1건" in text


def test_untrusted_title_broker_reason_and_source_are_html_escaped():
    title = '<b>원문 & 리포트</b> <a href="https://evil.example">제목</a>'
    messages = format_run_summary(run(
        rejected=[(report(title=title, broker="<회사 & 증권>", source="<기타>"), "<조건 & 이유>")],
    ))
    assert title in visible(messages)
    assert "&lt;b&gt;" in "".join(messages)
    assert "&lt;회사 &amp; 증권&gt;" in "".join(messages)
    assert_bounded(messages)


def test_long_exclusion_list_is_split_without_losing_any_title():
    rejected = [(report(index, title=f"검토 후 제외한 독립적인 산업 보고서 제목 [{index:04d}] " * 2),
                 "최소 10쪽 미만·7쪽") for index in range(140)]
    messages = format_run_summary(run(rejected=rejected, totals={"rejected": len(rejected)}))
    assert len(messages) > 1
    text = visible(messages)
    for item, reason in rejected:
        assert item.title in text
    assert_bounded(messages)


def test_oversized_single_title_and_emoji_keep_every_character_across_chunks():
    title = "산업😀<&>" * 2500
    messages = format_run_summary(run(rejected=[(report(title=title), "길이 조건")]))
    assert len(messages) > 3
    assert title in visible(messages)
    assert visible(messages).count("😀") == 2500
    assert_bounded(messages)


def test_iso_timestamp_is_converted_to_korea_and_invalid_timestamp_is_omitted():
    assert "2026-10-27 17:05" in visible(format_run_summary(run(started_at="2026-10-27T08:05:00Z")))
    assert "실행:" not in visible(format_run_summary(run(started_at="not-a-date")))


def test_custom_lookback_is_reported_without_changing_review_count():
    text = visible(format_run_summary(run(lookback_days=60, reviewed=1)))
    assert "최근 60일" in text
    assert "이번 검토: 1건" in text

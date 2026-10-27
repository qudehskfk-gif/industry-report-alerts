"""Format a completed monitor run without confusing discovery and review."""

from datetime import datetime
import html
from zoneinfo import ZoneInfo


MAX_MESSAGE_UNITS = 4096
_SOURCES = {"hankyung": "한경 컨센서스", "naver": "네이버 금융"}


def _units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _number(value) -> int:
    return value if type(value) is int and value >= 0 else 0


def _source(value) -> str:
    return _SOURCES.get(value, str(value))


def _timestamp(value) -> str | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("Asia/Seoul"))
    return value.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M")


def _pieces(text: str):
    """Split an individual long line only between Unicode code points."""
    buffer, used = [], 0
    for character in text:
        size = _units(character)
        if used + size > MAX_MESSAGE_UNITS:
            yield "".join(buffer)
            buffer, used = [], 0
        buffer.append(character)
        used += size
    if buffer:
        yield "".join(buffer)


def _messages(text: str) -> list[str]:
    # Pack complete lines whenever possible. Escape after splitting so entity
    # spellings cannot be cut and markup never contributes to the text budget.
    chunks, buffer, used = [], [], 0
    for line in text.splitlines(keepends=True):
        for piece in _pieces(line):
            size = _units(piece)
            if buffer and used + size > MAX_MESSAGE_UNITS:
                chunks.append(html.escape("".join(buffer), quote=False))
                buffer, used = [], 0
            buffer.append(piece)
            used += size
    if buffer:
        chunks.append(html.escape("".join(buffer), quote=False))
    return chunks


def _report_lines(items) -> list[str]:
    lines = []
    for index, (report, reason) in enumerate(items, 1):
        lines.extend([
            f"{index}. {report.title}",
            f"   {_source(report.source)} · {report.broker}",
            f"   사유: {reason}",
        ])
    return lines


def format_run_summary(data: dict) -> list[str]:
    """Return safe HTML messages, each <=4096 displayed UTF-16 units.

    ``reviewed`` counts only this run's unprocessed/reopened reports, while
    ``source_counts`` counts listing rows discovered across the lookback.
    Exclusion items are ``(Report, fixed_reason)`` pairs; no titles are omitted
    or truncated. ``started_at`` accepts an aware datetime or ISO string.
    """
    totals = data.get("totals", {})
    sources = data.get("source_counts", {})
    source_counts = {str(key): _number(value) for key, value in sources.items()}
    rejected = data.get("rejected", [])
    duplicates = data.get("duplicates", [])
    errors = data.get("errors", [])
    timestamp = _timestamp(data.get("started_at"))
    lines = ["📊 산업 리포트 실행 결과"]
    if timestamp:
        lines.append(f"실행: {timestamp} (한국시간)")
    lines.extend([
        "",
        f"목록 조회: {sum(source_counts.values())}건 (최근 {_number(data.get('lookback_days', 45))}일)",
    ])
    if source_counts:
        lines.append(" · ".join(f"{_source(key)} {value}건" for key, value in source_counts.items()))
    source_errors = _number(data.get("source_errors", 0))
    if source_errors:
        lines.append(f"⚠️ 목록 수집 오류 {source_errors}건: 확인된 자료 기준 결과입니다.")
    lines.extend([
        "목록 조회는 사이트별 게시 건수로, 양쪽 중복 게재도 포함합니다.",
        f"이번 검토: {_number(data.get('reviewed', 0))}건 (미처리·재검사 자료)",
        f"PDF 발송: {_number(totals.get('sent', 0))}건",
        f"조건 제외: {_number(totals.get('rejected', len(rejected)))}건",
        f"중복 제외: {_number(totals.get('duplicates', len(duplicates)))}건",
        f"오류 원문 링크 안내: {_number(totals.get('linked', 0))}건",
        f"미처리 대기: {_number(totals.get('pending', 0))}건",
    ])
    summary_counts = [
        ("기존 PDF 메시지 요약 추가", "summaries_updated"),
        ("이번 요약 보류", "summaries_deferred"),
        ("요약 자동 복구 중단", "summaries_failed"),
    ]
    for label, key in summary_counts:
        count = _number(totals.get(key, 0))
        if count:
            lines.append(f"{label}: {count}건")
    lines.extend(["", "조건에 맞지 않아 제외된 리포트"])
    lines.extend(_report_lines(rejected) if rejected else ["없음"])
    if duplicates:
        lines.extend(["", "중복으로 제외된 리포트", *_report_lines(duplicates)])
    if errors:
        lines.extend(["", "PDF 오류·링크 안내 대상", *_report_lines(errors)])
    return _messages("\n".join(lines))

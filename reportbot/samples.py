"""Send a requested batch of existing reports without changing monitor history."""

import logging
from collections import defaultdict
from dataclasses import replace
from itertools import zip_longest

from .classify import Decision, classify, title_rejection
from .config import Config
from .http import make_session
from .models import Report
from .sources import hankyung, naver
from .state import title_fingerprint
from .telegram import Telegram

log = logging.getLogger(__name__)
SOURCES = {"hankyung": hankyung.fetch_reports, "naver": naver.fetch_reports}


def select_reports(candidates: list[Report], session, *, count: int = 5, min_pages: int = 10) -> list[tuple[Report, Decision]]:
    if count < 1:
        raise ValueError("count must be positive")
    groups = defaultdict(list)
    for report in candidates:
        groups[report.source].append(report)
    for group in groups.values():
        group.sort(key=lambda r: (r.published_date, int(r.source_id) if r.source_id.isdigit() else 0), reverse=True)
    interleaved = [r for row in zip_longest(*groups.values()) for r in row if r is not None]
    # Latest date first; alternate sources for equally recent reports.
    interleaved.sort(key=lambda r: r.published_date, reverse=True)
    chosen = []
    titles = set()
    hashes = set()
    identities = set()
    for report in interleaved:
        identity = (report.source, report.source_id)
        fingerprint = title_fingerprint(report)
        if identity in identities or fingerprint in titles or title_rejection(report):
            continue
        identities.add(identity)
        try:
            if report.source == "naver" and (not report.pdf_url or naver.is_placeholder_pdf_url(report.pdf_url)):
                report = replace(report, pdf_url=naver.resolve_pdf_url(session, report))
            decision = classify(report, session, min_pages)
        except Exception as exc:
            log.warning("%s/%s PDF 확인 실패 (%s); 다른 후보 확인", report.source, report.source_id, type(exc).__name__)
            continue
        if not decision.accepted or (decision.pdf_hash and decision.pdf_hash in hashes):
            continue
        chosen.append((report, decision))
        titles.add(fingerprint)
        if decision.pdf_hash:
            hashes.add(decision.pdf_hash)
        log.info("선정 %d/%d: %s | %s | %s | %s", len(chosen), count, report.title, report.broker, report.published_date, decision.reason)
        if len(chosen) == count:
            return chosen
    raise ValueError(f"조건을 충족하는 서로 다른 자료가 {len(chosen)}건으로 요청한 {count}건보다 적습니다. 발송하지 않았습니다.")


def send_samples(config: Config) -> int:
    telegram = Telegram(config.token, config.chat_id)
    if not config.chat_id:
        raise ValueError("TELEGRAM_CHAT_ID가 필요합니다.")
    with make_session() as session:
        candidates = []
        for source, fetch in SOURCES.items():
            try:
                reports = fetch(session, max_pages=config.max_list_pages, lookback_days=config.scan_lookback_days)
                candidates.extend(reports)
                log.info("%s 후보 %d건 확인", source, len(reports))
            except Exception as exc:
                log.warning("%s 수집 실패 (%s); 다른 출처 후보 확인", source, type(exc).__name__)
        selected = select_reports(candidates, session, count=5, min_pages=config.min_pdf_pages)
    # Prepare all five first. A failed/uncertain send stops this batch; do not retry it automatically.
    for index, (report, decision) in enumerate(selected, 1):
        telegram.send_report(report, decision)
        log.info("발송 성공 %d/5: %s | %s", index, report.title, report.detail_url)
    log.info("요청한 산업 심층 리포트 5건 발송 완료. 정기 알림 기록은 변경하지 않았습니다.")
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        return send_samples(Config.from_env())
    except Exception as exc:
        # Exception messages can contain credential-bearing request URLs.
        log.error("요청 자료 발송 실패 (%s). 마지막 발송 성공 번호를 확인하세요. 자동 재발송하지 않습니다.", type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

import logging
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .classify import classify, title_rejection
from .config import Config
from .http import make_session
from .sources import hankyung, naver
from .state import State
from .telegram import Telegram

log = logging.getLogger(__name__)
SOURCES = {"hankyung": hankyung.fetch_reports, "naver": naver.fetch_reports}


def check(config: Config, *, dry_run: bool = False) -> int:
    telegram = None if dry_run else Telegram(config.token, config.chat_id)
    if not dry_run and not config.chat_id:
        raise ValueError("TELEGRAM_CHAT_ID를 등록하세요. python -m reportbot telegram-setup")
    state = None if dry_run else State(config.state_path)
    errors = 0
    totals = {"sent": 0, "skipped": 0, "baseline": 0, "preview": 0}
    cutoff = datetime.now(ZoneInfo("Asia/Seoul")).date() - timedelta(days=config.max_report_age_days)
    try:
        with make_session() as session:
            for source, fetch in SOURCES.items():
                try:
                    reports = fetch(session, max_pages=config.max_list_pages)
                    if not reports:
                        raise ValueError("Empty source listing; baseline not saved")
                except Exception as exc:
                    log.error("%s 수집 실패 (%s); 해당 출처 상태를 유지합니다", source, type(exc).__name__)
                    errors += 1
                    continue
                log.info("%s 산업 리포트 %d건 확인", source, len(reports))
                if state is not None and not state.initialized(source):
                    state.baseline(source, reports)
                    totals["baseline"] += len(reports)
                    log.info("%s 기존 %d건 기준선 저장, 첫 실행 발송 없음", source, len(reports))
                    continue
                for report in sorted(reports, key=lambda r: (r.published_date, r.source_id)):
                    if state is not None and state.seen(report):
                        continue
                    # Avoid costly PDF requests for items that can be rejected from listing metadata.
                    reason = title_rejection(report)
                    if report.published_date < cutoff:
                        reason = "오래된 자료"
                    if reason:
                        totals["skipped"] += 1
                        if state is not None:
                            state.record(report, "skipped: " + reason)
                        continue
                    if state is not None and state.duplicate(report):
                        state.record(report, "duplicate")
                        totals["skipped"] += 1
                        continue
                    try:
                        if report.source == "naver" and not report.pdf_url:
                            report = replace(report, pdf_url=naver.resolve_pdf_url(session, report))
                        decision = classify(report, session, config.min_pdf_pages)
                        if not decision.accepted:
                            if state is not None:
                                state.record(report, "skipped: " + decision.reason)
                            totals["skipped"] += 1
                        elif state is not None and state.duplicate(report, decision.pdf_hash):
                            state.record(report, "duplicate")
                            totals["skipped"] += 1
                        elif dry_run:
                            log.info("[발송 미리보기] %s | %s | %s | %s", report.title, report.broker, decision.reason, report.detail_url)
                            totals["preview"] += 1
                        else:
                            telegram.send_report(report, decision)
                            state.record(report, "sent", decision.pdf_hash)
                            totals["sent"] += 1
                    except Exception as exc:
                        # Keep unprocessed so a transient download/send failure can be retried.
                        # Do not print exception URLs: Telegram tokens are embedded in the URL.
                        log.error("%s/%s 처리 실패 (%s); 다음 실행에서 재시도", source, report.source_id, type(exc).__name__)
                        errors += 1
        log.info("결과: %s, 오류=%d", totals, errors)
        return 1 if errors else 0
    finally:
        if state is not None:
            state.close()

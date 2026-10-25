import logging
from dataclasses import replace

from .classify import classify, title_rejection
from .config import Config
from .http import make_session
from .sources import hankyung, naver
from .sources.shinhan import SingleCompanyReport
from .state import State, title_fingerprint
from .telegram import Telegram

log = logging.getLogger(__name__)
SOURCES = {"hankyung": hankyung.fetch_reports, "naver": naver.fetch_reports}


def discover(config, session, state=None):
    """Persist discovery before inspecting PDFs so retry survives a source outage."""
    found = []
    errors = 0
    for source, fetch in SOURCES.items():
        try:
            reports = fetch(session, max_pages=config.max_list_pages, lookback_days=config.scan_lookback_days)
            if not reports:
                raise ValueError("Empty source listing")
            found.extend(reports)
            if state is not None:
                marker = f"daily_discovery_started:{source}"
                first_expanded_scan = state.get_setting(marker) is None
                floor = state.highwater(source) if first_expanded_scan else None
                state.ingest(source, reports, initial_id_floor=floor)
                state.set_setting(marker, "1")
            log.info("%s 산업 리포트 %d건 확인 (%d일 재조회)", source, len(reports), config.scan_lookback_days)
        except Exception as exc:
            log.error("%s 수집 실패 (%s); 저장된 대기 자료는 계속 처리합니다", source, type(exc).__name__)
            errors += 1
    return found, errors


def resolve(report, session):
    if report.source == "naver" and (not report.pdf_url or naver.is_placeholder_pdf_url(report.pdf_url)):
        report = replace(report, pdf_url=naver.resolve_pdf_url(session, report))
    if not report.pdf_url:
        raise ValueError("No attachment yet; keep pending")
    return report


def check(config: Config, *, dry_run: bool = False, preview_only: bool = False) -> int:
    preview = dry_run or preview_only
    telegram = None if preview else Telegram(config.token, config.chat_id)
    if not preview and not config.chat_id:
        raise ValueError("TELEGRAM_CHAT_ID를 등록하세요.")
    state = None if dry_run else State(config.state_path)
    totals = {"sent": 0, "rejected": 0, "duplicates": 0, "ready": 0, "pending": 0}
    preview_titles = set()
    preview_hashes = set()
    try:
        with make_session() as session:
            discovered, errors = discover(config, session, state)
            queue = discovered if state is None else state.pending()
            log.info("미발송 대기 %d건 검사 시작", len(queue))
            for report in sorted(queue, key=lambda r: (r.published_date, r.source, r.source_id)):
                rejection = title_rejection(report)
                if rejection:
                    if state is not None:
                        state.record(report, "rejected: " + rejection)
                    totals["rejected"] += 1
                    continue
                if state is not None and state.duplicate(report):
                    state.record(report, "duplicate")
                    totals["duplicates"] += 1
                    continue
                try:
                    report = resolve(report, session)
                    if state is not None:
                        state.record(report, "pending")
                    decision = classify(report, session, config.min_pdf_pages)
                    if not decision.accepted:
                        if state is not None:
                            state.record(report, "rejected: " + decision.reason, decision.pdf_hash)
                        totals["rejected"] += 1
                        continue
                    if state is not None and state.duplicate(report, decision.pdf_hash):
                        state.record(report, "duplicate", decision.pdf_hash)
                        totals["duplicates"] += 1
                        continue
                    fingerprint = title_fingerprint(report)
                    if preview:
                        if fingerprint in preview_titles or (decision.pdf_hash and decision.pdf_hash in preview_hashes):
                            totals["duplicates"] += 1
                            continue
                        preview_titles.add(fingerprint)
                        if decision.pdf_hash:
                            preview_hashes.add(decision.pdf_hash)
                        totals["ready"] += 1
                        log.info("[발송 대기 PDF] %s/%s | %s | %s | %s | %s", report.source, report.source_id, report.title, report.broker, report.published_date, decision.reason)
                    else:
                        telegram.send_report(report, decision)
                        state.record(report, "sent", decision.pdf_hash)
                        totals["sent"] += 1
                        log.info("PDF 발송 성공: %s/%s | %s", report.source, report.source_id, report.title)
                except SingleCompanyReport:
                    if state is not None:
                        state.record(report, "rejected: 발행사에서 확인된 개별 기업 리포트")
                    totals["rejected"] += 1
                    log.info("개별 기업 자료 제외: %s/%s | %s", report.source, report.source_id, report.title)
                except Exception as exc:
                    if state is not None:
                        state.record(report, "pending: retry")
                    log.error("%s/%s 처리 실패 (%s); 다음 일일 실행에서 재시도", report.source, report.source_id, type(exc).__name__)
                    errors += 1
            if state is not None:
                totals["pending"] = len(state.pending())
            log.info("결과: %s, 오류=%d, PDF발송=%s", totals, errors, not preview)
            return 1 if errors else 0
    finally:
        if state is not None:
            state.close()


def repair_five(config: Config) -> int:
    """Replace the five links previously sent with actual PDFs and checkpoint each."""
    targets = [("hankyung", "653252"), ("naver", "46614"), ("naver", "46612"), ("naver", "46598"), ("naver", "46589")]
    state = State(config.state_path)
    telegram = Telegram(config.token, config.chat_id)
    if not config.chat_id:
        state.close()
        raise ValueError("TELEGRAM_CHAT_ID가 필요합니다.")
    completed = 0
    try:
        with make_session() as session:
            found, errors = discover(config, session, state)
            catalogue = {(r.source, r.source_id): r for r in found + state.pending()}
            for source, source_id in targets:
                marker = f"repaired_pdf:{source}:{source_id}"
                if state.get_setting(marker) == "1":
                    completed += 1
                    continue
                try:
                    report = resolve(catalogue[(source, source_id)], session)
                    decision = classify(report, session, config.min_pdf_pages)
                    if not decision.accepted:
                        raise ValueError("Requested report no longer qualifies")
                    telegram.send_report(report, decision)
                    state.record(report, "sent", decision.pdf_hash)
                    state.set_setting(marker, "1")
                    completed += 1
                    log.info("기존 5건 PDF 첨부 전송 성공 %d/5: %s | %s", completed, report.title, decision.pages)
                except Exception as exc:
                    log.error("%s/%s PDF 교체 실패 (%s); 완료한 자료는 재전송하지 않습니다", source, source_id, type(exc).__name__)
                    return 1
        log.info("기존 5건 PDF 첨부 완료, 실제 발송 기록 저장: %d/5", completed)
        return 1 if errors else 0
    finally:
        state.close()

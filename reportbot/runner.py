import logging
import os
from collections import Counter
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

from .classify import Decision, classify, inspect_pdf, title_rejection
from .config import Config
from .http import make_session
from .sources import hankyung, naver
from .sources.shinhan import SingleCompanyReport
from .state import State, summary_destination_key, title_fingerprint
from .summarize import SummaryError
from .telegram import Telegram, TelegramError

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


def _summary_recovery_enabled() -> bool:
    return (os.getenv("SUMMARY_MODE", "auto").strip().lower() in {"auto", "gemini"}
            and bool(os.getenv("GEMINI_API_KEY", "").strip()))


def _recover_summaries(config, session, state, telegram, totals):
    """Finish Gemini summaries by editing existing PDFs, never sending them again."""
    if not _summary_recovery_enabled():
        return
    destination = summary_destination_key(config.token, config.chat_id)
    for job in state.pending_summaries(destination):
        report = job["report"]
        failure_code = "download_error"
        try:
            report = resolve(report, session)
            pages, _, digest = inspect_pdf(session, report.pdf_url)
            if not job["pdf_hash"] or digest != job["pdf_hash"]:
                failure_code = "pdf_hash_mismatch"
                raise ValueError("Previously delivered PDF could not be confirmed")
            decision = Decision(True, f"산업 리포트 {pages}쪽", pages, digest)
            failure_code = "summary_error"
            telegram.update_report_summary(report, decision, job["message_id"])
            state.complete_summary(report.source, report.source_id, destination)
            totals["summaries_updated"] += 1
            log.info("기존 PDF 메시지의 요약 추가 완료: %s/%s", report.source, report.source_id)
        except Exception as exc:
            if isinstance(exc, SummaryError):
                failure_code = exc.code
            elif isinstance(exc, TelegramError):
                failure_code = "telegram_error"
            outcome = state.defer_summary(report.source, report.source_id, destination, failure_code)
            totals["summaries_failed" if outcome == "failed" else "summaries_deferred"] += 1
            log.warning("기존 PDF 요약 처리 보류: %s/%s (code=%s; state=%s). PDF는 재전송하지 않습니다.",
                        report.source, report.source_id, failure_code, outcome)
            if isinstance(exc, SummaryError) and exc.code == "quota_429":
                # Other queued reports remain untouched; a project-level quota
                # error must not consume all their recovery attempts.
                break


def _summary_receipt(result, config):
    if not isinstance(result, dict) or result.get("_reportbot_summary_status") != "pending":
        return None
    message_id = result.get("message_id")
    if type(message_id) is not int or message_id <= 0:
        # A successful native upload normally supplies this receipt. Do not turn
        # an already delivered PDF into an error-link or resend it if absent.
        log.warning("PDF 발송 응답에 요약 복구용 메시지 번호가 없어 예약하지 못했습니다.")
        return None
    return {"message_id": message_id,
            "destination_key": summary_destination_key(config.token, config.chat_id),
            "error_code": result.get("_reportbot_summary_error", "summary_error")}


def check(config: Config, *, dry_run: bool = False, preview_only: bool = False) -> int:
    started_at = datetime.now(ZoneInfo("Asia/Seoul"))
    preview = dry_run or preview_only
    telegram = None if preview else Telegram(config.token, config.chat_id)
    if not preview and not config.chat_id:
        raise ValueError("TELEGRAM_CHAT_ID를 등록하세요.")
    state = None if dry_run else State(config.state_path)
    totals = {"sent": 0, "linked": 0, "rejected": 0, "duplicates": 0, "ready": 0, "pending": 0,
              "summaries_updated": 0, "summaries_deferred": 0, "summaries_failed": 0}
    preview_titles = set()
    preview_hashes = set()
    report_failures = []
    rejected_reports = []
    duplicate_reports = []
    reviewed = 0
    notification_errors = 0
    run_summary_errors = 0
    try:
        with make_session() as session:
            discovered, errors = discover(config, session, state)
            if state is not None:
                reopened = state.requeue_page_threshold(
                    config.min_pdf_pages, lookback_days=config.scan_lookback_days,
                )
                if reopened:
                    log.info("최소 쪽수 기준 변경으로 기존 제외 자료 %d건 재검사 예약", reopened)
            if telegram is not None:
                # Process old jobs before recording new failed summaries, so an
                # exhausted provider is not immediately retried in the same run.
                _recover_summaries(config, session, state, telegram, totals)
            queue = discovered if state is None else state.pending()
            log.info("미발송 대기 %d건 검사 시작", len(queue))
            for report in sorted(queue, key=lambda r: (r.published_date, r.source, r.source_id)):
                if state is not None and state.outcome(report) == "pending: error notice":
                    # A previous PDF attempt already failed. Only an undelivered
                    # link notice can be retried; never repeat the PDF attempt.
                    reason = state.get_setting(f"report_error_reason:{report.source}:{report.source_id}", "PDF 첨부를 완료하지 못했습니다.")
                    report_failures.append((report, reason))
                    continue
                reviewed += 1
                rejection = title_rejection(report)
                if rejection:
                    if state is not None:
                        state.record(report, "rejected: " + rejection)
                    totals["rejected"] += 1
                    rejected_reports.append((report, rejection))
                    continue
                if state is not None and state.duplicate(report):
                    state.record(report, "duplicate")
                    totals["duplicates"] += 1
                    duplicate_reports.append((report, "이미 발송한 제목 또는 동일 PDF"))
                    continue
                failure_reason = "PDF 원문 주소를 확인하지 못했습니다."
                try:
                    report = resolve(report, session)
                    if state is not None:
                        state.record(report, "pending")
                    failure_reason = "PDF를 다운로드하거나 분석하지 못했습니다."
                    decision = classify(report, session, config.min_pdf_pages)
                    if not decision.accepted:
                        if state is not None:
                            state.record(report, "rejected: " + decision.reason, decision.pdf_hash)
                        totals["rejected"] += 1
                        rejected_reports.append((report, decision.reason))
                        continue
                    if state is not None and state.duplicate(report, decision.pdf_hash):
                        state.record(report, "duplicate", decision.pdf_hash)
                        totals["duplicates"] += 1
                        duplicate_reports.append((report, "이미 발송한 제목 또는 동일 PDF"))
                        continue
                    fingerprint = title_fingerprint(report)
                    if preview:
                        if fingerprint in preview_titles or (decision.pdf_hash and decision.pdf_hash in preview_hashes):
                            totals["duplicates"] += 1
                            duplicate_reports.append((report, "이번 미리보기에서 확인한 중복 제목 또는 동일 PDF"))
                            continue
                        preview_titles.add(fingerprint)
                        if decision.pdf_hash:
                            preview_hashes.add(decision.pdf_hash)
                        totals["ready"] += 1
                        log.info("[발송 대기 PDF] %s/%s | %s | %s | %s | %s", report.source, report.source_id, report.title, report.broker, report.published_date, decision.reason)
                    else:
                        failure_reason = "PDF 첨부를 텔레그램으로 전송하지 못했습니다."
                        delivery = telegram.send_report(report, decision)
                        receipt = _summary_receipt(delivery, config)
                        if receipt is None:
                            state.record(report, "sent", decision.pdf_hash)
                        else:
                            state.record(report, "sent", decision.pdf_hash, summary_job=receipt)
                            totals["summaries_deferred"] += 1
                        totals["sent"] += 1
                        log.info("PDF 발송 성공: %s/%s | %s", report.source, report.source_id, report.title)
                except SingleCompanyReport:
                    if state is not None:
                        state.record(report, "rejected: 발행사에서 확인된 개별 기업 리포트")
                    totals["rejected"] += 1
                    rejected_reports.append((report, "발행사에서 확인된 개별 기업 리포트"))
                    log.info("개별 기업 자료 제외: %s/%s | %s", report.source, report.source_id, report.title)
                except Exception as exc:
                    if state is not None:
                        if preview:
                            # Preview never sends a notice or stops future delivery.
                            state.record(report, "pending")
                        else:
                            state.set_setting(f"report_error_reason:{report.source}:{report.source_id}", failure_reason)
                            state.record(report, "pending: error notice")
                    report_failures.append((report, failure_reason))
                    log.warning("%s/%s PDF 처리 실패 (%s); 원문 링크 안내, 자동 PDF 재시도 없음", report.source, report.source_id, type(exc).__name__)
            if telegram is not None:
                # Finish other PDFs first. Each unavailable report gets its own
                # link message, then leaves the queue without a PDF-delivery key.
                for report, reason in report_failures:
                    try:
                        telegram.send_report_error(report, reason)
                        state.record(report, "error_notified")
                        totals["linked"] += 1
                        log.info("오류 자료 원문 링크 안내 성공: %s/%s | %s", report.source, report.source_id, report.title)
                    except Exception as exc:
                        notification_errors += 1
                        log.error("%s/%s 오류 링크 안내 실패 (%s)", report.source, report.source_id, type(exc).__name__)
            if state is not None:
                totals["pending"] = len(state.pending())
            if telegram is not None:
                try:
                    telegram.send_run_summary({
                        "started_at": started_at,
                        "lookback_days": config.scan_lookback_days,
                        "source_counts": dict(Counter(report.source for report in discovered)),
                        "source_errors": errors,
                        "reviewed": reviewed,
                        "totals": dict(totals),
                        "rejected": list(rejected_reports),
                        "duplicates": list(duplicate_reports),
                        "errors": list(report_failures),
                    })
                    log.info("실행 결과 안내 전송 완료: 검토=%d, PDF발송=%d, 조건제외=%d, 중복제외=%d",
                             reviewed, totals["sent"], totals["rejected"], totals["duplicates"])
                except Exception as exc:
                    # Summary delivery is independent of PDF bookkeeping. A
                    # failed footer must never cause an uploaded PDF to resend.
                    run_summary_errors += 1
                    log.error("실행 결과 안내 전송 실패 (%s)", type(exc).__name__)
            log.info("결과: %s, 수집오류=%d, 리포트오류=%d, 오류안내실패=%d, 실행요약실패=%d, PDF발송=%s", totals, errors, len(report_failures), notification_errors, run_summary_errors, not preview)
            # A report error handled with a delivered link is a completed run.
            # Collection/notification failures still need operator attention.
            return 1 if errors or notification_errors or run_summary_errors else 0
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

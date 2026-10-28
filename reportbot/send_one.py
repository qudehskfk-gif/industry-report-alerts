"""Deliver one explicitly selected, previously unverified industry PDF."""

import argparse
import logging
import re
import sys

from filelock import FileLock

from .classify import classify
from .config import Config
from .http import make_session
from .runner import _summary_receipt, resolve
from .state import State, summary_destination_key
from .telegram import Telegram


log = logging.getLogger(__name__)


def send_one(config: Config, source: str, report_id: str,
             expected_pdf_sha256: str | None = None, *,
             resend: bool = False, request_id: str | None = None) -> dict:
    """Inspect and send exactly one saved report, leaving every other row alone.

    This deliberately does not discover listings, reopen other exclusions, or
    override a report's terminal PDF-error policy. Actual PDF delivery evidence
    makes repeated manual invocations idempotent.
    """
    if source not in {"hankyung", "naver"} or not isinstance(report_id, str) or re.fullmatch(r"[0-9]{1,20}", report_id) is None:
        raise ValueError("저장된 리포트의 출처와 숫자 등록 번호가 필요합니다.")
    if expected_pdf_sha256 == "":
        expected_pdf_sha256 = None
    if expected_pdf_sha256 is not None and (not isinstance(expected_pdf_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", expected_pdf_sha256) is None):
        raise ValueError("확인할 PDF 지문은 SHA-256 형식이어야 합니다.")
    if resend and (expected_pdf_sha256 is None or not isinstance(request_id, str)
                   or re.fullmatch(r"[A-Za-z0-9_-]{1,80}", request_id) is None):
        raise ValueError("명시적 재발송에는 확인한 PDF 지문과 요청 식별자가 필요합니다.")
    if not resend and request_id is not None:
        raise ValueError("재발송 요청 식별자는 명시적 재발송에서만 사용할 수 있습니다.")
    if not config.state_path.is_file() or not config.token or not config.chat_id:
        raise ValueError("기존 발송 기록과 텔레그램 연결 설정이 필요합니다.")
    marker = (f"selected_resend:{summary_destination_key(config.token, config.chat_id)}:"
              f"{source}:{report_id}:{request_id}") if resend else None
    state = State(config.state_path)
    try:
        if marker is not None:
            previous = state.get_setting(marker)
            if previous == "sent":
                log.info("선택한 재발송 요청은 이미 완료됐습니다: %s/%s", source, report_id)
                return {"status": "already_sent", "source": source, "source_id": report_id}
            if previous is not None:
                raise ValueError("이 재발송 요청의 전송 결과를 확인해야 합니다. 자동 반복하지 않습니다.")
        row = state.db.execute("""
            SELECT outcome,metadata_json,pdf_hash FROM reports
            WHERE source=? AND source_id=?
        """, (source, report_id)).fetchone()
        if not row or not row[1]:
            raise ValueError("선택한 리포트의 저장된 원문 정보가 없습니다.")
        outcome, raw, _ = row
        if outcome == "sent" and not resend:
            log.info("선택한 PDF는 이미 발송 완료되어 다시 보내지 않습니다: %s/%s", source, report_id)
            return {"status": "already_sent", "source": source, "source_id": report_id}
        if outcome not in ({"pending", "duplicate", "sent"} if resend else {"pending", "duplicate"}):
            raise ValueError("미처리 또는 중복 제외 자료만 선택 발송할 수 있습니다.")
        report = state._decode(raw)
        if (report.source, report.source_id) != (source, report_id) or report.category != "industry":
            raise ValueError("선택한 산업 리포트의 등록 정보가 일치하지 않습니다.")
        with make_session() as session:
            report = resolve(report, session)
            decision = classify(report, session, config.min_pdf_pages)
        if not decision.accepted:
            raise ValueError("선택한 자료가 현재 산업 리포트 발송 조건에 맞지 않습니다.")
        if not isinstance(decision.pdf_hash, str) or re.fullmatch(r"[0-9a-f]{64}", decision.pdf_hash) is None:
            raise ValueError("선택한 PDF의 내용 지문을 확인하지 못했습니다.")
        if expected_pdf_sha256 is not None and decision.pdf_hash != expected_pdf_sha256:
            raise ValueError("선택한 원문이 사전에 확인한 PDF와 달라 보내지 않았습니다.")
        if not resend and state.duplicate_pdf_match(decision.pdf_hash) is not None:
            state.record(report, "duplicate", decision.pdf_hash)
            log.info("선택한 PDF와 동일한 파일의 발송 기록이 있어 다시 보내지 않습니다: %s/%s", source, report_id)
            return {"status": "duplicate", "source": source, "source_id": report_id}
        telegram = Telegram(config.token, config.chat_id)
        # Use the same Gemini analysis, combined caption, native PDF upload,
        # and persistent caption-recovery receipt as the daily sender.
        if marker is not None:
            # A timed-out upload can already have reached Telegram. Persist the
            # attempt before POST so rerunning the same request cannot duplicate it.
            state.set_setting(marker, "started")
        delivery = telegram.send_report(report, decision)
        receipt = _summary_receipt(delivery, config)
        if outcome != "sent":
            state.record(report, "sent", decision.pdf_hash, summary_job=receipt)
        # A deliberate copy must not rewrite the original delivery timestamp.
        if marker is not None:
            state.set_setting(marker, "sent")
        summary_status = delivery.get("_reportbot_summary_status") if isinstance(delivery, dict) else None
        log.info("선택한 PDF 발송 완료: %s/%s (summary=%s)", source, report_id,
                 summary_status if summary_status in {"complete", "pending", "disabled"} else "unknown")
        return {"status": "sent", "source": source, "source_id": report_id,
                "summary_status": summary_status}
    finally:
        state.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="저장된 미발송 산업 리포트 한 건만 선택 발송")
    parser.add_argument("--source", required=True, choices=["hankyung", "naver"])
    parser.add_argument("--report-id", required=True)
    parser.add_argument("--expected-pdf-sha256")
    parser.add_argument("--resend", action="store_true", help="명시적으로 요청한 한 건을 다시 발송")
    parser.add_argument("--request-id", help="재발송 요청의 고유 식별자")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = Config.from_env()
        with FileLock(str(config.state_path) + ".lock", timeout=0):
            send_one(config, args.source, args.report_id, args.expected_pdf_sha256,
                     resend=args.resend, request_id=args.request_id)
        return 0
    except Exception as exc:
        # Provider and Telegram exceptions can carry credentials or PDF text.
        print(f"선택한 리포트 발송을 완료하지 못했습니다 ({type(exc).__name__}). 설정과 실행 기록을 확인하세요.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

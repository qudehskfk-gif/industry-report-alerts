"""Manually copy a day's delivered PDFs to the configured Telegram group."""

import argparse
from datetime import date, datetime
import hashlib
import hmac
import json
import logging
import sys
import time
from zoneinfo import ZoneInfo

from filelock import FileLock

from .classify import classify
from .config import Config
from .http import make_session
from .runner import resolve
from .state import State
from .telegram import Telegram

log = logging.getLogger(__name__)


def batch_key(config: Config, day: date) -> str:
    destination = hmac.new(config.token.encode(), config.chat_id.encode(), hashlib.sha256).hexdigest()[:24]
    return f"group_replay:{day.isoformat()}:{destination}"


def replay(config: Config, day: date) -> int:
    """Checkpoint copies separately, preserving the original delivery ledger."""
    if not config.state_path.is_file():
        raise ValueError("기존 발송 기록 파일이 필요합니다.")
    try:
        destination_id = int(config.chat_id)
    except (ValueError, TypeError):
        raise ValueError("등록된 그룹 수신처를 확인하세요.") from None
    telegram = Telegram(config.token, config.chat_id)
    chat = telegram.call("getChat", {"chat_id": config.chat_id})
    if not isinstance(chat, dict) or chat.get("id") != destination_id or chat.get("type") not in {"group", "supergroup"}:
        raise ValueError("그룹방으로 설정된 수신처에서만 다시 발송할 수 있습니다.")
    state = State(config.state_path)
    key = batch_key(config, day)
    totals = {"selected": 0, "sent": 0, "skipped": 0, "linked": 0, "errors": 0}
    last_delivery = None
    try:
        frozen = state.get_setting(key + ":targets")
        if frozen is None:
            targets = [list(row) for row in state.db.execute("""
                SELECT source,source_id FROM reports
                WHERE outcome='sent' AND metadata_json IS NOT NULL
                    AND date(processed_at,'+9 hours')=?
                ORDER BY processed_at,source,source_id
            """, (day.isoformat(),)).fetchall()]
            state.set_setting(key + ":targets", json.dumps(targets))
        else:
            targets = json.loads(frozen)
        if not isinstance(targets, list) or any(not isinstance(pair, list) or len(pair) != 2 or not all(isinstance(value, str) for value in pair) for pair in targets):
            raise ValueError("다시 발송할 자료 목록 기록을 확인하세요.")
        totals["selected"] = len(targets)
        failures = []
        log.info("한국시간 %s에 발송한 리포트 %d건을 그룹으로 다시 보냅니다.", day, len(targets))
        with make_session() as session:
            for source, source_id in targets:
                marker = f"{key}:{source}:{source_id}"
                if state.get_setting(marker) in {"sent", "linked"}:
                    totals["skipped"] += 1
                    continue
                row = state.db.execute("SELECT metadata_json,pdf_hash FROM reports WHERE source=? AND source_id=?", (source, source_id)).fetchone()
                if not row or not row[0]:
                    totals["errors"] += 1
                    log.error("다시 발송할 공개 메타데이터가 없습니다: %s/%s", source, source_id)
                    continue
                report = state._decode(row[0])
                reason = state.get_setting(marker + ":error")
                if reason:
                    failures.append((report, marker, reason))
                    continue
                reason = "PDF 원문 주소를 확인하지 못했습니다."
                try:
                    report = resolve(report, session)
                    reason = "PDF를 다운로드하거나 분석하지 못했습니다."
                    decision = classify(report, session, config.min_pdf_pages)
                    if not decision.accepted or (row[1] and decision.pdf_hash != row[1]):
                        raise ValueError("Previously delivered PDF could not be confirmed")
                    reason = "PDF 첨부를 텔레그램으로 전송하지 못했습니다."
                    if last_delivery is not None:
                        time.sleep(max(0, 3.1 - (time.monotonic() - last_delivery)))
                    try:
                        telegram.send_report(report, decision)
                    finally:
                        last_delivery = time.monotonic()
                except Exception as exc:
                    state.set_setting(marker + ":error", reason)
                    failures.append((report, marker, reason))
                    log.warning("그룹 다시 발송 PDF 오류: %s/%s (%s)", source, source_id, type(exc).__name__)
                    continue
                # Do not change reports, delivered keys, or original timestamps.
                state.set_setting(marker, "sent")
                totals["sent"] += 1
                log.info("그룹 PDF 다시 발송 성공: %s/%s | %s", source, source_id, report.title)
            for report, marker, reason in failures:
                try:
                    # Inspection time counts toward the group pacing interval.
                    if last_delivery is not None:
                        time.sleep(max(0, 3.1 - (time.monotonic() - last_delivery)))
                    try:
                        telegram.send_report_error(report, reason)
                    finally:
                        last_delivery = time.monotonic()
                except Exception as exc:
                    totals["errors"] += 1
                    log.error("그룹 원문 링크 안내 실패: %s/%s (%s)", report.source, report.source_id, type(exc).__name__)
                    continue
                state.set_setting(marker, "linked")
                totals["linked"] += 1
                log.info("그룹 원문 링크 안내 성공: %s/%s | %s", report.source, report.source_id, report.title)
        log.info("그룹 다시 발송 결과: %s", totals)
        return 1 if totals["errors"] else 0
    finally:
        state.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="발송한 날짜의 PDF를 현재 그룹방에 한 번 다시 보내기")
    parser.add_argument("--date", type=date.fromisoformat, default=datetime.now(ZoneInfo("Asia/Seoul")).date())
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = Config.from_env()
        with FileLock(str(config.state_path) + ".lock", timeout=0):
            return replay(config, args.date)
    except Exception as exc:
        # Network exceptions may contain a bot-token URL or chat identifiers.
        print(f"그룹 다시 발송을 완료하지 못했습니다 ({type(exc).__name__}). 설정과 실행 기록을 확인하세요.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

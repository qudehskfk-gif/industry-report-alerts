"""Send delivered PDFs with summaries without changing delivery history."""

import argparse
from datetime import date
import hashlib
import io
import logging
import sqlite3
import sys
import time
from pathlib import Path

from pypdf import PdfReader

from .classify import Decision
from .config import Config
from .http import make_session
from .runner import resolve
from .state import State
from .summarize import SummaryError, check_provider_connection, summarize_pdf
from .telegram import Telegram, TelegramError, download_pdf, pdf_filename, report_summary_caption


def send_summary_test(config: Config, day: date | None = None):
    uri = Path(config.state_path).resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as database:
        if day is None:
            rows = database.execute("""
                SELECT metadata_json,pdf_hash FROM reports
                WHERE outcome='sent' AND metadata_json IS NOT NULL
                ORDER BY processed_at DESC,source,source_id LIMIT 1
            """).fetchall()
        else:
            rows = database.execute("""
                SELECT metadata_json,pdf_hash FROM reports
                WHERE outcome='sent' AND metadata_json IS NOT NULL
                    AND date(processed_at,'+9 hours')=?
                ORDER BY processed_at,source,source_id
            """, (day.isoformat(),)).fetchall()
    if not rows:
        raise ValueError("요약을 시험할 기존 발송 자료가 없습니다.")
    telegram = Telegram(config.token, config.chat_id)
    if day is not None:
        try:
            destination = int(config.chat_id)
        except (TypeError, ValueError):
            raise ValueError("등록된 그룹 수신처를 확인하세요.") from None
        chat = telegram.call("getChat", {"chat_id": config.chat_id})
        if (not isinstance(chat, dict) or chat.get("id") != destination
                or chat.get("type") not in {"group", "supergroup"}):
            raise TelegramError("설정된 그룹방 수신처를 확인하지 못했습니다.")
        print(f"한국시간 {day.isoformat()} 발송 자료 {len(rows)}건의 요약을 준비합니다.", flush=True)

    # Complete all generation before sending: never silently substitute a
    # PDF-only delivery or send part of a batch whose summaries are incomplete.
    prepared = []
    with make_session() as session:
        for raw, expected_hash in rows:
            report = resolve(State._decode(raw), session)
            data = download_pdf(report.pdf_url)
            if expected_hash and hashlib.sha256(data).hexdigest() != expected_hash:
                raise ValueError("기존 발송 PDF와 원문 파일이 일치하지 않습니다.")
            pages = len(PdfReader(io.BytesIO(data), strict=False).pages)
            print(f"PDF 원문 확보 완료 ({pages}쪽). 큰 제목과 부가 설명을 갖춘 요약을 요청합니다.", flush=True)
            summary = summarize_pdf(report, data, pages)
            if summary is None:
                raise ValueError("GEMINI_API_KEY Secret을 등록해야 요약을 시험할 수 있습니다.")
            caption = report_summary_caption(report, Decision(True, f"산업 리포트 {pages}쪽", pages), summary)
            print(f"요약 검증 완료 (큰 제목 {len(summary)}개, 설명 {sum(len(section['points']) for section in summary)}줄).", flush=True)
            prepared.append((report, data, caption))

    for index, (report, data, caption) in enumerate(prepared):
        if index:
            time.sleep(3.1)
        with io.BytesIO(data) as document:
            result = telegram.call("sendDocument", {
                "chat_id": config.chat_id, "caption": caption, "parse_mode": "HTML",
            }, files={"document": (pdf_filename(report), document, "application/pdf")})
        if not isinstance(result, dict) or not isinstance(result.get("document"), dict) or not isinstance(result.get("chat"), dict):
            raise TelegramError("요약과 PDF가 포함된 메시지의 첨부 완료 응답을 확인하지 못했습니다.")
        destination = result["chat"].get("type")
        print("그룹방으로 주제별 요약과 PDF 원문을 한 메시지로 보냈습니다." if destination in {"group", "supergroup"} else "설정된 대화방으로 주제별 요약과 PDF 원문을 한 메시지로 보냈습니다.", flush=True)
        print(f"요약·PDF 발송 완료 {index + 1}/{len(prepared)}: {report.source}/{report.source_id}", flush=True)


def main(argv=()) -> int:
    parser = argparse.ArgumentParser(description="최근 한 건 또는 지정한 한국 날짜의 발송 자료에 요약을 붙여 보내기")
    parser.add_argument("--date", type=date.fromisoformat)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = Config.from_env()
        print("Gemini API 연결을 확인합니다.", flush=True)
        check_provider_connection()
        print("Gemini API 키와 모델 연결을 확인했습니다.", flush=True)
        send_summary_test(config, args.date)
        return 0
    except SummaryError as exc:
        diagnostics = [exc.code]
        if exc.status_code is not None:
            diagnostics.append(f"HTTP {exc.status_code}")
        if exc.reason:
            diagnostics.append(exc.reason)
        if exc.code.startswith("validation_") or exc.code in {
            "response_json", "response_schema", "response_incomplete",
        }:
            hint = "요약 응답의 형식·길이 검사를 확인하세요."
        elif exc.code == "quota_429":
            hint = "Gemini 프로젝트의 사용량 제한을 확인하세요."
        elif exc.status_code is not None and exc.status_code >= 500:
            hint = "요약 서비스의 일시적인 오류입니다."
        elif exc.code == "transport_error":
            hint = "요약 서비스 연결 또는 응답 시간을 확인하세요."
        else:
            hint = "표시된 오류 코드와 요약 서비스 설정을 확인하세요."
        print(f"요약 테스트를 완료하지 못했습니다 (SummaryError: {'; '.join(diagnostics)}). {hint}")
        return 1
    except Exception as exc:
        print(f"요약 테스트를 완료하지 못했습니다 ({type(exc).__name__}). API 키와 무료 사용 한도를 확인하세요.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

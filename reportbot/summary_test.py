"""Send one delivered report as a PDF with a sectioned summary caption."""

import io
import logging
import sqlite3
from pathlib import Path

from pypdf import PdfReader

from .classify import Decision
from .config import Config
from .http import make_session
from .runner import resolve
from .state import State
from .summarize import SummaryError, check_provider_connection, summarize_pdf
from .telegram import Telegram, TelegramError, download_pdf, pdf_filename, report_summary_caption


def send_summary_test(config: Config):
    uri = Path(config.state_path).resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as database:
        row = database.execute("""
            SELECT metadata_json FROM reports
            WHERE outcome='sent' AND metadata_json IS NOT NULL
            ORDER BY processed_at DESC,source,source_id LIMIT 1
        """).fetchone()
    if not row:
        raise ValueError("요약을 시험할 기존 발송 자료가 없습니다.")
    report = State._decode(row[0])
    with make_session() as session:
        report = resolve(report, session)
    data = download_pdf(report.pdf_url)
    pages = len(PdfReader(io.BytesIO(data), strict=False).pages)
    print(f"PDF 원문 확보 완료 ({pages}쪽). 큰 제목과 부가 설명을 갖춘 요약을 요청합니다.", flush=True)
    summary = summarize_pdf(report, data, pages)
    if summary is None:
        raise ValueError("GEMINI_API_KEY Secret을 등록해야 요약을 시험할 수 있습니다.")
    caption = report_summary_caption(report, Decision(True, f"산업 리포트 {pages}쪽", pages), summary)
    print(f"요약 검증 완료 (큰 제목 {len(summary)}개, 설명 {sum(len(section['points']) for section in summary)}줄).", flush=True)
    with io.BytesIO(data) as document:
        result = Telegram(config.token, config.chat_id).call("sendDocument", {
            "chat_id": config.chat_id, "caption": caption, "parse_mode": "HTML",
        }, files={"document": (pdf_filename(report), document, "application/pdf")})
    if not isinstance(result, dict) or not isinstance(result.get("document"), dict) or not isinstance(result.get("chat"), dict):
        raise TelegramError("요약과 PDF가 포함된 메시지의 첨부 완료 응답을 확인하지 못했습니다.")
    destination = result["chat"].get("type")
    print("그룹방으로 주제별 요약과 PDF 원문을 한 메시지로 보냈습니다." if destination in {"group", "supergroup"} else "설정된 대화방으로 주제별 요약과 PDF 원문을 한 메시지로 보냈습니다.")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = Config.from_env()
        print("Gemini API 연결을 확인합니다.", flush=True)
        check_provider_connection()
        print("Gemini API 키와 모델 연결을 확인했습니다.", flush=True)
        send_summary_test(config)
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
    raise SystemExit(main())

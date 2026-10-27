"""Send one summary of a delivered report without repeating its PDF."""

import io
import logging
import sqlite3
from pathlib import Path

from pypdf import PdfReader

from .config import Config
from .http import make_session
from .runner import resolve
from .state import State
from .summarize import SummaryError, check_provider_connection, summarize_pdf
from .telegram import Telegram, download_pdf, report_summary_message


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
    print(f"PDF 원문 확보 완료 ({pages}쪽). 10줄 요약을 요청합니다.", flush=True)
    summary = summarize_pdf(report, data, pages)
    if summary is None:
        raise ValueError("GEMINI_API_KEY Secret을 등록해야 요약을 시험할 수 있습니다.")
    result = Telegram(config.token, config.chat_id).send(report_summary_message(report, summary))
    destination = result.get("chat", {}).get("type") if isinstance(result, dict) else None
    print("그룹방으로 10줄 요약 테스트를 보냈습니다." if destination in {"group", "supergroup"} else "설정된 대화방으로 10줄 요약 테스트를 보냈습니다.")


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
        print(f"요약 테스트를 완료하지 못했습니다 (SummaryError: {'; '.join(diagnostics)}). API 키와 무료 사용 한도를 확인하세요.")
        return 1
    except Exception as exc:
        print(f"요약 테스트를 완료하지 못했습니다 ({type(exc).__name__}). API 키와 무료 사용 한도를 확인하세요.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

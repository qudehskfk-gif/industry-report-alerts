import hashlib
import io
import re
from dataclasses import dataclass

from pypdf import PdfReader

from .models import Report

DEPTH = re.compile(r"in[\s‐‑–—_-]*depth|심층\s*(?:분석|리포트|보고서)|산업\s*대해부", re.I)
PERIODIC = re.compile(
    r"\b(?:daily|weekly|monthly|morning|brief|briefing|snapshot|pulse)\b|"
    r"데일리|위클리|먼슬리|모닝|브리프|브리핑|주간|일간|월간|일일|주보|월보|"
    r"(?:실적|어닝)\s*(?:프리뷰|리뷰|preview|review)|동향\s*(?:점검|체크)", re.I
)
COMPANY = re.compile(r"[\[(]\s*\d{6}\s*(?:[,.][A-Z]+)?\s*[\])]|목표\s*주가|투자의견")
MAX_PDF_BYTES = 25 * 1024 * 1024


@dataclass(frozen=True)
class Decision:
    accepted: bool
    reason: str
    pages: int | None = None
    pdf_hash: str | None = None


def title_rejection(report: Report) -> str | None:
    if report.category != "industry":
        return "산업 분류가 아님"
    if COMPANY.search(report.title):
        return "개별 종목 리포트 표시"
    if report.periodic or PERIODIC.search(report.title):
        return "정기 동향·실적 점검 자료"
    return None


def inspect_pdf(session, url: str) -> tuple[int, str, str]:
    with session.get(url, timeout=(10, 60), stream=True) as response:
        response.raise_for_status()
        if int(response.headers.get("Content-Length", "0")) > MAX_PDF_BYTES:
            raise ValueError("PDF exceeds 25 MiB inspection limit")
        data = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            data.extend(chunk)
            if len(data) > MAX_PDF_BYTES:
                raise ValueError("PDF exceeds 25 MiB inspection limit")
    if b"%PDF-" not in data[:1024]:
        raise ValueError("PDF link returned a non-PDF document")
    reader = PdfReader(io.BytesIO(data), strict=False)
    pages = len(reader.pages)
    if pages == 0:
        raise ValueError("PDF has no pages")
    # Some legitimate Korean PDFs have unreadable font maps. Page count remains
    # usable; a missing text layer alone must not block all long reports.
    try:
        front = "\n".join(page.extract_text() or "" for page in reader.pages[:2])
    except Exception:
        front = ""
    return pages, front, hashlib.sha256(data).hexdigest()


def classify(report: Report, session, min_pages: int = 20) -> Decision:
    rejection = title_rejection(report)
    if rejection:
        return Decision(False, rejection)
    explicit = report.explicit_in_depth or bool(DEPTH.search(report.title))
    if not report.pdf_url:
        # An explicit label is enough even when the source supplies only a detail link.
        return Decision(explicit, "명시된 심층 리포트" if explicit else "심층 표시·PDF 없음")
    pages, front, digest = inspect_pdf(session, report.pdf_url)
    if explicit or DEPTH.search(front[:12000]):
        return Decision(True, "In-depth·심층 표시", pages, digest)
    if re.search(r"\b(?:daily|weekly|monthly)\b|(?:주간|월간|일간)\s*(?:리뷰|동향|보고|분석)", front[:800], re.I):
        return Decision(False, "표지의 정기 동향 자료 표시", pages, digest)
    if pages >= min_pages:
        return Decision(True, f"산업 리포트 {pages}쪽 (분량 기준)", pages, digest)
    return Decision(False, f"심층 표시 없음·{pages}쪽", pages, digest)

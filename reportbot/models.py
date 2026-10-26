from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Report:
    source: str
    source_id: str
    title: str
    broker: str
    published_date: date
    detail_url: str
    pdf_url: str | None = None
    category: str = "industry"
    explicit_in_depth: bool = False
    periodic: bool = False
    sector: str | None = None

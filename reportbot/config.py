import os
from dataclasses import dataclass
from pathlib import Path


def positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 1:
        raise ValueError(f"{name} must be at least 1")
    return value


@dataclass(frozen=True)
class Config:
    state_path: Path
    token: str
    chat_id: str
    min_pdf_pages: int
    max_list_pages: int
    poll_seconds: int
    max_report_age_days: int
    scan_lookback_days: int = 45

    @classmethod
    def from_env(cls):
        from dotenv import load_dotenv

        load_dotenv(override=False)
        return cls(
            Path(os.getenv("STATE_PATH", "data/state.sqlite3")),
            os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            positive_int("MIN_PDF_PAGES", 10),
            positive_int("MAX_LIST_PAGES", 100),
            positive_int("POLL_SECONDS", 86400),
            positive_int("MAX_REPORT_AGE_DAYS", 14),
            positive_int("SCAN_LOOKBACK_DAYS", 45),
        )

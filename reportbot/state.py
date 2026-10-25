import hashlib
import re
import sqlite3
import unicodedata
from pathlib import Path

from .models import Report


def title_fingerprint(report: Report) -> str:
    # Remove only presentation differences; retain sector names and substantive words.
    title = re.sub(r"(?i)\b(?:in[\s_-]*depth|심층분석|심층리포트)\b", "", report.title)
    normalize = lambda s: re.sub(r"[^\w]", "", unicodedata.normalize("NFKC", s).lower())
    broker = normalize(report.broker).removesuffix("증권")
    payload = f"{report.published_date.isoformat()}|{broker}|{normalize(title)}"
    return hashlib.sha256(payload.encode()).hexdigest()


class State:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sources (name TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS reports (
                source TEXT NOT NULL, source_id TEXT NOT NULL,
                title TEXT NOT NULL, outcome TEXT NOT NULL,
                processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(source, source_id)
            );
            CREATE TABLE IF NOT EXISTS delivered (
                kind TEXT NOT NULL, fingerprint TEXT NOT NULL,
                PRIMARY KEY(kind, fingerprint)
            );
        """)

    def close(self):
        self.db.close()

    def initialized(self, source: str) -> bool:
        return self.db.execute("SELECT 1 FROM sources WHERE name=?", (source,)).fetchone() is not None

    def seen(self, report: Report) -> bool:
        return self.db.execute("SELECT 1 FROM reports WHERE source=? AND source_id=?", (report.source, report.source_id)).fetchone() is not None

    def baseline(self, source: str, reports: list[Report]):
        with self.db:
            self.db.executemany("INSERT OR IGNORE INTO reports(source, source_id, title, outcome) VALUES (?,?,?,'baseline')",
                                [(r.source, r.source_id, r.title) for r in reports])
            # Also suppress the same preexisting document if it later appears on the other site.
            self.db.executemany("INSERT OR IGNORE INTO delivered(kind, fingerprint) VALUES ('title', ?)",
                                [(title_fingerprint(r),) for r in reports])
            self.db.execute("INSERT OR IGNORE INTO sources(name) VALUES (?)", (source,))

    def duplicate(self, report: Report, pdf_hash: str | None = None) -> bool:
        keys = [("title", title_fingerprint(report))]
        if pdf_hash:
            keys.append(("pdf", pdf_hash))
        return any(self.db.execute("SELECT 1 FROM delivered WHERE kind=? AND fingerprint=?", key).fetchone() for key in keys)

    def record(self, report: Report, outcome: str, pdf_hash: str | None = None):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO reports(source, source_id, title, outcome) VALUES (?,?,?,?)",
                            (report.source, report.source_id, report.title, outcome))
            if outcome == "sent":
                self.db.execute("INSERT OR IGNORE INTO delivered VALUES ('title', ?)", (title_fingerprint(report),))
                if pdf_hash:
                    self.db.execute("INSERT OR IGNORE INTO delivered VALUES ('pdf', ?)", (pdf_hash,))

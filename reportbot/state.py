import hashlib
import json
import re
import sqlite3
import unicodedata
from dataclasses import asdict, fields
from datetime import date
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
            CREATE TABLE IF NOT EXISTS state_meta (
                name TEXT PRIMARY KEY, value TEXT NOT NULL
            );
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(reports)")}
        with self.db:
            if "metadata_json" not in columns:
                self.db.execute("ALTER TABLE reports ADD COLUMN metadata_json TEXT")
            if "pdf_hash" not in columns:
                self.db.execute("ALTER TABLE reports ADD COLUMN pdf_hash TEXT")
            if not self.db.execute("SELECT 1 FROM state_meta WHERE name='delivery_only_v2'").fetchone():
                # Old initial baselines put every title into delivered, even though
                # nothing had been sent. Those title keys cannot prove delivery.
                # Actual PDF-delivery keys remain valid; sent title keys are rebuilt
                # from saved metadata, or when legacy sent rows are hydrated later.
                self.db.execute("DELETE FROM delivered WHERE kind='title'")
                for raw, digest in self.db.execute(
                    "SELECT metadata_json, pdf_hash FROM reports WHERE outcome='sent'"
                ).fetchall():
                    if raw:
                        self._delivered(self._decode(raw), digest)
                self.db.execute("INSERT INTO state_meta(name,value) VALUES ('delivery_only_v2','1')")

    @staticmethod
    def _encode(report: Report) -> str:
        data = asdict(report)
        data["published_date"] = report.published_date.isoformat()
        return json.dumps(data, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _decode(raw: str) -> Report:
        data = json.loads(raw)
        data["published_date"] = date.fromisoformat(data["published_date"])
        names = {field.name for field in fields(Report)}
        return Report(**{key: value for key, value in data.items() if key in names})

    def _delivered(self, report: Report, pdf_hash: str | None = None):
        self.db.execute("INSERT OR IGNORE INTO delivered VALUES ('title', ?)", (title_fingerprint(report),))
        if pdf_hash:
            self.db.execute("INSERT OR IGNORE INTO delivered VALUES ('pdf', ?)", (pdf_hash,))

    def close(self):
        self.db.close()

    def initialized(self, source: str) -> bool:
        return self.db.execute("SELECT 1 FROM sources WHERE name=?", (source,)).fetchone() is not None

    def seen(self, report: Report) -> bool:
        return self.db.execute("SELECT 1 FROM reports WHERE source=? AND source_id=?", (report.source, report.source_id)).fetchone() is not None

    def outcome(self, report: Report) -> str | None:
        row = self.db.execute("SELECT outcome FROM reports WHERE source=? AND source_id=?", (report.source, report.source_id)).fetchone()
        return row[0] if row else None

    def get_setting(self, name: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM state_meta WHERE name=?", (name,)).fetchone()
        return row[0] if row else default

    def set_setting(self, name: str, value: str):
        with self.db:
            self.db.execute("""
                INSERT INTO state_meta(name,value) VALUES (?,?)
                ON CONFLICT(name) DO UPDATE SET value=excluded.value
            """, (name, str(value)))

    def highwater(self, source: str) -> int | None:
        row = self.db.execute("""
            SELECT MAX(CAST(source_id AS INTEGER)) FROM reports
            WHERE source=? AND source_id<>'' AND source_id NOT GLOB '*[^0-9]*'
        """, (source,)).fetchone()
        return row[0]

    def ingest(self, source: str, reports: list[Report], *, initial_id_floor: int | None = None):
        """Persist discovery before any network processing; retain real outcomes."""
        if any(report.source != source for report in reports):
            raise ValueError("Source does not match report metadata")
        with self.db:
            for report in reports:
                row = self.db.execute(
                    "SELECT outcome, pdf_hash FROM reports WHERE source=? AND source_id=?",
                    (report.source, report.source_id),
                ).fetchone()
                outcome, digest = row if row else ("pending", None)
                # An initial wider catalogue scan can discover pre-start history.
                # Keep the already-known backlog while avoiding a historical flood.
                if row is None and initial_id_floor is not None and report.source_id.isdigit() and int(report.source_id) <= initial_id_floor:
                    outcome = "historical_pre_start"
                if outcome == "baseline" or outcome.replace(" ", "") == "skipped:오래된자료":
                    outcome = "pending"
                self.db.execute("""
                    INSERT INTO reports(source,source_id,title,outcome,metadata_json,pdf_hash)
                    VALUES (?,?,?,?,?,?)
                    ON CONFLICT(source,source_id) DO UPDATE SET
                        title=excluded.title, outcome=excluded.outcome,
                        metadata_json=excluded.metadata_json
                """, (report.source, report.source_id, report.title, outcome, self._encode(report), digest))
                if outcome == "sent":
                    self._delivered(report, digest)
            self.db.execute("INSERT OR IGNORE INTO sources(name) VALUES (?)", (source,))

    def pending(self) -> list[Report]:
        """Return queued metadata even if the source no longer lists the report."""
        rows = self.db.execute("""
            SELECT metadata_json FROM reports
            WHERE (outcome='pending' OR outcome LIKE 'pending:%')
                AND metadata_json IS NOT NULL
            ORDER BY processed_at, source, source_id
        """).fetchall()
        return [self._decode(row[0]) for row in rows]

    def baseline(self, source: str, reports: list[Report]):
        with self.db:
            self.db.executemany("""
                INSERT OR IGNORE INTO reports(source,source_id,title,outcome,metadata_json)
                VALUES (?,?,?,'baseline',?)
            """, [(r.source, r.source_id, r.title, self._encode(r)) for r in reports])
            self.db.execute("INSERT OR IGNORE INTO sources(name) VALUES (?)", (source,))

    def duplicate(self, report: Report, pdf_hash: str | None = None) -> bool:
        keys = [("title", title_fingerprint(report))]
        if pdf_hash:
            keys.append(("pdf", pdf_hash))
        return any(self.db.execute("SELECT 1 FROM delivered WHERE kind=? AND fingerprint=?", key).fetchone() for key in keys)

    def record(self, report: Report, outcome: str, pdf_hash: str | None = None):
        with self.db:
            self.db.execute("""
                INSERT INTO reports(source,source_id,title,outcome,metadata_json,pdf_hash)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(source,source_id) DO UPDATE SET
                    title=excluded.title, outcome=excluded.outcome,
                    metadata_json=excluded.metadata_json,
                    pdf_hash=COALESCE(excluded.pdf_hash,reports.pdf_hash),
                    processed_at=CURRENT_TIMESTAMP
            """, (report.source, report.source_id, report.title, outcome, self._encode(report), pdf_hash))
            if outcome == "sent":
                self._delivered(report, pdf_hash)

import hashlib
import hmac
import json
import re
import sqlite3
import unicodedata
from dataclasses import asdict, fields, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .models import Report


_SUMMARY_ERROR_CODES = frozenset({
    "summary_error", "http_error", "quota_429", "transport_error",
    "response_json", "response_schema", "response_incomplete",
    "config_mode", "config_model", "config_key", "model_not_supported",
    "validation_pdf", "validation_page_count", "validation_items",
    "validation_item", "validation_text", "validation_duplicate",
    "validation_pages", "validation_budget", "caption_error", "telegram_error",
    "download_error", "pdf_hash_mismatch", "summary_disabled",
})
_PAGE_ONLY_REJECTION = re.compile(
    r"rejected: (?:심층 표시 없음|최소 [1-9]\d*쪽 미만)·([1-9]\d*)쪽"
)
_PAGE_THRESHOLD_POLICY = "page_threshold_recheck_v1"


def summary_destination_key(token: str, chat_id: str) -> str:
    """Identify the configured recipient without storing its ID or bot token."""
    if not isinstance(token, str) or not token or not isinstance(chat_id, str) or not chat_id:
        raise ValueError("A bot credential and recipient are required")
    return hmac.new(token.encode(), chat_id.encode(), hashlib.sha256).hexdigest()[:24]


def _destination_key(value) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{24}", value) is None:
        raise ValueError("Invalid summary recipient key")
    return value


def _summary_error_code(value) -> str:
    if isinstance(value, str) and (value in _SUMMARY_ERROR_CODES or re.fullmatch(r"http_[1-5][0-9]{2}", value)):
        return value
    return "summary_error"


def _utc_now(now=None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc).replace(microsecond=0)
    if isinstance(now, str):
        now = datetime.fromisoformat(now.replace("Z", "+00:00"))
    if not isinstance(now, datetime):
        raise ValueError("Invalid summary retry time")
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).replace(microsecond=0)


def _utc_text(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


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
            CREATE TABLE IF NOT EXISTS summary_jobs (
                source TEXT NOT NULL, source_id TEXT NOT NULL,
                destination_key TEXT NOT NULL,
                message_id INTEGER NOT NULL CHECK(message_id > 0),
                attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
                state TEXT NOT NULL DEFAULT 'pending'
                    CHECK(state IN ('pending','completed','failed')),
                next_attempt_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_error_code TEXT NOT NULL DEFAULT 'summary_error',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(source,source_id,destination_key)
            );
            CREATE INDEX IF NOT EXISTS summary_jobs_due
                ON summary_jobs(destination_key,state,next_attempt_at);
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
                    "SELECT outcome, pdf_hash, metadata_json FROM reports WHERE source=? AND source_id=?",
                    (report.source, report.source_id),
                ).fetchone()
                outcome, digest, previous_raw = row if row else ("pending", None, None)
                if previous_raw and not report.pdf_url:
                    previous = self._decode(previous_raw)
                    if (previous.title, previous.broker, previous.published_date) == (report.title, report.broker, report.published_date):
                        # List APIs omit attachment URLs. Keep a verified URL so
                        # tomorrow's ingestion does not discard today's resolution.
                        report = replace(report, pdf_url=previous.pdf_url)
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

    def requeue_page_threshold(self, min_pages: int, *, lookback_days: int = 45,
                              today: date | None = None) -> int:
        """Reconsider recent, unsent PDFs rejected only for their page count.

        Keep the active policy for inspection, but do not use it to suppress
        later metadata hydration after a source outage. Changing an eligible
        row to pending makes repeated calls naturally idempotent. Sent,
        historical, periodic, company and PDF-error records are never reopened.
        """
        if type(min_pages) is not int or min_pages < 1:
            raise ValueError("Minimum PDF pages must be positive")
        if type(lookback_days) is not int or lookback_days < 1:
            raise ValueError("Report lookback days must be positive")
        if today is None:
            today = datetime.now(ZoneInfo("Asia/Seoul")).date()
        if type(today) is not date:
            raise ValueError("A report recheck date is required")
        cutoff = today - timedelta(days=lookback_days)
        policy = json.dumps({"min_pages": min_pages, "lookback_days": lookback_days},
                            sort_keys=True)
        previous_policy = self.get_setting(_PAGE_THRESHOLD_POLICY)
        candidates = []
        for source, source_id, outcome, raw in self.db.execute("""
            SELECT source,source_id,outcome,metadata_json FROM reports
            WHERE outcome LIKE 'rejected: %쪽' AND metadata_json IS NOT NULL
        """).fetchall():
            match = _PAGE_ONLY_REJECTION.fullmatch(outcome)
            if not match or int(match[1]) < min_pages:
                continue
            try:
                report = self._decode(raw)
            except (ValueError, TypeError, KeyError):
                # Incomplete legacy metadata can be hydrated by a later scan.
                continue
            if (report.source, report.source_id) != (source, source_id):
                continue
            if report.category != "industry" or not cutoff <= report.published_date <= today:
                continue
            candidates.append((source, source_id, outcome))
        changed = 0
        with self.db:
            for source, source_id, outcome in candidates:
                result = self.db.execute("""
                    UPDATE reports SET outcome='pending'
                    WHERE source=? AND source_id=? AND outcome=?
                """, (source, source_id, outcome))
                changed += result.rowcount
            if previous_policy != policy:
                self.db.execute("""
                    INSERT INTO state_meta(name,value) VALUES (?,?)
                    ON CONFLICT(name) DO UPDATE SET value=excluded.value
                """, (_PAGE_THRESHOLD_POLICY, policy))
        return changed

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

    def record(self, report: Report, outcome: str, pdf_hash: str | None = None,
               *, summary_job: dict | None = None):
        receipt = None
        if summary_job is not None:
            if outcome != "sent" or not isinstance(summary_job, dict):
                raise ValueError("Summary retries require a successful PDF delivery")
            message_id = summary_job.get("message_id")
            if type(message_id) is not int or message_id <= 0:
                raise ValueError("Invalid delivered-message receipt")
            receipt = (message_id, _destination_key(summary_job.get("destination_key")),
                       _summary_error_code(summary_job.get("error_code")))
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
            if receipt is not None:
                message_id, destination_key, error_code = receipt
                # Delivery and recovery receipt commit together. Reingestion or
                # repeated bookkeeping must never reopen a completed/failed job.
                self.db.execute("""
                    INSERT INTO summary_jobs(
                        source,source_id,destination_key,message_id,last_error_code
                    ) VALUES (?,?,?,?,?)
                    ON CONFLICT(source,source_id,destination_key) DO NOTHING
                """, (report.source, report.source_id, destination_key, message_id, error_code))

    def pending_summaries(self, destination_key: str, *, now=None) -> list[dict]:
        """Read due caption-repair jobs for the currently configured recipient."""
        destination_key = _destination_key(destination_key)
        rows = self.db.execute("""
            SELECT j.source,j.source_id,j.destination_key,j.message_id,j.attempts,
                   r.metadata_json,r.pdf_hash,j.last_error_code,j.next_attempt_at
            FROM summary_jobs AS j
            JOIN reports AS r ON r.source=j.source AND r.source_id=j.source_id
            WHERE j.destination_key=? AND j.state='pending'
                AND j.next_attempt_at<=? AND r.outcome='sent'
                AND r.metadata_json IS NOT NULL
            ORDER BY j.next_attempt_at,j.created_at,j.source,j.source_id
        """, (destination_key, _utc_text(_utc_now(now)))).fetchall()
        return [{"source": row[0], "source_id": row[1], "destination_key": row[2],
                 "message_id": row[3], "attempts": row[4],
                 "report": self._decode(row[5]), "pdf_hash": row[6],
                 "error_code": row[7], "next_attempt_at": row[8]}
                for row in rows]

    def complete_summary(self, source: str, source_id: str, destination_key: str) -> bool:
        """Complete recovery without touching PDF delivery or deduplication."""
        destination_key = _destination_key(destination_key)
        with self.db:
            result = self.db.execute("""
                UPDATE summary_jobs SET state='completed',updated_at=CURRENT_TIMESTAMP
                WHERE source=? AND source_id=? AND destination_key=? AND state='pending'
            """, (source, source_id, destination_key))
        return result.rowcount == 1

    def defer_summary(self, source: str, source_id: str, destination_key: str,
                      error_code, *, max_attempts: int = 3,
                      retry_delay_seconds: int = 3600, now=None) -> str | None:
        """Persist bounded retries; exhausted jobs never become pending again."""
        destination_key = _destination_key(destination_key)
        if type(max_attempts) is not int or max_attempts < 1:
            raise ValueError("Summary retry attempts must be positive")
        if type(retry_delay_seconds) is not int or retry_delay_seconds < 0:
            raise ValueError("Summary retry delay must be nonnegative")
        instant = _utc_now(now)
        next_attempt = _utc_text(instant + timedelta(seconds=retry_delay_seconds))
        with self.db:
            self.db.execute("""
                UPDATE summary_jobs
                SET attempts=attempts+1,
                    state=CASE WHEN attempts+1>=? THEN 'failed' ELSE 'pending' END,
                    next_attempt_at=?,last_error_code=?,updated_at=?
                WHERE source=? AND source_id=? AND destination_key=? AND state='pending'
            """, (max_attempts, next_attempt, _summary_error_code(error_code),
                  _utc_text(instant), source, source_id, destination_key))
            row = self.db.execute("""
                SELECT state FROM summary_jobs
                WHERE source=? AND source_id=? AND destination_key=?
            """, (source, source_id, destination_key)).fetchone()
        return row[0] if row else None

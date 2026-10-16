from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

from .state import utc_now

DOWNLOAD_PENDING = "pending"
DOWNLOAD_DOWNLOADED = "downloaded"
DOWNLOAD_FAILED = "failed"

AVAILABILITY_AVAILABLE = "available"
AVAILABILITY_UNAVAILABLE = "unavailable"
AVAILABILITY_UNKNOWN = "unknown"

STATUS_RE = re.compile(r"\bstatus-(\d{5,})\b")


@dataclass(frozen=True, slots=True)
class BackfillResult:
    associated: int
    unassociated: int


@dataclass(frozen=True, slots=True)
class DueRepost:
    status_id: str
    url: str


def canonical_url(url: str, status_id: str | None = None) -> tuple[str, str | None]:
    parsed = urlparse(url.strip())
    parts = [part for part in parsed.path.split("/") if part]
    found_id = status_id
    if not found_id:
        for index, part in enumerate(parts):
            if part == "status" and index + 1 < len(parts) and parts[index + 1].isdigit():
                found_id = parts[index + 1]
                break
    if found_id:
        return f"https://x.com/i/status/{found_id}", found_id
    return url.strip().split("?", 1)[0].rstrip("/"), None


def compact_error(error: str) -> str:
    return " ".join(error.split())[:1000]


class RepostDB:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._db() as db:
            self._init_schema(db)

    @staticmethod
    def preview_existing_downloads(video_dir: Path) -> BackfillResult:
        associated = 0
        unassociated = 0
        for path in video_dir.iterdir():
            if not path.is_file():
                continue
            if STATUS_RE.search(path.name):
                associated += 1
            else:
                unassociated += 1
        return BackfillResult(associated=associated, unassociated=unassociated)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA busy_timeout = 5000")
        return db

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _init_schema(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS reposts (
              repost_id INTEGER PRIMARY KEY,
              canonical_url TEXT NOT NULL UNIQUE,
              status_id TEXT UNIQUE,
              first_seen_at TEXT NOT NULL,
              last_seen_at TEXT NOT NULL,
              download_status TEXT NOT NULL
                CHECK(download_status IN ('pending','downloaded','failed')),
              downloaded_at TEXT,
              last_failed_at TEXT,
              last_error TEXT,
              local_path TEXT,
              local_paths_json TEXT NOT NULL DEFAULT '[]',
              remote_availability TEXT NOT NULL DEFAULT 'unknown'
                CHECK(remote_availability IN ('available','unavailable','unknown')),
              last_availability_checked_at TEXT,
              last_available_at TEXT,
              first_unavailable_at TEXT,
              last_availability_error TEXT,
              updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_reposts_download_status
              ON reposts(download_status);
            CREATE INDEX IF NOT EXISTS idx_reposts_availability_due
              ON reposts(download_status, last_availability_checked_at);

            CREATE INDEX IF NOT EXISTS idx_reposts_updated_at
              ON reposts(updated_at);

            CREATE VIEW IF NOT EXISTS repost_status AS
            SELECT
              repost_id,
              status_id,
              canonical_url,
              download_status,
              remote_availability,
              first_seen_at,
              last_seen_at,
              downloaded_at,
              last_failed_at,
              last_error,
              local_path,
              local_paths_json,
              last_availability_checked_at,
              last_available_at,
              first_unavailable_at,
              last_availability_error,
              updated_at
            FROM reposts;
            """
        )

    def upsert_seen(self, url: str, status_id: str | None, download_status: str = DOWNLOAD_PENDING) -> None:
        now = utc_now()
        canon, parsed_status_id = canonical_url(url, status_id)
        status_id = status_id or parsed_status_id
        with self._db() as db:
            db.execute(
                """
                INSERT INTO reposts (
                  canonical_url, status_id, first_seen_at, last_seen_at,
                  download_status, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(canonical_url) DO UPDATE SET
                  status_id = COALESCE(reposts.status_id, excluded.status_id),
                  last_seen_at = excluded.last_seen_at,
                  download_status = CASE
                    WHEN reposts.download_status = 'downloaded' THEN reposts.download_status
                    WHEN reposts.download_status = 'failed' THEN reposts.download_status
                    ELSE excluded.download_status
                  END,
                  updated_at = excluded.updated_at
                """,
                (canon, status_id, now, now, download_status, now),
            )

    def mark_downloaded(self, url: str, status_id: str | None, paths: list[Path]) -> None:
        now = utc_now()
        canon, parsed_status_id = canonical_url(url, status_id)
        values = [str(path) for path in paths]
        with self._db() as db:
            db.execute(
                """
                INSERT INTO reposts (
                  canonical_url, status_id, first_seen_at, last_seen_at,
                  download_status, downloaded_at, local_path, local_paths_json,
                  remote_availability, last_available_at, updated_at
                )
                VALUES (?, ?, ?, ?, 'downloaded', ?, ?, ?, 'available', ?, ?)
                ON CONFLICT(canonical_url) DO UPDATE SET
                  status_id = COALESCE(reposts.status_id, excluded.status_id),
                  download_status = 'downloaded',
                  downloaded_at = excluded.downloaded_at,
                  last_error = NULL,
                  local_path = excluded.local_path,
                  local_paths_json = excluded.local_paths_json,
                  remote_availability = 'available',
                  last_available_at = excluded.last_available_at,
                  updated_at = excluded.updated_at
                """,
                (
                    canon,
                    status_id or parsed_status_id,
                    now,
                    now,
                    now,
                    values[0] if values else None,
                    json.dumps(values),
                    now,
                    now,
                ),
            )

    def mark_failed(self, url: str, status_id: str | None, error: str) -> None:
        now = utc_now()
        canon, parsed_status_id = canonical_url(url, status_id)
        with self._db() as db:
            db.execute(
                """
                INSERT INTO reposts (
                  canonical_url, status_id, first_seen_at, last_seen_at,
                  download_status, last_failed_at, last_error, updated_at
                )
                VALUES (?, ?, ?, ?, 'failed', ?, ?, ?)
                ON CONFLICT(canonical_url) DO UPDATE SET
                  status_id = COALESCE(reposts.status_id, excluded.status_id),
                  download_status = 'failed',
                  last_failed_at = excluded.last_failed_at,
                  last_error = excluded.last_error,
                  updated_at = excluded.updated_at
                """,
                (canon, status_id or parsed_status_id, now, now, now, compact_error(error), now),
            )

    def mark_availability(self, url: str, status_id: str | None, status: str, error: str | None = None) -> None:
        now = utc_now()
        canon, parsed_status_id = canonical_url(url, status_id)
        error_value = compact_error(error) if error else None
        with self._db() as db:
            db.execute(
                """
                INSERT INTO reposts (
                  canonical_url, status_id, first_seen_at, last_seen_at,
                  download_status, remote_availability,
                  last_availability_checked_at, last_available_at,
                  first_unavailable_at, last_availability_error, updated_at
                )
                VALUES (
                  ?, ?, ?, ?, 'pending', ?, ?,
                  CASE WHEN ? = 'available' THEN ? ELSE NULL END,
                  CASE WHEN ? = 'unavailable' THEN ? ELSE NULL END,
                  ?, ?
                )
                ON CONFLICT(canonical_url) DO UPDATE SET
                  remote_availability = excluded.remote_availability,
                  last_availability_checked_at = excluded.last_availability_checked_at,
                  last_available_at = CASE
                    WHEN excluded.remote_availability = 'available' THEN excluded.last_available_at
                    ELSE reposts.last_available_at
                  END,
                  first_unavailable_at = CASE
                    WHEN excluded.remote_availability = 'unavailable'
                    THEN COALESCE(reposts.first_unavailable_at, excluded.first_unavailable_at)
                    ELSE reposts.first_unavailable_at
                  END,
                  last_availability_error = excluded.last_availability_error,
                  updated_at = excluded.updated_at
                """,
                (
                    canon,
                    status_id or parsed_status_id,
                    now,
                    now,
                    status,
                    now,
                    status,
                    now,
                    status,
                    now,
                    error_value,
                    now,
                ),
            )

    def due_downloaded(self, min_age_days: int, limit: int | None = None) -> list[DueRepost]:
        query = """
            SELECT status_id, canonical_url
            FROM reposts
            WHERE download_status = 'downloaded'
              AND (
                last_availability_checked_at IS NULL
                OR datetime(last_availability_checked_at) <= datetime('now', ?)
              )
            ORDER BY COALESCE(last_availability_checked_at, first_seen_at), repost_id
        """
        params: list[object] = [f"-{min_age_days} days"]
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        with self._db() as db:
            return [DueRepost(row["status_id"], row["canonical_url"]) for row in db.execute(query, params)]

    def plan_existing_downloads(self, video_dir: Path, metadata_dir: Path) -> list[tuple[Path, str, str]]:
        metadata: dict[str, tuple[str, str | None]] = {}
        for info_path in metadata_dir.glob("*.info.json"):
            try:
                payload = json.loads(info_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            status_id = str(payload.get("id") or "")
            if not status_id.isdigit():
                continue
            url = str(payload.get("webpage_url") or payload.get("original_url") or "")
            if not url:
                url = f"https://x.com/i/status/{status_id}"
            metadata[status_id] = (url, status_id)

        planned: list[tuple[Path, str, str]] = []
        for path in sorted(video_dir.iterdir()):
            if not path.is_file():
                continue
            match = STATUS_RE.search(path.name)
            if not match:
                continue
            status_id = match.group(1)
            url, _ = metadata.get(status_id, (f"https://x.com/i/status/{status_id}", status_id))
            planned.append((path, status_id, url))
        return planned

    def count_unassociated_downloads(self, video_dir: Path) -> int:
        return sum(1 for path in video_dir.iterdir() if path.is_file() and not STATUS_RE.search(path.name))

    def backfill_existing_downloads(self, video_dir: Path, metadata_dir: Path) -> BackfillResult:
        planned = self.plan_existing_downloads(video_dir, metadata_dir)
        for path, status_id, url in planned:
            self.mark_downloaded(url, status_id, [path])
        unassociated = self.count_unassociated_downloads(video_dir)
        return BackfillResult(associated=len(planned), unassociated=unassociated)

    def integrity_summary(self) -> dict[str, object]:
        with self._db() as db:
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = db.execute("PRAGMA foreign_key_check").fetchall()
            rows = db.execute("SELECT COUNT(*) FROM reposts").fetchone()[0]
            duplicate_urls = db.execute(
                "SELECT COUNT(*) FROM (SELECT canonical_url FROM reposts GROUP BY canonical_url HAVING COUNT(*) > 1)"
            ).fetchone()[0]
            return {
                "integrity": integrity,
                "foreign_key_errors": len(foreign_keys),
                "rows": rows,
                "duplicate_urls": duplicate_urls,
            }

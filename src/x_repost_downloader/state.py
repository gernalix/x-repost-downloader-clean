from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator

from .errors import LockBusy

ITEM_DISCOVERED = "discovered"
ITEM_PENDING = "pending"
ITEM_DOWNLOADED = "downloaded"
ITEM_FAILED = "failed"


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(slots=True)
class RunMetrics:
    visited_reposts: int = 0
    video_reposts: int = 0
    discovered: int = 0
    pending: int = 0
    downloaded: int = 0
    downloaded_files: int = 0
    duplicates: int = 0
    failed: int = 0
    scrolls: int = 0


@dataclass(slots=True)
class RepostState:
    status_id: str
    url: str
    author: str
    has_video: bool
    status: str
    sequence: int
    published_at: str | None = None
    first_seen: str = field(default_factory=utc_now)
    last_seen: str = field(default_factory=utc_now)
    attempts: int = 0
    downloaded_files: list[str] = field(default_factory=list)
    archive_ids: list[str] = field(default_factory=list)
    last_error: str | None = None
    completed_at: str | None = None


@dataclass(slots=True)
class State:
    schema: int = 2
    last_check: str | None = None
    last_success: str | None = None
    last_mode: str | None = None
    last_result: str = "never_run"
    last_error: str | None = None
    last_metrics: RunMetrics = field(default_factory=RunMetrics)
    last_backfill_at: str | None = None
    last_backfill_metrics: RunMetrics = field(default_factory=RunMetrics)
    items: dict[str, RepostState] = field(default_factory=dict)
    next_sequence: int = 1
    backfill_runs: int = 0
    backfill_complete: bool = False
    backfill_stop_reason: str | None = None
    backfill_technical_limit: bool = False
    backfill_last_checkpoint: str | None = None
    backfill_scrolls_total: int = 0
    oldest_status_id: str | None = None
    oldest_post_date: str | None = None
    duplicates_total: int = 0
    downloaded_files_total: int = 0
    next_allowed_check_at: str | None = None
    protection_failures_consecutive: int = 0

    def seen(self) -> set[str]:
        return set(self.items)

    def discover(
        self,
        *,
        status_id: str,
        url: str,
        author: str,
        has_video: bool,
        published_at: str | None,
    ) -> RepostState:
        now = utc_now()
        item = self.items.get(status_id)
        if item is None:
            item = RepostState(
                status_id=status_id,
                url=url,
                author=author,
                has_video=has_video,
                status=ITEM_PENDING if has_video else ITEM_DISCOVERED,
                sequence=self.next_sequence,
                published_at=published_at,
                first_seen=now,
                last_seen=now,
            )
            self.next_sequence += 1
            self.items[status_id] = item
        else:
            item.url = url or item.url
            item.author = author or item.author
            item.last_seen = now
            if published_at and not item.published_at:
                item.published_at = published_at
            if has_video and not item.has_video:
                item.has_video = True
                if item.status == ITEM_DISCOVERED:
                    item.status = ITEM_PENDING
        if published_at and (self.oldest_post_date is None or published_at < self.oldest_post_date):
            self.oldest_post_date = published_at
            self.oldest_status_id = status_id
        return item

    def actionable(self, include_failed: bool = True) -> list[RepostState]:
        statuses = {ITEM_PENDING, ITEM_FAILED} if include_failed else {ITEM_PENDING}
        return sorted(
            (item for item in self.items.values() if item.has_video and item.status in statuses),
            key=lambda item: item.sequence,
        )

    def mark_attempt(self, status_id: str) -> RepostState:
        item = self.items[status_id]
        item.attempts += 1
        item.last_error = None
        return item

    def mark_downloaded(self, status_id: str, files: list[Path], archive_ids: list[str]) -> None:
        item = self.items[status_id]
        item.status = ITEM_DOWNLOADED
        item.downloaded_files = [str(path) for path in files]
        item.archive_ids = list(dict.fromkeys(archive_ids))
        item.last_error = None
        item.completed_at = utc_now()

    def mark_failed(self, status_id: str, error: str) -> None:
        item = self.items[status_id]
        item.status = ITEM_FAILED
        item.last_error = error[:1000]
        item.completed_at = None

    def counts(self) -> dict[str, int]:
        values = {ITEM_DISCOVERED: 0, ITEM_PENDING: 0, ITEM_DOWNLOADED: 0, ITEM_FAILED: 0}
        for item in self.items.values():
            values[item.status] = values.get(item.status, 0) + 1
        values["visited_reposts"] = len(self.items)
        values["video_reposts"] = sum(item.has_video for item in self.items.values())
        return values

    def cooldown_active(self, now: str | None = None) -> bool:
        if not self.next_allowed_check_at:
            return False
        checked_at = datetime.fromisoformat(now or utc_now())
        next_allowed = datetime.fromisoformat(self.next_allowed_check_at)
        return checked_at < next_allowed

    def mark_protection_blocked(self, error: str) -> None:
        now = datetime.now(UTC).replace(microsecond=0)
        self.protection_failures_consecutive += 1
        hours = min(24, 2 ** self.protection_failures_consecutive)
        self.next_allowed_check_at = (now + timedelta(hours=hours)).isoformat()
        self.last_check = now.isoformat()
        self.last_result = "x_protection_cooldown"
        self.last_error = error[:1000]

    def reset_protection_cooldown(self) -> None:
        self.next_allowed_check_at = None
        self.protection_failures_consecutive = 0


class StateStore:
    def __init__(self, path: Path):
        self.path = path

    @staticmethod
    def _known_values(cls, raw: dict) -> dict:
        names = {item.name for item in fields(cls)}
        return {key: value for key, value in raw.items() if key in names}

    def _migrate_v1(self, raw: dict) -> State:
        state = State(
            last_check=raw.get("last_check"),
            last_success=raw.get("last_success"),
            last_mode=raw.get("last_mode"),
            last_result=raw.get("last_result", "migrated"),
            last_error=raw.get("last_error"),
            backfill_complete=bool(raw.get("backfill_complete", False)),
        )
        failed = set(raw.get("failed_video_ids", []))
        for status_id in raw.get("seen_repost_ids", []):
            item = state.discover(
                status_id=status_id,
                url="",
                author="",
                has_video=status_id in failed,
                published_at=None,
            )
            if status_id in failed:
                item.status = ITEM_FAILED
                item.last_error = "migrated_from_schema_1"
        return state

    def load(self) -> State:
        if not self.path.exists():
            return State()
        with self.path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        if int(raw.get("schema", 1)) < 2:
            return self._migrate_v1(raw)
        metrics = RunMetrics(**self._known_values(RunMetrics, raw.get("last_metrics", {})))
        backfill_metrics = RunMetrics(**self._known_values(RunMetrics, raw.get("last_backfill_metrics", {})))
        items = {
            status_id: RepostState(**self._known_values(RepostState, item_raw))
            for status_id, item_raw in raw.get("items", {}).items()
        }
        values = self._known_values(State, raw)
        values["last_metrics"] = metrics
        values["last_backfill_metrics"] = backfill_metrics
        values["items"] = items
        return State(**values)

    def save(self, state: State) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(asdict(state), handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            self.path.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise LockBusy("another x-repost-downloader process is already running") from error
        os.ftruncate(descriptor, 0)
        os.write(descriptor, f"{os.getpid()}\n".encode())
        yield
    finally:
        os.close(descriptor)

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from x_repost_downloader.downloader import VideoDownloader
from x_repost_downloader.repost_db import (
    AVAILABILITY_AVAILABLE,
    AVAILABILITY_UNAVAILABLE,
    AVAILABILITY_UNKNOWN,
    DOWNLOAD_DOWNLOADED,
    DOWNLOAD_FAILED,
    RepostDB,
    canonical_url,
)


class RepostDBTests(unittest.TestCase):
    def row(self, path: Path, status_id: str):
        with closing(sqlite3.connect(path)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM reposts WHERE status_id = ?", (status_id,)).fetchone()
            return dict(row) if row else None

    def test_schema_uniqueness_and_upsert_idempotence(self):
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "reposts.sqlite"
            db = RepostDB(db_path)
            db.upsert_seen("https://x.com/A/status/123?x=1", "123")
            first = self.row(db_path, "123")
            db.upsert_seen("https://x.com/Other/status/123", "123")
            second = self.row(db_path, "123")
            summary = db.integrity_summary()
            self.assertEqual(summary["integrity"], "ok")
            self.assertEqual(summary["foreign_key_errors"], 0)
            self.assertEqual(summary["rows"], 1)
            self.assertEqual(summary["duplicate_urls"], 0)
            self.assertEqual(first["first_seen_at"], second["first_seen_at"])
            self.assertEqual(second["canonical_url"], "https://x.com/i/status/123")

    def test_status_view_and_updated_index_are_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "reposts.sqlite"
            db = RepostDB(db_path)
            db.mark_downloaded(
                "https://x.com/source/status/456?ref=timeline",
                "456",
                [Path("/videos/status-456.mp4")],
            )

            # Reopening an existing database is the source migration path.
            RepostDB(db_path)
            RepostDB(db_path)

            with closing(sqlite3.connect(db_path)) as connection:
                connection.row_factory = sqlite3.Row
                raw = dict(connection.execute(
                    "SELECT * FROM reposts WHERE status_id = '456'"
                ).fetchone())
                view = dict(connection.execute(
                    "SELECT * FROM repost_status WHERE status_id = '456'"
                ).fetchone())
                indexes = {
                    row["name"]
                    for row in connection.execute("PRAGMA index_list('reposts')")
                }
                view_count = connection.execute(
                    "SELECT COUNT(*) FROM sqlite_master "
                    "WHERE type = 'view' AND name = 'repost_status'"
                ).fetchone()[0]

            self.assertIn("idx_reposts_updated_at", indexes)
            self.assertEqual(view_count, 1)
            self.assertEqual(view["canonical_url"], "https://x.com/i/status/456")
            self.assertEqual(view["updated_at"], raw["updated_at"])
            self.assertEqual(view["first_seen_at"], raw["first_seen_at"])
            self.assertEqual(view["last_seen_at"], raw["last_seen_at"])
            self.assertEqual(view["downloaded_at"], raw["downloaded_at"])
            self.assertEqual(view["local_path"], raw["local_path"])
            self.assertEqual(db.integrity_summary()["integrity"], "ok")
            self.assertEqual(db.integrity_summary()["foreign_key_errors"], 0)

    def test_download_transitions(self):
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "reposts.sqlite"
            db = RepostDB(db_path)
            db.upsert_seen("https://x.com/a/status/1", "1")
            db.mark_downloaded("https://x.com/a/status/1", "1", [Path("/tmp/one.mp4")])
            self.assertEqual(self.row(db_path, "1")["download_status"], DOWNLOAD_DOWNLOADED)

            db.upsert_seen("https://x.com/a/status/2", "2")
            db.mark_failed("https://x.com/a/status/2", "2", "failed")
            self.assertEqual(self.row(db_path, "2")["download_status"], DOWNLOAD_FAILED)
            db.mark_downloaded("https://x.com/a/status/2", "2", [Path("/tmp/two.mp4")])
            self.assertEqual(self.row(db_path, "2")["download_status"], DOWNLOAD_DOWNLOADED)

    def test_unavailable_timestamp_is_not_overwritten_and_transient_stays_unknown(self):
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "reposts.sqlite"
            db = RepostDB(db_path)
            db.mark_downloaded("https://x.com/a/status/3", "3", [Path("/tmp/three.mp4")])
            with patch("x_repost_downloader.repost_db.utc_now", return_value="2026-01-01T00:00:00+00:00"):
                db.mark_availability("https://x.com/a/status/3", "3", AVAILABILITY_UNAVAILABLE, "deleted")
            with patch("x_repost_downloader.repost_db.utc_now", return_value="2026-01-02T00:00:00+00:00"):
                db.mark_availability("https://x.com/a/status/3", "3", AVAILABILITY_UNAVAILABLE, "deleted")
            row = self.row(db_path, "3")
            self.assertEqual(row["first_unavailable_at"], "2026-01-01T00:00:00+00:00")
            self.assertEqual(row["remote_availability"], AVAILABILITY_UNAVAILABLE)

            db.mark_availability("https://x.com/a/status/4", "4", AVAILABILITY_UNKNOWN, "rate limit")
            row = self.row(db_path, "4")
            self.assertEqual(row["remote_availability"], AVAILABILITY_UNKNOWN)
            self.assertIsNone(row["first_unavailable_at"])

    def test_backfill_uses_metadata_or_status_id_without_modifying_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            videos = root / "videos"
            metadata = root / "metadata"
            videos.mkdir()
            metadata.mkdir()
            video = videos / "2026-01-01 - a - status-55555 - title.mp4"
            orphan = videos / "unmatched.mp4"
            video.write_bytes(b"x")
            orphan.write_bytes(b"y")
            stat_before = (video.stat().st_mtime_ns, orphan.stat().st_mtime_ns)
            (metadata / "55555.info.json").write_text(
                '{"id":"55555","webpage_url":"https://x.com/source/status/55555"}',
                encoding="utf-8",
            )

            db = RepostDB(root / "reposts.sqlite")
            result = db.backfill_existing_downloads(videos, metadata)

            self.assertEqual(result.associated, 1)
            self.assertEqual(result.unassociated, 1)
            self.assertEqual((video.stat().st_mtime_ns, orphan.stat().st_mtime_ns), stat_before)
            self.assertEqual(self.row(root / "reposts.sqlite", "55555")["local_path"], str(video))

    def test_availability_error_classification_is_conservative(self):
        self.assertEqual(
            VideoDownloader._classify_availability_error("ERROR: This tweet is no longer available"),
            AVAILABILITY_UNAVAILABLE,
        )
        self.assertEqual(
            VideoDownloader._classify_availability_error("ERROR: HTTP Error 429: Too Many Requests"),
            AVAILABILITY_UNKNOWN,
        )
        self.assertEqual(
            VideoDownloader._classify_availability_error("ERROR: login required"),
            AVAILABILITY_UNKNOWN,
        )

    def test_canonical_url_normalizes_logical_duplicates(self):
        self.assertEqual(canonical_url("https://mobile.x.com/user/status/777?lang=en")[0], "https://x.com/i/status/777")


if __name__ == "__main__":
    unittest.main()

import logging
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from x_repost_downloader.config import Config
from x_repost_downloader.downloader import VideoDownloader


class DownloaderTests(unittest.TestCase):
    @patch("x_repost_downloader.downloader.shutil.which", return_value="/tmp/yt-dlp")
    def test_command_has_archive_and_best_fallback(self, _which):
        with tempfile.TemporaryDirectory() as temporary:
            downloader = VideoDownloader(Config(output_dir=Path(temporary)), logging.getLogger("test"))
            archive = Path(temporary) / "archive.txt"
            command = downloader._base_command(archive)
            self.assertIn(
                "bestvideo[height<=720]+bestaudio/best[height<=720]/bestvideo+bestaudio/best",
                command,
            )
            self.assertIn("--download-archive", command)
            self.assertIn(str(archive), command)
            self.assertIn("--write-info-json", command)
            self.assertIn("--no-overwrites", command)

    @patch("x_repost_downloader.downloader.shutil.which", return_value="/tmp/yt-dlp")
    @patch("x_repost_downloader.downloader.verify_storage")
    @patch("x_repost_downloader.downloader.subprocess.run")
    def test_error_redacts_external_url_and_does_not_commit_archive(self, run, _storage, _which):
        run.return_value = subprocess.CompletedProcess(
            [], 1, stdout="", stderr="ERROR https://video.example/signed/secret?token=value"
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            (output / "state").mkdir()
            archive = output / "state" / "download-archive.txt"
            archive.write_text("twitter existing\n", encoding="utf-8")
            downloader = VideoDownloader(Config(output_dir=output), logging.getLogger("test"))
            result = downloader.download("https://x.com/a/status/1")
            self.assertIn("external-url-redacted", result.error)
            self.assertNotIn("secret", result.error)
            self.assertEqual(archive.read_text(encoding="utf-8"), "twitter existing\n")

    @patch("x_repost_downloader.downloader.shutil.which", return_value="/tmp/tool")
    @patch("x_repost_downloader.downloader.verify_storage")
    @patch("x_repost_downloader.downloader.subprocess.run")
    def test_ffprobe_failure_does_not_commit_permanent_archive(self, run, _storage, _which):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            for name in ("videos", "metadata", "state"):
                (output / name).mkdir()
            media = output / "videos" / "status-99.mp4"
            media.write_bytes(b"not a valid video")
            archive = output / "state" / "download-archive.txt"
            archive.write_text("twitter existing\n", encoding="utf-8")
            run.return_value = subprocess.CompletedProcess(
                [],
                0,
                stdout=f"__XRD_FILE__{media}\n__XRD_ARCHIVE__twitter 99\n",
                stderr="",
            )
            downloader = VideoDownloader(Config(output_dir=output), logging.getLogger("test"))
            with patch.object(downloader, "validate_file", return_value="ffprobe failed"):
                result = downloader.download("https://x.com/a/status/100")
            self.assertEqual(result.error, "ffprobe failed")
            self.assertEqual(archive.read_text(encoding="utf-8"), "twitter existing\n")
            self.assertFalse(media.exists())

    @patch("x_repost_downloader.downloader.shutil.which", return_value="/tmp/tool")
    def test_reconciles_canonical_media_id_when_repost_wrapper_id_differs(self, _which):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            (output / "videos").mkdir()
            (output / "metadata").mkdir()
            media = output / "videos" / "2026-01-01 - author - status-222 - title.mp4"
            media.touch()
            info = {
                "id": "222",
                "ext": "mp4",
                "extractor": "twitter",
                "webpage_url": "https://x.com/original/status/222",
                "original_url": "https://x.com/original/status/222",
            }
            (output / "metadata" / "canonical.info.json").write_text(json.dumps(info), encoding="utf-8")
            downloader = VideoDownloader(Config(output_dir=output), logging.getLogger("test"))
            with patch.object(downloader, "validate_file", return_value=None):
                paths, archive_ids, error = downloader._find_and_validate_existing(
                    "https://x.com/reposter/status/111",
                    media_ids={"222"},
                )
            self.assertEqual(paths, [media])
            self.assertEqual(archive_ids, ["twitter 222"])
            self.assertIsNone(error)


if __name__ == "__main__":
    unittest.main()

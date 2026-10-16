import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from x_repost_downloader import cli
from x_repost_downloader.config import Config
from x_repost_downloader.downloader import DownloadResult
from x_repost_downloader.errors import RateLimited
from x_repost_downloader.state import ITEM_DOWNLOADED, ITEM_PENDING, State, StateStore


class FakeCrawlResult:
    reposts = []
    examined_repost_ids = set()
    scrolls = 0
    stopped_reason = "synthetic"
    technical_limit = False
    timeline_end_reached = False


class DownloadPolicyTests(unittest.TestCase):
    def args(self):
        return argparse.Namespace(command="check", verbose=False, max_scrolls=0, output_dir=None, dry_run=False, max_downloads=None)

    def prepare_state(self, output: Path, count: int) -> None:
        state = State()
        for index in range(count):
            status_id = str(100000 + index)
            state.discover(
                status_id=status_id,
                url=f"https://x.com/a/status/{status_id}",
                author="a",
                has_video=True,
                published_at=None,
            )
        StateStore(output / "state" / "state.json").save(state)

    def run_with(self, output: Path, crawler, downloader):
        config = Config(output_dir=output)
        with patch("x_repost_downloader.cli.load_config", return_value=config), \
             patch("x_repost_downloader.cli.verify_storage"), \
             patch("x_repost_downloader.browser.BrowserCrawler", return_value=crawler), \
             patch("x_repost_downloader.downloader.VideoDownloader", return_value=downloader):
            return cli._run_download_mode(self.args(), "check")

    def test_more_than_ten_pending_items_are_all_attempted(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            self.prepare_state(output, 12)
            attempted = []

            class Downloader:
                def download(self, url):
                    attempted.append(url)
                    return DownloadResult(downloaded_paths=[Path("/tmp/video.mp4")], validated=True)

            class Crawler:
                def crawl(self, *args, **kwargs):
                    return FakeCrawlResult()

            self.assertEqual(self.run_with(output, Crawler(), Downloader()), 0)
            self.assertEqual(len(attempted), 12)
            state = StateStore(output / "state" / "state.json").load()
            self.assertEqual(state.counts()[ITEM_DOWNLOADED], 12)

    def test_rate_limit_during_crawl_sets_cooldown(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            self.prepare_state(output, 2)

            class Crawler:
                def crawl(self, *args, **kwargs):
                    raise RateLimited("HTTP 429 rate limit")

            with self.assertRaises(RateLimited):
                self.run_with(output, Crawler(), object())
            state = StateStore(output / "state" / "state.json").load()
            self.assertEqual(state.last_result, "x_protection_cooldown")
            self.assertTrue(state.next_allowed_check_at)
            self.assertEqual(state.counts()[ITEM_PENDING], 2)

    def test_cooldown_noop_makes_no_x_calls(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            self.prepare_state(output, 1)
            state = StateStore(output / "state" / "state.json").load()
            state.next_allowed_check_at = "2999-01-01T00:00:00+00:00"
            StateStore(output / "state" / "state.json").save(state)

            class Crawler:
                def crawl(self, *args, **kwargs):
                    raise AssertionError("crawler should not be called")

            self.assertEqual(self.run_with(output, Crawler(), object()), 0)
            state = StateStore(output / "state" / "state.json").load()
            self.assertEqual(state.last_result, "cooldown_noop")

    def test_cooldown_increases_and_success_resets_it(self):
        state = State()
        with patch("x_repost_downloader.state.datetime") as fake_datetime:
            fake_datetime.now.return_value = __import__("datetime").datetime(2026, 1, 1, tzinfo=__import__("datetime").UTC)
            fake_datetime.fromisoformat.side_effect = __import__("datetime").datetime.fromisoformat
            state.mark_protection_blocked("HTTP 429")
            first = state.next_allowed_check_at
            state.mark_protection_blocked("HTTP 429")
            second = state.next_allowed_check_at
        self.assertLess(first, second)
        self.assertEqual(state.protection_failures_consecutive, 2)
        state.reset_protection_cooldown()
        self.assertIsNone(state.next_allowed_check_at)
        self.assertEqual(state.protection_failures_consecutive, 0)

    def test_download_protection_error_stops_and_leaves_rest_pending(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            self.prepare_state(output, 3)
            attempted = []

            class Downloader:
                def download(self, url):
                    attempted.append(url)
                    return DownloadResult(error="HTTP Error 429: Too Many Requests", validated=False)

            class Crawler:
                def crawl(self, *args, **kwargs):
                    return FakeCrawlResult()

            self.assertEqual(self.run_with(output, Crawler(), Downloader()), 0)
            self.assertEqual(len(attempted), 1)
            state = StateStore(output / "state" / "state.json").load()
            self.assertEqual(state.last_result, "x_protection_cooldown")
            self.assertEqual(state.counts()[ITEM_PENDING], 3)


if __name__ == "__main__":
    unittest.main()

import multiprocessing
import json
import tempfile
import time
import unittest
from pathlib import Path

from x_repost_downloader.errors import LockBusy
from x_repost_downloader.state import (
    ITEM_DOWNLOADED,
    ITEM_FAILED,
    ITEM_PENDING,
    State,
    StateStore,
    exclusive_lock,
)


def hold_lock(path: str, ready):
    with exclusive_lock(Path(path)):
        ready.set()
        time.sleep(2)


class StateTests(unittest.TestCase):
    def test_state_atomic_round_trip(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            store = StateStore(path)
            state = State()
            state.discover(status_id="1", url="https://x.com/a/status/1", author="a", has_video=False, published_at=None)
            state.discover(status_id="2", url="https://x.com/b/status/2", author="b", has_video=True, published_at=None)
            store.save(state)
            loaded = store.load()
            self.assertEqual(loaded.seen(), {"1", "2"})
            self.assertEqual(loaded.items["2"].status, ITEM_PENDING)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_downloaded_requires_explicit_validated_transition(self):
        state = State()
        item = state.discover(
            status_id="42",
            url="https://x.com/a/status/42",
            author="a",
            has_video=True,
            published_at="2025-01-01T00:00:00Z",
        )
        self.assertEqual(item.status, ITEM_PENDING)
        state.mark_attempt("42")
        state.mark_failed("42", "ffprobe failed")
        self.assertEqual(item.status, ITEM_FAILED)
        state.mark_attempt("42")
        state.mark_downloaded("42", [Path("video.mp4")], ["twitter 42"])
        self.assertEqual(item.status, ITEM_DOWNLOADED)
        self.assertEqual(item.attempts, 2)

    def test_schema_one_state_migrates_without_losing_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            path.write_text(
                json.dumps({"seen_repost_ids": ["1", "2"], "failed_video_ids": ["2"]}),
                encoding="utf-8",
            )
            state = StateStore(path).load()
            self.assertEqual(state.schema, 2)
            self.assertEqual(state.seen(), {"1", "2"})
            self.assertEqual(state.items["2"].status, ITEM_FAILED)

    def test_lock_rejects_second_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "run.lock"
            ready = multiprocessing.Event()
            process = multiprocessing.Process(target=hold_lock, args=(str(path), ready))
            process.start()
            self.assertTrue(ready.wait(5))
            try:
                with self.assertRaises(LockBusy):
                    with exclusive_lock(path):
                        pass
            finally:
                process.join(5)


if __name__ == "__main__":
    unittest.main()

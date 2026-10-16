import tempfile
import unittest
from pathlib import Path

from x_repost_downloader.config import Config, load_config, save_config, ensure_layout


class ConfigTests(unittest.TestCase):
    def test_round_trip_and_permissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            expected = Config(output_dir=Path(temporary) / "output", max_scrolls_check=7)
            save_config(expected, path)
            actual = load_config(path)
            self.assertEqual(actual.output_dir, expected.output_dir)
            self.assertEqual(actual.max_scrolls_check, 7)
            self.assertEqual(actual.max_downloads_per_run, 10)
            self.assertEqual(actual.availability_recheck_days, 7)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_default_storage_is_local_without_a_specific_device(self):
        config = Config()
        self.assertEqual(config.output_dir, Path.home() / ".local/share/x-repost-downloader/archive")
        self.assertEqual(config.required_mount, Path("/"))

    def test_private_external_mount_guard_still_refuses_absent_mount(self):
        with tempfile.TemporaryDirectory() as temporary:
            mount = Path(temporary) / "absent-mount"
            with self.assertRaisesRegex(RuntimeError, "not mounted"):
                ensure_layout(Config(output_dir=mount / "archive", required_mount=mount))
            self.assertFalse(mount.exists())

    def test_cookie_spec_contains_no_credentials(self):
        config = Config(browser_profile=Path("/tmp/Profile 1"))
        self.assertEqual(config.cookie_browser_spec, "chrome+gnomekeyring:/tmp/Profile 1")


if __name__ == "__main__":
    unittest.main()

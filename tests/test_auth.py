import unittest

from x_repost_downloader.auth import _unix_expiry


class AuthTests(unittest.TestCase):
    def test_chromium_webkit_timestamp_converts_to_unix(self):
        self.assertAlmostEqual(_unix_expiry(13_437_000_000_000_000), 1_792_526_400)

    def test_unix_timestamp_is_unchanged(self):
        self.assertEqual(_unix_expiry(1_800_000_000), 1_800_000_000)

    def test_session_cookie_remains_without_expiry(self):
        self.assertIsNone(_unix_expiry(None))


if __name__ == "__main__":
    unittest.main()

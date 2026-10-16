import unittest

from x_repost_downloader.logging_utils import redact, redact_external_urls


class LoggingTests(unittest.TestCase):
    def test_secret_assignment_redacted(self):
        self.assertNotIn("abcdef", redact("auth_token=abcdef"))

    def test_external_signed_url_redacted(self):
        result = redact_external_urls("ERROR https://cdn.example/path-secret?token=abcdef")
        self.assertNotIn("path-secret", result)
        self.assertNotIn("abcdef", result)


if __name__ == "__main__":
    unittest.main()

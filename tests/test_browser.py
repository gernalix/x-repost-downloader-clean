import unittest

from x_repost_downloader.browser import BrowserCrawler, parse_graphql_reposts, parse_status_href
from x_repost_downloader.config import Config
from x_repost_downloader.errors import AuthenticationRequired


class BrowserTests(unittest.TestCase):
    def test_parse_canonical_status(self):
        self.assertEqual(parse_status_href("/SomeAuthor/status/123456?ref=abc"), ("SomeAuthor", "123456"))
        self.assertEqual(parse_status_href("https://x.com/a/status/42/video/1"), ("a", "42"))

    def test_reject_non_status(self):
        self.assertIsNone(parse_status_href("/LivingGaul/media"))

    def test_parse_graphql_repost_uses_original_canonical_url_and_video_media(self):
        payload = {
            "data": {
                "entry": {
                    "rest_id": "900",
                    "core": {"user_results": {"result": {"legacy": {"screen_name": "LivingGaul"}}}},
                    "legacy": {
                        "retweeted_status_result": {
                            "result": {
                                "rest_id": "800",
                                "core": {
                                    "user_results": {
                                        "result": {"legacy": {"screen_name": "OriginalAuthor"}}
                                    }
                                },
                                "legacy": {
                                    "created_at": "Mon Jan 01 12:00:00 +0000 2024",
                                    "extended_entities": {"media": [{"type": "video"}]},
                                },
                            }
                        }
                    },
                }
            }
        }
        reposts = parse_graphql_reposts(payload, "LivingGaul")
        self.assertEqual(len(reposts), 1)
        self.assertEqual(reposts[0].status_id, "800")
        self.assertEqual(reposts[0].url, "https://x.com/OriginalAuthor/status/800")
        self.assertTrue(reposts[0].has_video)

    def test_expired_session_simulation_preserves_state_layer(self):
        class Page:
            url = "https://x.com/i/flow/login"

        crawler = BrowserCrawler(Config(), logger=None)
        with self.assertRaises(AuthenticationRequired):
            crawler._check_page_state(Page())


if __name__ == "__main__":
    unittest.main()

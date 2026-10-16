"""Synthetic DOM regressions: no real account, media, yt-dlp or network."""
import subprocess
import sys
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright

from x_repost_downloader.capsules.timeline import article_identity, parse_status_href, select_repost_timeline
from x_repost_downloader.capsules.timeline.diagnostic import block_request


class TimelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(executable_path="/usr/bin/chromium-browser", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.route("**/*", lambda route: route.fulfill(content_type="text/html", body=self.html))

    def tearDown(self):
        self.page.close()

    def load(self, html):
        self.html = html
        self.page.goto("https://x.com/Example")

    def test_new_separate_tab_changes_feed_and_recognizes_reposts(self):
        self.load('''<a role="tab" href="/Example" aria-selected="true">Posts</a>
          <a role="tab" href="/Example/reposts" aria-selected="false">Reposts</a>
          <article data-testid="tweet"><a href="/Example/status/1"><time></time></a></article>
          <script>document.querySelectorAll('a[role=tab]')[1].onclick = e => {
            e.preventDefault(); history.pushState({}, '', '/Example/reposts');
            document.querySelector('[aria-selected=true]').setAttribute('aria-selected','false');
            e.currentTarget.setAttribute('aria-selected','true');
            document.querySelector('article').innerHTML = '<span data-testid="socialContext">Example reposted</span><a href="/Author/status/2"><time></time></a>';
          };</script>''')
        article = self.page.locator("article")
        self.assertIsNone(article_identity(article, "Example"))
        self.assertEqual(select_repost_timeline(self.page, "Example"), "reposts_tab")
        self.assertEqual(article_identity(article, "Example"), ("Author", "2"))

    def test_legacy_mixed_timeline_keeps_social_context_discovery(self):
        self.load('''<a role="tab" href="/Example" aria-selected="true">Posts</a>
          <article><span data-testid="socialContext">Example reposted</span>
          <a href="/Author/status/3"><time></time></a></article>''')
        self.assertEqual(select_repost_timeline(self.page, "Example"), "legacy_mixed")
        self.assertEqual(article_identity(self.page.locator("article"), "Example"), ("Author", "3"))

    def test_nested_quote_cannot_supply_parent_identity_or_repost_marker(self):
        self.load('''<article id="outer"><a href="/Writer/status/4"><time></time></a>
          <article><span data-testid="socialContext">Example reposted</span>
          <a href="/Quoted/status/5"><time></time></a></article></article>''')
        self.assertIsNone(article_identity(self.page.locator("#outer"), "Example"))
        self.page.locator("#outer").evaluate("a => a.insertAdjacentHTML('afterbegin', '<span data-testid=\"socialContext\">Example reposted</span>')")
        self.assertEqual(article_identity(self.page.locator("#outer"), "Example"), ("Writer", "4"))

    def test_pinned_liked_or_button_labels_do_not_count_as_reposts(self):
        for header in ("Pinned", "Example liked", "Example"):
            self.load(f'''<article><span data-testid="socialContext">{header}</span>
              <a href="/Author/status/6"><time></time></a><button>Repost</button></article>''')
            self.assertIsNone(article_identity(self.page.locator("article"), "Example"))

    def test_external_status_is_not_an_x_status(self):
        self.assertIsNone(parse_status_href("https://unrelated.example/Author/status/7"))

    def test_media_block_includes_xhr_playlists_and_unknown_hosts(self):
        for kind, url in (
            ("image", "https://x.com/asset"), ("media", "https://x.com/asset"),
            ("xhr", "https://video.twimg.com/playlist"),
            ("fetch", "https://x.com/example.m3u8"),
            ("fetch", "https://unknown.example/opaque"),
        ):
            self.assertTrue(block_request(kind, url))
        self.assertFalse(block_request("script", "https://abs.twimg.com/client.js"))
        self.assertFalse(block_request("xhr", "https://x.com/i/api/graphql/id/UserRepostsTimeline"))

    def test_cli_diagnostic_imports_no_downloader(self):
        source = str(Path(__file__).resolve().parents[1] / "src")
        script = f'''import site,sys
sys.path.extend(site.getsitepackages(['/usr/local']))
sys.path.insert(0,{source!r})
from x_repost_downloader.cli import build_parser
from x_repost_downloader.capsules.timeline.diagnostic import run_diagnostic
assert build_parser().parse_args(['diagnose']).command == 'diagnose'
assert 'yt_dlp' not in sys.modules
assert 'x_repost_downloader.downloader' not in sys.modules
'''
        subprocess.run([sys.executable, "-I", "-B", "-c", script], check=True)


if __name__ == "__main__":
    unittest.main()

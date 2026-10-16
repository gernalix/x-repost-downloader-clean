"""Profile navigation and post identity; never reads media or tweet bodies."""
from __future__ import annotations

import re
from urllib.parse import urlparse

STATUS_PATH = re.compile(r"^/([^/]+)/status/(\d+)(?:/|$)", re.IGNORECASE)
TWEET_SELECTOR = 'article[data-testid="tweet"]'
TIMELINE_OPERATIONS = frozenset({"UserTweets", "UserTweetsAndReplies", "UserMedia", "UserRepostsTimeline"})


def parse_status_href(href: str | None) -> tuple[str, str] | None:
    if not href:
        return None
    parsed = urlparse(href)
    if parsed.netloc and parsed.hostname not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
        return None
    match = STATUS_PATH.match(parsed.path)
    return (match.group(1), match.group(2)) if match else None


def select_repost_timeline(page, account: str) -> str:
    """Follow the actual Reposts tab, retaining support for the old mixed feed.

    X now defaults profiles to UserOriginalsTimeline. A missing separate tab
    means legacy layout; a present tab that fails navigation is an error.
    """
    page.locator('[role="tab"]').first.wait_for(state="attached", timeout=12_000)
    wanted = f"/{account}/reposts".casefold()
    tabs = page.locator('[role="tab"][href]')
    for index in range(tabs.count()):
        tab = tabs.nth(index)
        href = tab.get_attribute("href") or ""
        parsed = urlparse(href)
        if parsed.netloc and parsed.hostname != "x.com":
            continue
        if parsed.path.rstrip("/").casefold() != wanted:
            continue
        if tab.get_attribute("aria-selected") != "true":
            tab.click()
        page.wait_for_url(lambda u: urlparse(u).path.rstrip("/").casefold() == wanted)
        tab.wait_for(state="attached")
        # Wait for selection as well as the URL before reading the new feed.
        page.wait_for_function(
            "path => [...document.querySelectorAll('[role=tab][aria-selected=true]')]"
            ".some(t => new URL(t.href, location.href).pathname.replace(/\\/$/, '').toLowerCase() === path)",
            arg=wanted,
        )
        return "reposts_tab"
    return "legacy_mixed"


def article_identity(article, account: str) -> tuple[str, str] | None:
    """Read status links and the social header, excluding nested quoted posts."""
    metadata = article.evaluate(r"""a => {
        const own = e => e.closest('article') === a;
        const links = [...a.querySelectorAll('a:has(time),a[href*="/status/"]')].filter(own);
        const timestamp = links.find(e => e.querySelector('time'));
        const context = [...a.querySelectorAll('[data-testid="socialContext"]')].find(own);
        return {href: (timestamp || links[0])?.getAttribute('href') || null,
                social: context?.textContent || ''};
    }""")
    status = parse_status_href(metadata["href"])
    if not status or status[0].casefold() == account.casefold():
        return None
    if not re.search(r"\b(?:repost(?:ed)?|retweet(?:ed)?|ha ripostato|ha ripubblicato)\b", metadata["social"], re.I):
        return None
    return status

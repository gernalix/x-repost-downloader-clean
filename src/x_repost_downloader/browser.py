from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field
from datetime import UTC
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Error as PlaywrightError, Page, TimeoutError, sync_playwright

from .auth import clear_cookie_values, load_x_cookies
from .capsules.timeline import (
    TIMELINE_OPERATIONS, TWEET_SELECTOR, article_identity, parse_status_href, select_repost_timeline,
)
from .config import Config
from .errors import AuthenticationRequired, BrowserUnavailable, CheckpointRequired, RateLimited

VIDEO_SELECTOR = '[data-testid="videoPlayer"], [data-testid="videoComponent"], video'


@dataclass(frozen=True, slots=True)
class Repost:
    status_id: str
    url: str
    author: str
    has_video: bool
    published_at: str | None = None


@dataclass(slots=True)
class CrawlResult:
    reposts: list[Repost] = field(default_factory=list)
    examined_repost_ids: set[str] = field(default_factory=set)
    scrolls: int = 0
    stopped_reason: str = "max_scrolls"
    authenticated: bool = False
    timeline_end_reached: bool = False
    technical_limit: bool = False
    checkpoints: int = 0


def _top_level_status(article) -> tuple[str, str] | None:
    timestamp_links = article.locator("a:has(time)")
    for index in range(timestamp_links.count()):
        parsed = parse_status_href(timestamp_links.nth(index).get_attribute("href"))
        if parsed:
            return parsed
    links = article.locator('a[href*="/status/"]')
    for index in range(links.count()):
        parsed = parse_status_href(links.nth(index).get_attribute("href"))
        if parsed:
            return parsed
    return None


def classify_article(article, account: str) -> Repost | None:
    status = article_identity(article, account)
    if not status:
        return None
    author, status_id = status
    if author.casefold() == account.casefold():
        return None
    has_video = article.locator(VIDEO_SELECTOR).count() > 0
    published_at = None
    times = article.locator("a:has(time) time")
    if times.count() > 0:
        published_at = times.first.get_attribute("datetime")
    return Repost(
        status_id=status_id,
        url=f"https://x.com/{author}/status/{status_id}",
        author=author,
        has_video=has_video,
        published_at=published_at,
    )


def _screen_name(result: object) -> str | None:
    if not isinstance(result, dict):
        return None
    legacy = result.get("legacy") or {}
    core = result.get("core") or {}
    return legacy.get("screen_name") or core.get("screen_name")


def _unwrap_tweet_result(result: object) -> dict | None:
    if not isinstance(result, dict):
        return None
    tweet = result.get("tweet")
    return tweet if isinstance(tweet, dict) else result


def _walk_graphql_tweets(value: object):
    if isinstance(value, dict):
        rest_id = value.get("rest_id")
        if isinstance(rest_id, str) and rest_id.isdigit() and isinstance(value.get("legacy"), dict):
            yield value
        for child in value.values():
            yield from _walk_graphql_tweets(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_graphql_tweets(child)


def parse_graphql_reposts(payload: object, account: str) -> list[Repost]:
    """Extract reposted originals from authenticated UserTweets responses."""
    found: dict[str, Repost] = {}
    for wrapper in _walk_graphql_tweets(payload):
        wrapper_user = (((wrapper.get("core") or {}).get("user_results") or {}).get("result") or {})
        if (_screen_name(wrapper_user) or "").casefold() != account.casefold():
            continue
        wrapper_legacy = wrapper.get("legacy") or {}
        original = _unwrap_tweet_result(
            (wrapper_legacy.get("retweeted_status_result") or {}).get("result")
        )
        if not original:
            continue
        status_id = str(original.get("rest_id") or "")
        if not status_id.isdigit():
            continue
        original_user = (((original.get("core") or {}).get("user_results") or {}).get("result") or {})
        author = _screen_name(original_user)
        if not author:
            continue
        legacy = original.get("legacy") or {}
        media = (legacy.get("extended_entities") or {}).get("media") or []
        has_video = any(
            isinstance(item, dict) and item.get("type") in {"video", "animated_gif"}
            for item in media
        )
        published_at = None
        if legacy.get("created_at"):
            try:
                published_at = parsedate_to_datetime(legacy["created_at"]).astimezone(UTC).isoformat()
            except (TypeError, ValueError):
                pass
        found[status_id] = Repost(
            status_id=status_id,
            url=f"https://x.com/{author}/status/{status_id}",
            author=author,
            has_video=has_video,
            published_at=published_at,
        )
    return list(found.values())


class BrowserCrawler:
    def __init__(self, config: Config, logger):
        self.config = config
        self.logger = logger

    def _check_page_state(self, page: Page) -> None:
        current = page.url.casefold()
        if any(marker in current for marker in ("/i/flow/login", "/login")):
            raise AuthenticationRequired("X redirected the browser to the login flow")
        if "/account/access" in current or "/i/flow/challenge" in current:
            raise CheckpointRequired("X requires manual account verification")
        body = ""
        try:
            body = page.locator("body").inner_text(timeout=3_000).casefold()
        except PlaywrightError:
            pass
        if any(marker in body for marker in ("rate limit exceeded", "limite di frequenza", "rate limit")):
            raise RateLimited("X is rate limiting timeline access")
        if any(marker in body for marker in ("verify your identity", "verifica la tua identità", "captcha")):
            raise CheckpointRequired("X requires manual verification; no bypass was attempted")

    def _verify_authenticated(self, page: Page) -> None:
        self._check_page_state(page)
        try:
            page.locator('[data-testid="SideNav_AccountSwitcher_Button"]').first.wait_for(
                state="attached", timeout=12_000
            )
        except TimeoutError as error:
            self._check_page_state(page)
            raise AuthenticationRequired("X did not expose an authenticated account session") from error

    def crawl(
        self,
        seen_ids: set[str],
        max_scrolls: int | None,
        backfill: bool = False,
        on_repost: Callable[[Repost], None] | None = None,
        on_checkpoint: Callable[[CrawlResult], None] | None = None,
    ) -> CrawlResult:
        cookies: list[dict[str, object]] = []
        if not self.config.auth_method.startswith("dedicated"):
            cookies, summary = load_x_cookies(self.config)
            self.logger.debug("Loaded %d X-scoped cookies in memory", summary.total_x_cookies)
        result = CrawlResult()
        session_status_ids: set[str] = set()
        session_repost_ids: set[str] = set()
        stagnant = 0
        consecutive_known = 0
        try:
            with sync_playwright() as playwright:
                browser = None
                try:
                    if self.config.auth_method.startswith("dedicated"):
                        context = playwright.chromium.launch_persistent_context(
                            user_data_dir=str(self.config.dedicated_profile),
                            executable_path=str(self.config.chromium_executable),
                            headless=True,
                            locale="en-US",
                            viewport={"width": 1280, "height": 1000},
                            service_workers="block",
                        )
                    else:
                        browser = playwright.chromium.launch(
                            executable_path=str(self.config.chromium_executable), headless=True
                        )
                        context = browser.new_context(
                            locale="en-US",
                            viewport={"width": 1280, "height": 1000},
                            service_workers="block",
                        )
                except PlaywrightError as error:
                    raise BrowserUnavailable("unable to launch the configured Chromium executable") from error
                try:
                    if cookies:
                        context.add_cookies(cookies)
                    page = context.new_page()
                    network_reposts: dict[str, Repost] = {}

                    def capture_timeline_response(response) -> None:
                        path = urlparse(response.url).path
                        if "/graphql/" not in path:
                            return
                        operation = path.rsplit("/", 1)[-1]
                        if operation not in TIMELINE_OPERATIONS:
                            return
                        try:
                            payload = response.json()
                        except (PlaywrightError, ValueError):
                            return
                        for repost in parse_graphql_reposts(payload, self.config.account):
                            network_reposts[repost.status_id] = repost

                    page.on("response", capture_timeline_response)
                    page.set_default_timeout(self.config.navigation_timeout_seconds * 1000)
                    try:
                        page.goto(
                            self.config.profile_url,
                            wait_until="domcontentloaded",
                            timeout=self.config.navigation_timeout_seconds * 1000,
                        )
                    except TimeoutError:
                        self.logger.warning("Initial X navigation timed out; inspecting the loaded page")
                    self._verify_authenticated(page)
                    result.authenticated = True
                    timeline = select_repost_timeline(page, self.config.account)
                    self.logger.info("Profile timeline selected: %s", timeline)
                    self._check_page_state(page)
                    try:
                        page.locator(TWEET_SELECTOR).first.wait_for(state="attached", timeout=30_000)
                    except TimeoutError as error:
                        self._check_page_state(page)
                        raise BrowserUnavailable(
                            "the profile timeline did not load; X may be unavailable or the sensitive-content setting may block it"
                        ) from error

                    stop_requested = False
                    scroll_index = 0
                    stagnant_limit = self.config.backfill_stagnant_scrolls if backfill else self.config.stagnant_scrolls

                    def accept_repost(repost: Repost, source: str) -> bool:
                        nonlocal consecutive_known
                        if repost.status_id in session_repost_ids:
                            previous = next(
                                item for item in result.reposts if item.status_id == repost.status_id
                            )
                            if repost.has_video and not previous.has_video:
                                result.reposts[result.reposts.index(previous)] = repost
                                if on_repost:
                                    on_repost(repost)
                                self.logger.debug(
                                    "Repost status=%s upgraded to video source=%s",
                                    repost.status_id,
                                    source,
                                )
                            return False
                        session_repost_ids.add(repost.status_id)
                        result.examined_repost_ids.add(repost.status_id)
                        result.reposts.append(repost)
                        if on_repost:
                            on_repost(repost)
                        if repost.status_id in seen_ids:
                            consecutive_known += 1
                        else:
                            consecutive_known = 0
                        self.logger.debug(
                            "Repost status=%s author=%s video=%s known=%s source=%s",
                            repost.status_id,
                            repost.author,
                            repost.has_video,
                            repost.status_id in seen_ids,
                            source,
                        )
                        return not backfill and consecutive_known >= self.config.known_reposts_stop

                    while True:
                        self._check_page_state(page)
                        new_statuses_this_screen = 0
                        articles = page.locator(TWEET_SELECTOR)
                        social_context_articles = 0
                        video_articles = 0
                        classified_reposts = 0
                        for repost in list(network_reposts.values()):
                            if repost.status_id not in session_repost_ids:
                                new_statuses_this_screen += 1
                                classified_reposts += 1
                            if accept_repost(repost, "graphql"):
                                result.stopped_reason = "known_reposts_reached"
                                stop_requested = True
                                break
                        if stop_requested:
                            result.checkpoints += 1
                            if on_checkpoint:
                                on_checkpoint(result)
                            break

                        for index in range(articles.count()):
                            article = articles.nth(index)
                            if article.locator('[data-testid="socialContext"]').count() > 0:
                                social_context_articles += 1
                                if scroll_index == 0:
                                    social_element = article.locator('[data-testid="socialContext"]').first
                                    context_links = social_element.locator("a[href]")
                                    link_paths = []
                                    for link_index in range(context_links.count()):
                                        link_paths.append(
                                            urlparse(context_links.nth(link_index).get_attribute("href") or "").path
                                        )
                                    context_text = re.sub(r"\s+", "", social_element.inner_text()).casefold()
                                    self.logger.debug(
                                        "Social context signature own_path=%s descendant_paths=%s "
                                        "contains_account=%s contains_repost=%s",
                                        urlparse(social_element.get_attribute("href") or "").path,
                                        link_paths,
                                        self.config.account.casefold() in context_text,
                                        "repost" in context_text or "retweet" in context_text,
                                    )
                            if article.locator(VIDEO_SELECTOR).count() > 0:
                                video_articles += 1
                            top_status = _top_level_status(article)
                            if top_status and top_status[1] not in session_status_ids:
                                session_status_ids.add(top_status[1])
                                new_statuses_this_screen += 1

                            repost = classify_article(article, self.config.account)
                            if not repost:
                                continue
                            if repost.status_id not in session_repost_ids:
                                classified_reposts += 1
                            if accept_repost(repost, "dom"):
                                result.stopped_reason = "known_reposts_reached"
                                stop_requested = True
                                break

                        self.logger.debug(
                            "Timeline screen=%d articles=%d social_context=%d video_articles=%d "
                            "new_statuses=%d classified_reposts=%d",
                            scroll_index,
                            articles.count(),
                            social_context_articles,
                            video_articles,
                            new_statuses_this_screen,
                            classified_reposts,
                        )

                        result.checkpoints += 1
                        if on_checkpoint:
                            on_checkpoint(result)

                        if stop_requested:
                            break
                        if new_statuses_this_screen == 0:
                            stagnant += 1
                        else:
                            stagnant = 0
                        if page.locator('[data-testid="emptyState"]').count() > 0 and scroll_index > 0:
                            result.stopped_reason = "timeline_end_explicit"
                            result.timeline_end_reached = True
                            break
                        if stagnant >= stagnant_limit:
                            result.stopped_reason = f"x_timeline_no_progress_after_{stagnant_limit}_attempts"
                            result.technical_limit = True
                            break
                        if max_scrolls is not None and scroll_index >= max_scrolls:
                            result.stopped_reason = "user_scroll_limit"
                            break
                        if new_statuses_this_screen == 0:
                            page.mouse.wheel(0, -350)
                            time.sleep(0.5)
                        page.evaluate(
                            "window.scrollBy({top: Math.max(600, window.innerHeight * 0.85), behavior: 'instant'})"
                        )
                        result.scrolls += 1
                        scroll_index += 1
                        time.sleep(random.uniform(self.config.min_pause_seconds, self.config.max_pause_seconds))
                    return result
                finally:
                    context.close()
                    if browser is not None:
                        browser.close()
        finally:
            clear_cookie_values(cookies)


def interactive_dedicated_login(config: Config) -> None:
    profile_root = config.dedicated_profile
    profile_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    profile_root.chmod(0o700)
    with sync_playwright() as playwright:
        context: BrowserContext = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_root),
            executable_path=str(config.chromium_executable),
            headless=False,
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://x.com/login", wait_until="domcontentloaded", timeout=45_000)
            input(
                "Accedi manualmente direttamente a X con username/email e password (non usare Continua con Google), "
                "verifica che i contenuti sensibili siano visibili, "
                "poi torna qui e premi Invio: "
            )
        finally:
            context.close()
    for path in (profile_root, profile_root / "Default"):
        if path.exists():
            path.chmod(0o700)


def initialize_dedicated_profile_from_chrome(
    source_config: Config, dedicated_config: Config, logger
) -> tuple[int, CrawlResult]:
    cookies, summary = load_x_cookies(source_config)
    profile_root = dedicated_config.dedicated_profile
    profile_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    profile_root.chmod(0o700)
    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_root),
                executable_path=str(dedicated_config.chromium_executable),
                headless=True,
                locale="en-US",
                viewport={"width": 1280, "height": 1000},
                service_workers="block",
            )
            try:
                context.add_cookies(cookies)
                imported_domains = {item["domain"].lstrip(".").casefold() for item in cookies}
                if not all(
                    domain in {"x.com", "twitter.com"}
                    or domain.endswith(".x.com")
                    or domain.endswith(".twitter.com")
                    for domain in imported_domains
                ):
                    raise AuthenticationRequired("the filtered import contained a non-X cookie domain")
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(
                    dedicated_config.profile_url,
                    wait_until="domcontentloaded",
                    timeout=dedicated_config.navigation_timeout_seconds * 1000,
                )
                BrowserCrawler(dedicated_config, logger)._verify_authenticated(page)
                page.locator(TWEET_SELECTOR).first.wait_for(state="attached", timeout=30_000)
                if f"/{dedicated_config.account}".casefold() not in urlparse(page.url).path.casefold():
                    raise AuthenticationRequired("the imported session did not remain on the requested X profile")
            finally:
                context.close()
        for path in (profile_root, profile_root / "Default"):
            if path.exists():
                path.chmod(0o700)
        # A fresh process/context proves the session was actually persisted and
        # is not merely surviving in the importer's memory.
        reopened = BrowserCrawler(dedicated_config, logger).crawl(set(), max_scrolls=3)
        return summary.total_x_cookies, reopened
    finally:
        clear_cookie_values(cookies)

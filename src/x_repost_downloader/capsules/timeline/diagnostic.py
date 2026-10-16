"""Bounded DOM-only diagnostic, isolated from downloader, archive and state."""
from __future__ import annotations

from collections import Counter
import json
import os
import re
import sys
import time
from urllib.parse import urlsplit

from playwright.sync_api import TimeoutError, sync_playwright

from . import TWEET_SELECTOR, article_identity, parse_status_href, select_repost_timeline


MEDIA_SUFFIX = re.compile(r"\.(?:mp4|m3u8|m4s|ts|webm|mp3|m4a|ogg|wav|jpg|jpeg|png|gif|webp|avif|svg|ico)(?:$|/)", re.I)


def block_request(resource_type: str, url: str) -> bool:
    parsed = urlsplit(url)
    # X's media CDN also uses xhr for playlists, so resource type alone is insufficient.
    if resource_type in {"image", "media", "font"} or MEDIA_SUFFIX.search(parsed.path):
        return True
    host = parsed.hostname or ""
    if host in {"pbs.twimg.com", "video.twimg.com", "ton.twitter.com"}:
        return True
    return not (host in {"x.com", "abs.twimg.com", "api.x.com", "api.twitter.com"})


def page_state(page) -> str:
    path = urlsplit(page.url).path.lower()
    if "/login" in path or page.locator('input[autocomplete="username"],input[name="password"]').count():
        return "login"
    if any(part in path for part in ("/account/access", "/i/flow/challenge")):
        return "challenge"
    if page.get_by_text(re.compile(r"^(Something went wrong|Try reloading|Rate limit exceeded|You are offline)", re.I)).count():
        return "error"
    if page.locator('[data-testid="SideNav_AccountSwitcher_Button"]').count():
        return "authenticated"
    return "unknown"


def run_diagnostic(config, max_scrolls: int | None = None) -> int:
    limit = 4 if max_scrolls is None else max_scrolls
    if not 0 <= limit <= 24:
        raise ValueError("diagnose --max-scrolls must be between 0 and 24")
    profile = config.dedicated_profile
    report = {
        "mode": "diagnose", "uid": os.getuid(), "profile_exists": profile.is_dir(),
        "profile_readable": os.access(profile, os.R_OK | os.X_OK),
        "profile_database_readable": False, "auth_cookie_pair": False,
        "authenticated": False, "page_state": "unknown", "visited": 0,
        "status_count": 0, "article_count": 0, "samples": [], "media_downloads": 0,
        "output_dir": str(config.output_dir), "state_written": False,
    }
    blocked, failures, errors, operations = Counter(), Counter(), Counter(), Counter()
    console_errors = []
    reposts, statuses = set(), set()
    page = None
    try:
        if not config.auth_method.startswith("dedicated"):
            raise ValueError("diagnose requires an existing dedicated Chromium session")
        if not report["profile_exists"] or not report["profile_readable"]:
            raise PermissionError("dedicated profile is missing or unreadable")
        for relative in ("Default/Cookies", "Default/Network/Cookies"):
            path = profile / relative
            if path.is_file():
                descriptor = os.open(path, os.O_RDONLY)
                os.close(descriptor)
                report["profile_database_readable"] = True
                break
        if not report["profile_database_readable"]:
            raise PermissionError("dedicated session database is missing or unreadable")
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile), executable_path=str(config.chromium_executable),
                headless=True, locale="en-US", viewport={"width": 1280, "height": 1000},
                service_workers="block", accept_downloads=False,
            )
            try:
                def route_request(route):
                    request = route.request
                    if block_request(request.resource_type, request.url):
                        blocked[request.resource_type] += 1
                        route.abort()
                    else:
                        route.continue_()

                context.route("**/*", route_request)
                context.add_init_script("HTMLMediaElement.prototype.play = function() { return Promise.resolve(); };")
                cookies = context.cookies(["https://x.com"])
                names = {c["name"] for c in cookies if c.get("expires", -1) == -1 or c.get("expires", 0) > time.time()}
                report["auth_cookie_pair"] = {"auth_token", "ct0"} <= names
                for cookie in cookies:
                    cookie["value"] = ""
                cookies.clear()
                page = context.new_page()
                page.set_default_timeout(config.navigation_timeout_seconds * 1000)

                def response_status(response):
                    path = urlsplit(response.url).path
                    if response.status >= 400:
                        errors[str(response.status)] += 1
                    if "/graphql/" in path:
                        operation = path.rsplit("/", 1)[-1]
                        if re.fullmatch(r"[A-Za-z0-9_]{1,80}", operation):
                            operations[f"{operation}:{response.status}"] += 1
                    # Do not read response bodies, media metadata, tokens or headers.

                page.on("response", response_status)
                page.on("requestfailed", lambda request: failures.update([
                    "blocked" if block_request(request.resource_type, request.url) else "network"
                ]))
                page.on("console", lambda message: console_errors.append(1) if message.type == "error" else None)
                try:
                    response = page.goto(config.profile_url, wait_until="domcontentloaded")
                    report["http_status"] = response.status if response else None
                    report["navigation"] = "success"
                except TimeoutError:
                    report["navigation"] = "timeout"
                try:
                    page.locator('[data-testid="SideNav_AccountSwitcher_Button"]').wait_for(state="attached", timeout=12_000)
                except TimeoutError:
                    pass
                report["page_state"] = page_state(page)
                if report["page_state"] == "authenticated":
                    report["authenticated"] = True
                    report["timeline"] = select_repost_timeline(page, config.account)
                    page.locator(TWEET_SELECTOR).first.wait_for(state="attached", timeout=30_000)
                    for step in range(limit + 1):
                        page.wait_for_timeout(2000)
                        report["page_state"] = page_state(page)
                        if report["page_state"] != "authenticated":
                            break
                        articles = page.locator(TWEET_SELECTOR)
                        count = articles.count()
                        report["article_count"] = max(report["article_count"], count)
                        for index in range(count):
                            article = articles.nth(index)
                            identity = article_identity(article, config.account)
                            if identity:
                                reposts.add(identity[1])
                        # Counts only; quoted status IDs are not treated as reposts.
                        hrefs = articles.locator('a[href*="/status/"]').evaluate_all(
                            "links => links.map(a => a.getAttribute('href'))"
                        )
                        statuses.update(parsed[1] for href in hrefs if (parsed := parse_status_href(href)))
                        report["samples"].append({"step": step, "articles": count, "visited": len(reposts)})
                        if step < limit:
                            page.evaluate("window.scrollBy({top: Math.max(600, innerHeight * .85), behavior: 'instant'})")
                current = urlsplit(page.url)
                report["final_url"] = f"{current.scheme}://{current.netloc}{current.path}"
                report["title"] = page.title()
            finally:
                context.close()
    except Exception as error:
        # Playwright exception strings can contain page data: expose only the type.
        report["error_type"] = type(error).__name__
    report.update(authenticated=report["page_state"] == "authenticated", visited=len(reposts), status_count=len(statuses), blocked_requests=dict(blocked),
                  request_failures=dict(failures), http_errors=dict(errors), api_status=dict(operations),
                  console_errors=len(console_errors), yt_dlp_imported="yt_dlp" in sys.modules)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
    if report["page_state"] == "login":
        return 20
    if report["page_state"] == "challenge":
        return 22
    return 0 if (report["visited"] > 0 and report["page_state"] == "authenticated"
                 and not report.get("error_type") and not report["yt_dlp_imported"]) else 1

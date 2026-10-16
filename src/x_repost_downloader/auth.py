from __future__ import annotations

import contextlib
import http.cookiejar
import time
from dataclasses import dataclass

from yt_dlp import YoutubeDL

from .config import Config
from .errors import AuthenticationRequired, BrowserProfileLocked


class SilentYtDlpLogger:
    def debug(self, message: str) -> None:
        return None

    def warning(self, message: str) -> None:
        return None

    def error(self, message: str) -> None:
        return None


@dataclass(frozen=True, slots=True)
class CookieSummary:
    total_x_cookies: int
    has_authentication_cookie: bool


def _load_cookie_jar(config: Config) -> http.cookiejar.CookieJar:
    options = {
        "cookiesfrombrowser": (
            config.browser_name,
            str(config.browser_profile),
            config.keyring.upper() if config.keyring else None,
            None,
        ),
        "logger": SilentYtDlpLogger(),
        "quiet": True,
        "no_warnings": True,
    }
    try:
        with YoutubeDL(options) as downloader:
            return downloader.cookiejar
    except Exception as error:
        detail = str(error).casefold()
        if any(marker in detail for marker in ("database is locked", "could not copy", "resource busy")):
            raise BrowserProfileLocked(
                "Chrome is keeping its cookie database locked; the browser was not closed automatically"
            ) from error
        raise AuthenticationRequired("unable to read the configured browser session") from error


def _is_x_domain(domain: str) -> bool:
    normalized = domain.lstrip(".").lower()
    return normalized in {"x.com", "twitter.com"} or normalized.endswith(".x.com") or normalized.endswith(".twitter.com")


def _unix_expiry(expires: int | float | None) -> float | None:
    if expires is None:
        return None
    value = float(expires)
    # Chromium 150 may expose its 1601-based microsecond timestamp through the
    # cookie jar. Playwright accepts Unix seconds only.
    if value > 253_402_300_799:
        value = value / 1_000_000 - 11_644_473_600
    return value if 0 < value <= 253_402_300_799 else None


def load_x_cookies(config: Config) -> tuple[list[dict[str, object]], CookieSummary]:
    now = time.time()
    playwright_cookies: list[dict[str, object]] = []
    names: set[str] = set()
    jar = _load_cookie_jar(config)
    for cookie in jar:
        if not _is_x_domain(cookie.domain):
            continue
        expiry = _unix_expiry(cookie.expires)
        if expiry is not None and expiry <= now:
            continue
        names.add(cookie.name)
        converted: dict[str, object] = {
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain,
            "path": cookie.path or "/",
            "secure": bool(cookie.secure),
        }
        if expiry is not None:
            converted["expires"] = expiry
        playwright_cookies.append(converted)
    summary = CookieSummary(len(playwright_cookies), "auth_token" in names and "ct0" in names)
    if not summary.has_authentication_cookie:
        for cookie in playwright_cookies:
            cookie["value"] = ""
        playwright_cookies.clear()
        raise AuthenticationRequired("the browser profile does not contain an active X login session")
    return playwright_cookies, summary


def clear_cookie_values(cookies: list[dict[str, object]]) -> None:
    for cookie in cookies:
        with contextlib.suppress(KeyError):
            cookie["value"] = ""
    cookies.clear()

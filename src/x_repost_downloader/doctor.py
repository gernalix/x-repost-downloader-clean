from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .auth import clear_cookie_values, load_x_cookies
from .browser import BrowserCrawler
from .config import Config
from .constants import DEFAULT_CONFIG_PATH
from .storage import verify_storage


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    detail: str


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def run_doctor(config: Config, logger, live_auth: bool = True) -> list[Check]:
    checks: list[Check] = []
    python = shutil.which("python3")
    bundled_yt_dlp = Path(sys.executable).with_name("yt-dlp")
    yt_dlp = shutil.which("yt-dlp") or (str(bundled_yt_dlp) if bundled_yt_dlp.is_file() else None)
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    checks.extend(
        [
            Check("python", bool(python), python or "missing"),
            Check("yt-dlp", bool(yt_dlp), yt_dlp or "missing"),
            Check("ffmpeg", bool(ffmpeg), ffmpeg or "missing"),
            Check("ffprobe", bool(ffprobe), ffprobe or "missing"),
            Check(
                "chromium",
                config.chromium_executable.is_file() and os.access(config.chromium_executable, os.X_OK),
                str(config.chromium_executable),
            ),
            Check("browser-profile", config.browser_profile.is_dir(), str(config.browser_profile)),
        ]
    )
    if yt_dlp:
        version = subprocess.run([yt_dlp, "--version"], capture_output=True, text=True, check=False).stdout.strip()
        extractors = subprocess.run(
            [yt_dlp, "--list-extractors"], capture_output=True, text=True, check=False
        ).stdout.splitlines()
        checks.append(Check("yt-dlp-version", bool(version), version or "unknown"))
        checks.append(Check("X/Twitter-extractor", any(item.startswith("twitter") for item in extractors), "available"))

    if DEFAULT_CONFIG_PATH.exists():
        checks.append(Check("config-permissions", _mode(DEFAULT_CONFIG_PATH) == 0o600, oct(_mode(DEFAULT_CONFIG_PATH))))
    else:
        checks.append(Check("config-permissions", False, "config missing"))
    for name, path in (
        ("output", config.output_dir),
        ("videos", config.video_dir),
        ("metadata", config.metadata_dir),
        ("state", config.state_dir),
        ("logs", config.log_dir),
    ):
        checks.append(Check(f"directory-{name}", path.is_dir() and os.access(path, os.W_OK), str(path)))

    try:
        storage = verify_storage(config)
        checks.append(
            Check(
                "storage-mount-and-space",
                True,
                f"{storage.mount}; free_bytes={storage.free_bytes}; total_bytes={storage.total_bytes}",
            )
        )
    except Exception as error:
        checks.append(Check("storage-mount-and-space", False, str(error)))

    try:
        cookies, summary = load_x_cookies(config)
        checks.append(Check("browser-cookie-session", summary.has_authentication_cookie, "X-scoped session available"))
        clear_cookie_values(cookies)
    except Exception as error:
        checks.append(Check("browser-cookie-session", False, str(error)))

    if live_auth and checks[-1].ok:
        try:
            result = BrowserCrawler(config, logger).crawl(set(), max_scrolls=0)
            checks.append(Check("live-X-session", result.authenticated, "authenticated timeline loaded"))
        except Exception as error:
            checks.append(Check("live-X-session", False, str(error)))

    service_path = Path("/etc/systemd/system/x-repost-downloader.service")
    timer_path = Path("/etc/systemd/system/x-repost-downloader.timer")
    checks.append(Check("systemd-service", service_path.is_file(), str(service_path)))
    checks.append(Check("systemd-timer", timer_path.is_file(), str(timer_path)))
    if timer_path.is_file():
        enabled = subprocess.run(
            ["systemctl", "is-enabled", "x-repost-downloader.timer"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        checks.append(Check("timer-enabled", enabled == "enabled", enabled or "disabled"))
    return checks

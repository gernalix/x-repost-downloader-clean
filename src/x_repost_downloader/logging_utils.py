from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)(auth_token|ct0|cookie|password|token|authorization)=([^\s&]+)"
)
URL_QUERY = re.compile(r"(https?://[^\s?]+)\?[^\s]+")
ANY_URL = re.compile(r"https?://[^\s]+")


def redact(text: str) -> str:
    text = SENSITIVE_ASSIGNMENT.sub(r"\1=REDACTED", text)
    return URL_QUERY.sub(r"\1?REDACTED", text)


def redact_external_urls(text: str) -> str:
    def replacement(match: re.Match[str]) -> str:
        url = match.group(0)
        if re.match(r"https://(?:www\.)?(?:x|twitter)\.com/[^/]+/status/\d+", url, re.IGNORECASE):
            return url.split("?", 1)[0]
        return "[external-url-redacted]"

    return redact(ANY_URL.sub(replacement, text))


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def configure_logging(log_dir: Path, verbose: bool = False) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_dir.chmod(0o700)
    logger = logging.getLogger("x_repost_downloader")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()
    formatter = RedactingFormatter("%(asctime)s %(levelname)s %(message)s")

    stream = logging.StreamHandler()
    stream.setLevel(logging.DEBUG if verbose else logging.INFO)
    stream.setFormatter(formatter)
    logger.addHandler(stream)

    log_path = log_dir / "x-repost-downloader.log"
    file_handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    log_path.chmod(0o600)
    logger.propagate = False
    return logger

from __future__ import annotations

import os
import json
import tomllib
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .constants import (
    DEFAULT_ACCOUNT,
    DEFAULT_CHROME_PROFILE,
    DEFAULT_CHROMIUM,
    DEFAULT_CONFIG_PATH,
    DEFAULT_DEDICATED_PROFILE,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_REQUIRED_MOUNT,
)


@dataclass(frozen=True, slots=True)
class Config:
    account: str = DEFAULT_ACCOUNT
    output_dir: Path = DEFAULT_OUTPUT_DIR
    auth_method: str = "chrome"
    browser_name: str = "chrome"
    browser_profile: Path = DEFAULT_CHROME_PROFILE
    keyring: str = "gnomekeyring"
    chromium_executable: Path = DEFAULT_CHROMIUM
    dedicated_profile: Path = DEFAULT_DEDICATED_PROFILE
    max_scrolls_check: int = 24
    max_scrolls_backfill: int = 200
    stagnant_scrolls: int = 5
    backfill_stagnant_scrolls: int = 12
    known_reposts_stop: int = 3
    max_downloads_per_run: int = 10
    availability_recheck_days: int = 7
    max_availability_checks_per_run: int = 50
    required_mount: Path = DEFAULT_REQUIRED_MOUNT
    minimum_free_gib: int = 20
    min_pause_seconds: float = 1.5
    max_pause_seconds: float = 3.5
    navigation_timeout_seconds: int = 45

    @property
    def profile_url(self) -> str:
        return f"https://x.com/{self.account}"

    @property
    def state_dir(self) -> Path:
        return self.output_dir / "state"

    @property
    def video_dir(self) -> Path:
        return self.output_dir / "videos"

    @property
    def metadata_dir(self) -> Path:
        return self.output_dir / "metadata"

    @property
    def log_dir(self) -> Path:
        return self.output_dir / "logs"

    @property
    def archive_path(self) -> Path:
        return self.state_dir / "download-archive.txt"

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "run.lock"

    @property
    def state_path(self) -> Path:
        return self.state_dir / "state.json"

    @property
    def repost_db_path(self) -> Path:
        return self.state_dir / "reposts.sqlite"

    @property
    def cookie_browser_spec(self) -> str:
        profile = str(self.browser_profile)
        return f"{self.browser_name}+{self.keyring}:{profile}"

    def with_output_dir(self, output_dir: Path | None) -> "Config":
        return replace(self, output_dir=output_dir.expanduser().resolve()) if output_dir else self


PATH_FIELDS = {"output_dir", "browser_profile", "chromium_executable", "dedicated_profile", "required_mount"}
KNOWN_FIELDS = set(Config.__dataclass_fields__)


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> Config:
    if not path.exists():
        return Config()
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    unknown = set(raw) - KNOWN_FIELDS
    if unknown:
        raise ValueError(f"Unknown configuration keys: {', '.join(sorted(unknown))}")
    for field in PATH_FIELDS & raw.keys():
        raw[field] = Path(raw[field]).expanduser()
    return Config(**raw)


def ensure_layout(config: Config) -> None:
    if config.output_dir.resolve().is_relative_to(config.required_mount.resolve()) and not config.required_mount.is_mount():
        raise RuntimeError(f"required storage is not mounted at {config.required_mount}")
    for directory in (
        config.output_dir,
        config.video_dir,
        config.metadata_dir,
        config.state_dir,
        config.log_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
    config.archive_path.touch(mode=0o600, exist_ok=True)
    config.archive_path.chmod(0o600)


def _toml_value(value: object) -> str:
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def save_config(config: Config, path: Path = DEFAULT_CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    content = "\n".join(f"{key} = {_toml_value(value)}" for key, value in asdict(config).items()) + "\n"
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def ensure_default_config(path: Path = DEFAULT_CONFIG_PATH) -> Config:
    config = load_config(path)
    if not path.exists():
        save_config(config, path)
    ensure_layout(config)
    return config


def detect_chrome_profile() -> Path:
    roots = (
        Path.home() / ".var" / "app" / "com.google.Chrome" / "config" / "google-chrome",
        Path.home() / ".config" / "google-chrome",
    )
    for root in roots:
        local_state = root / "Local State"
        if not local_state.is_file():
            continue
        candidates: list[str] = []
        try:
            with local_state.open("r", encoding="utf-8") as handle:
                profile = json.load(handle).get("profile", {})
            candidates.extend(profile.get("last_active_profiles", []))
            if profile.get("last_used"):
                candidates.append(profile["last_used"])
        except (OSError, ValueError, TypeError):
            pass
        candidates.append("Default")
        for name in dict.fromkeys(candidates):
            path = root / name
            if (path / "Cookies").is_file() or (path / "Network" / "Cookies").is_file():
                return path
    raise FileNotFoundError("no readable Google Chrome profile with a Cookies database was found")

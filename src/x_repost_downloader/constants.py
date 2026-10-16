from pathlib import Path

APP_NAME = "x-repost-downloader"
DEFAULT_ACCOUNT = "LivingGaul"
DEFAULT_OUTPUT_DIR = Path.home() / ".local" / "share" / APP_NAME / "archive"
DEFAULT_REQUIRED_MOUNT = Path("/")
DEFAULT_CONFIG_PATH = Path.home() / ".config" / APP_NAME / "config.toml"
DEFAULT_CHROME_PROFILE = (
    Path.home()
    / ".var"
    / "app"
    / "com.google.Chrome"
    / "config"
    / "google-chrome"
    / "Default"
)
DEFAULT_DEDICATED_PROFILE = Path.home() / ".local" / "share" / APP_NAME / "x-session-profile"
DEFAULT_CHROMIUM = Path("/usr/bin/chromium-browser")

EXIT_OK = 0
EXIT_PARTIAL_FAILURE = 1
EXIT_USAGE = 2
EXIT_AUTH_REQUIRED = 20
EXIT_RATE_LIMITED = 21
EXIT_CHECKPOINT = 22
EXIT_LOCKED = 75

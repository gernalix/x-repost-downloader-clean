from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .config import Config
from .logging_utils import redact_external_urls
from .repost_db import AVAILABILITY_AVAILABLE, AVAILABILITY_UNAVAILABLE, AVAILABILITY_UNKNOWN
from .storage import verify_storage

X_PROTECTION_ERROR_MARKERS = (
    "rate limit",
    "too many requests",
    "http error 429",
    "429",
    "login",
    "auth",
    "cookie",
    "checkpoint",
    "captcha",
    "forbidden",
    "unauthorized",
)


def is_x_protection_error(error: str) -> bool:
    lowered = error.casefold()
    return any(marker in lowered for marker in X_PROTECTION_ERROR_MARKERS)


@dataclass(slots=True)
class DownloadResult:
    downloaded_paths: list[Path] = field(default_factory=list)
    new_paths: list[Path] = field(default_factory=list)
    archive_ids: list[str] = field(default_factory=list)
    skipped: bool = False
    validated: bool = False
    error: str | None = None


@dataclass(frozen=True, slots=True)
class AvailabilityResult:
    status: str
    error: str | None = None


class VideoDownloader:
    def __init__(self, config: Config, logger):
        self.config = config
        self.logger = logger
        bundled = Path(sys.executable).with_name("yt-dlp")
        self.executable = shutil.which("yt-dlp") or (str(bundled) if bundled.is_file() else None)
        self.ffprobe = shutil.which("ffprobe")
        if not self.executable:
            raise RuntimeError("yt-dlp executable is not available")
        if not self.ffprobe:
            raise RuntimeError("ffprobe executable is not available")

    def _base_command(self, archive_path: Path | None) -> list[str]:
        template = (
            "%(upload_date>%Y-%m-%d|unknown)s - "
            "%(uploader_id|uploader|unknown).60B - status-%(id)s - %(title).100B.%(ext)s"
        )
        command = [
            self.executable,
            "--cookies-from-browser",
            self.config.cookie_browser_spec,
            "--format",
            "bestvideo[height<=720]+bestaudio/best[height<=720]/bestvideo+bestaudio/best",
            "--paths",
            f"home:{self.config.video_dir}",
            "--paths",
            f"temp:{self.config.video_dir}",
            "--paths",
            f"infojson:{self.config.metadata_dir}",
            "--output",
            template,
        ]
        if archive_path is not None:
            command.extend(("--download-archive", str(archive_path)))
        command.extend(
            (
                "--write-info-json",
                "--no-write-playlist-metafiles",
                "--no-overwrites",
                "--continue",
                "--part",
                "--trim-filenames",
                "220",
                "--retries",
                "5",
                "--fragment-retries",
                "5",
                "--retry-sleep",
                "http:exp=1:20",
                "--retry-sleep",
                "fragment:exp=1:20",
                "--socket-timeout",
                "30",
                "--sleep-requests",
                "1",
                "--sleep-interval",
                "2",
                "--max-sleep-interval",
                "5",
                "--concurrent-fragments",
                "1",
                "--no-progress",
                "--no-warnings",
            )
        )
        return command

    def probe(self, url: str) -> tuple[bool, int, str | None]:
        command = self._base_command(None) + [
            "--simulate",
            "--print",
            "after_filter:__XRD_PROBE__%(id)s",
            url,
        ]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        identifiers = [line for line in completed.stdout.splitlines() if line.startswith("__XRD_PROBE__")]
        if completed.returncode == 0 and identifiers:
            return True, len(identifiers), None
        return False, 0, self._safe_error(completed)

    def check_availability(self, url: str) -> AvailabilityResult:
        command = self._base_command(None) + [
            "--simulate",
            "--skip-download",
            "--print",
            "after_filter:__XRD_AVAILABLE__%(id)s",
            url,
        ]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode == 0 and "__XRD_AVAILABLE__" in completed.stdout:
            return AvailabilityResult(AVAILABILITY_AVAILABLE)
        error = self._safe_error(completed)
        return AvailabilityResult(self._classify_availability_error(error), error)

    def download(self, url: str) -> DownloadResult:
        verify_storage(self.config)
        working_archive = self.config.state_dir / f".archive-work-{os.getpid()}-{uuid.uuid4().hex}.txt"
        if self.config.archive_path.exists():
            shutil.copy2(self.config.archive_path, working_archive)
        else:
            working_archive.touch(mode=0o600)
        working_archive.chmod(0o600)
        before_lines = self._read_archive(self.config.archive_path)
        try:
            command = self._base_command(working_archive) + [
                "--print",
                "after_move:__XRD_FILE__%(filepath)s",
                "--print",
                "after_move:__XRD_ARCHIVE__%(extractor)s %(id)s",
                url,
            ]
            completed = subprocess.run(command, capture_output=True, text=True, check=False)
            paths = [
                Path(line.removeprefix("__XRD_FILE__"))
                for line in completed.stdout.splitlines()
                if line.startswith("__XRD_FILE__")
            ]
            printed_ids = [
                line.removeprefix("__XRD_ARCHIVE__").strip()
                for line in completed.stdout.splitlines()
                if line.startswith("__XRD_ARCHIVE__")
            ]
            if completed.returncode != 0:
                return DownloadResult(downloaded_paths=paths, archive_ids=printed_ids, error=self._safe_error(completed))

            if not paths:
                probed_ids, probe_error = self._probe_archive_ids(url)
                media_ids = {value.rsplit(" ", 1)[-1] for value in probed_ids if " " in value}
                existing_paths, existing_ids, error = self._find_and_validate_existing(
                    url,
                    media_ids=media_ids,
                )
                return DownloadResult(
                    downloaded_paths=existing_paths,
                    archive_ids=list(dict.fromkeys(probed_ids + existing_ids)),
                    skipped=True,
                    validated=bool(existing_paths) and error is None,
                    error=probe_error or error,
                )

            validation_errors = [error for path in paths if (error := self.validate_file(path))]
            if validation_errors:
                self._quarantine_invalid(paths)
                return DownloadResult(
                    downloaded_paths=paths,
                    new_paths=paths,
                    archive_ids=printed_ids,
                    error="; ".join(validation_errors)[:1000],
                )

            # Reconcile every media item belonging to the repost, including
            # entries that yt-dlp skipped because they were already archived.
            # This prevents a partly missing multi-video repost from being
            # declared complete merely because one newly downloaded file was
            # valid.
            printed_media_ids = {
                archive_id.rsplit(" ", 1)[-1]
                for archive_id in printed_ids
                if " " in archive_id
            }
            all_paths, metadata_ids, reconciliation_error = self._find_and_validate_existing(
                url,
                media_ids=printed_media_ids,
            )
            if reconciliation_error:
                self._quarantine_invalid(paths)
                return DownloadResult(
                    downloaded_paths=paths,
                    new_paths=paths,
                    archive_ids=printed_ids,
                    error=reconciliation_error,
                )

            after_lines = self._read_archive(working_archive)
            new_lines = [line for line in after_lines if line not in set(before_lines)]
            archive_ids = list(dict.fromkeys(printed_ids + metadata_ids + new_lines))
            self._merge_archive(after_lines)
            return DownloadResult(
                downloaded_paths=all_paths,
                new_paths=paths,
                archive_ids=archive_ids,
                validated=True,
            )
        finally:
            working_archive.unlink(missing_ok=True)

    def validate_file(self, path: Path) -> str | None:
        if not path.is_file():
            return f"downloaded path does not exist: {path.name}"
        if path.stat().st_size <= 0:
            return f"downloaded file is empty: {path.name}"
        completed = subprocess.run(
            [
                self.ffprobe,
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,codec_name:format=duration,size",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            return f"ffprobe failed for {path.name}: {redact_external_urls(completed.stderr)[:300]}"
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return f"ffprobe returned invalid JSON for {path.name}"
        if not any(stream.get("codec_type") == "video" for stream in payload.get("streams", [])):
            return f"ffprobe found no video stream in {path.name}"
        return None

    def _probe_archive_ids(self, url: str) -> tuple[list[str], str | None]:
        command = self._base_command(None) + [
            "--simulate",
            "--print",
            "after_filter:__XRD_ARCHIVE__%(extractor)s %(id)s",
            url,
        ]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        archive_ids = [
            line.removeprefix("__XRD_ARCHIVE__").strip()
            for line in completed.stdout.splitlines()
            if line.startswith("__XRD_ARCHIVE__")
        ]
        if completed.returncode == 0 and archive_ids:
            return archive_ids, None
        return [], self._safe_error(completed)

    def _find_and_validate_existing(
        self,
        url: str,
        media_ids: set[str] | None = None,
    ) -> tuple[list[Path], list[str], str | None]:
        status_id = url.rstrip("/").split("/")[-1]
        media_ids = media_ids or set()
        found: list[Path] = []
        archive_ids: list[str] = []
        missing_media: list[str] = []
        matching_metadata = 0
        for info_path in self.config.metadata_dir.glob("*.info.json"):
            try:
                with info_path.open("r", encoding="utf-8") as handle:
                    info = json.load(handle)
            except (OSError, json.JSONDecodeError):
                continue
            source_urls = (str(info.get("webpage_url") or ""), str(info.get("original_url") or ""))
            media_id = str(info.get("id") or "")
            if not (
                any(f"/status/{status_id}" in source for source in source_urls)
                or (media_id and media_id in media_ids)
            ):
                continue
            matching_metadata += 1
            extractor = str(info.get("extractor") or info.get("extractor_key") or "twitter").casefold()
            if media_id:
                archive_ids.append(f"{extractor} {media_id}")
            extension = str(info.get("ext") or "")
            pattern = f"*status-{media_id}*.{extension}" if extension else f"*status-{media_id}*"
            media_paths = [path for path in self.config.video_dir.glob(pattern) if path.is_file()]
            found.extend(media_paths)
            if not media_paths:
                missing_media.append(media_id or info_path.name)
        found = list(dict.fromkeys(found))
        if missing_media:
            return found, archive_ids, "metadata exists but media file is missing for: " + ", ".join(missing_media)
        if matching_metadata == 0:
            return [], archive_ids, "yt-dlp did not produce matching info.json metadata"
        if not found:
            return [], archive_ids, "download archive matched but no corresponding media file exists"
        errors = [error for path in found if (error := self.validate_file(path))]
        if errors:
            return found, archive_ids, "; ".join(errors)[:1000]
        return found, archive_ids, None

    def _merge_archive(self, desired_lines: list[str]) -> None:
        current = self._read_archive(self.config.archive_path)
        merged = list(dict.fromkeys(current + desired_lines))
        temporary = self.config.archive_path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                for line in merged:
                    handle.write(f"{line}\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.config.archive_path)
            self.config.archive_path.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _read_archive(path: Path) -> list[str]:
        if not path.is_file():
            return []
        with path.open("r", encoding="utf-8") as handle:
            return [line.strip() for line in handle if line.strip()]

    def _quarantine_invalid(self, paths: list[Path]) -> None:
        destination = self.config.state_dir / "invalid"
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        for path in paths:
            if path.is_file():
                path.replace(destination / f"{path.name}.invalid-{stamp}")

    @staticmethod
    def _safe_error(completed: subprocess.CompletedProcess[str]) -> str:
        candidates = []
        for line in (completed.stderr + "\n" + completed.stdout).splitlines():
            if "error" in line.casefold() or "warning" in line.casefold():
                candidates.append(line.strip())
        summary = " | ".join(candidates[-3:]) if candidates else f"yt-dlp exited {completed.returncode}"
        return redact_external_urls(summary)[:1000]

    @staticmethod
    def _classify_availability_error(error: str) -> str:
        lowered = error.casefold()
        transient_markers = (
            "timeout",
            "timed out",
            "network",
            "connection",
            "temporarily",
            "500",
            "502",
            "503",
            "504",
        )
        unavailable_markers = (
            "not available",
            "no longer available",
            "does not exist",
            "deleted",
            "removed",
            "suspended",
            "unable to extract",
        )
        if is_x_protection_error(error) or any(marker in lowered for marker in transient_markers):
            return AVAILABILITY_UNKNOWN
        if any(marker in lowered for marker in unavailable_markers):
            return AVAILABILITY_UNAVAILABLE
        return AVAILABILITY_UNKNOWN

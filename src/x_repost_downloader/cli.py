from __future__ import annotations

import argparse
import copy
import json
import sys
from dataclasses import asdict, replace
from pathlib import Path

from .config import Config, detect_chrome_profile, ensure_default_config, ensure_layout, load_config, save_config
from .constants import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_DEDICATED_PROFILE,
    EXIT_AUTH_REQUIRED,
    EXIT_CHECKPOINT,
    EXIT_LOCKED,
    EXIT_OK,
    EXIT_PARTIAL_FAILURE,
    EXIT_RATE_LIMITED,
)
from .errors import (
    AuthenticationRequired,
    BrowserProfileLocked,
    CheckpointRequired,
    LockBusy,
    RateLimited,
)
from .logging_utils import configure_logging
from .repost_db import (
    AVAILABILITY_UNKNOWN,
    DOWNLOAD_PENDING,
    RepostDB,
)
from .state import RunMetrics, StateStore, exclusive_lock, utc_now
from .storage import verify_storage


def _common_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--verbose", action="store_true", help="show detailed decisions without secrets")
    common.add_argument("--max-scrolls", type=int, help="override the configured maximum timeline scrolls")
    common.add_argument("--output-dir", type=Path, help="override the output root for this invocation")
    return common


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="x-repost-downloader")
    subparsers = parser.add_subparsers(dest="command", required=True)
    common = _common_parser()

    setup = subparsers.add_parser("setup", parents=[common], help="prepare directories and authentication")
    setup.add_argument("--reauthenticate", action="store_true", help="create or renew the dedicated browser session")
    setup.add_argument(
        "--direct-x-login",
        action="store_true",
        help="fallback: manually log in directly to X in the dedicated profile; never uses Google login",
    )

    for name, help_text in (
        ("check", "check recent reposts and download missing videos"),
        ("backfill", "progressively scan reachable historical reposts"),
    ):
        command = subparsers.add_parser(name, parents=[common], help=help_text)
        command.add_argument("--dry-run", action="store_true", help="discover and probe without downloads or state changes")
        command.add_argument(
            "--max-downloads",
            type=int,
            help="limit downloads/probes in this run; deferred new items remain pending",
        )

    subparsers.add_parser("diagnose", parents=[common], help="inspect authenticated repost discovery with media blocked; no downloads or state writes")
    db_backfill = subparsers.add_parser("db-backfill", parents=[common], help="register existing downloaded video files in the SQLite repost database")
    db_backfill.add_argument("--dry-run", action="store_true", help="count deterministic associations without writing the database")
    availability = subparsers.add_parser("availability-check", parents=[common], help="probe downloaded repost URLs without downloading media")
    availability.add_argument("--limit", type=int, help="maximum due records to check in this run")
    availability.add_argument("--all", action="store_true", help="ignore the configured 7-day eligibility window")
    subparsers.add_parser("status", parents=[common], help="show the last persisted run status")
    subparsers.add_parser("doctor", parents=[common], help="validate dependencies, authentication and timer")
    return parser


def _load_for_args(args) -> Config:
    config = load_config(DEFAULT_CONFIG_PATH)
    return config.with_output_dir(args.output_dir)


def _setup(args) -> int:
    from .browser import BrowserCrawler, initialize_dedicated_profile_from_chrome, interactive_dedicated_login

    config = ensure_default_config(DEFAULT_CONFIG_PATH)
    if args.output_dir:
        config = config.with_output_dir(args.output_dir)
        ensure_layout(config)
    logger = configure_logging(config.log_dir, args.verbose)
    source_profile = detect_chrome_profile()
    source = replace(
        config,
        auth_method="chrome",
        browser_name="chrome",
        browser_profile=source_profile,
        keyring="gnomekeyring",
        dedicated_profile=DEFAULT_DEDICATED_PROFILE,
    )
    candidate = replace(
        source,
        auth_method="dedicated_direct" if args.direct_x_login else "dedicated_imported",
        browser_name="chromium",
        browser_profile=DEFAULT_DEDICATED_PROFILE / "Default",
        keyring="gnomekeyring",
        dedicated_profile=DEFAULT_DEDICATED_PROFILE,
    )
    if args.direct_x_login:
        interactive_dedicated_login(candidate)
        BrowserCrawler(candidate, logger).crawl(set(), max_scrolls=1)
        BrowserCrawler(candidate, logger).crawl(set(), max_scrolls=1)
        method = "direct manual X username/email and password in dedicated profile"
    else:
        try:
            imported_count, reopened = initialize_dedicated_profile_from_chrome(source, candidate, logger)
        except BrowserProfileLocked:
            logger.error(
                "Chiudi una sola volta tutte le finestre di Google Chrome, poi riesegui: x-repost-downloader setup"
            )
            return EXIT_AUTH_REQUIRED
        except AuthenticationRequired:
            logger.error(
                "Apri il normale Google Chrome, accedi direttamente a x.com e conferma che il profilo @LivingGaul sia visibile"
            )
            return EXIT_AUTH_REQUIRED
        logger.info(
            "Imported %d X/Twitter-only cookies; persisted session reopened; reposts_seen=%d video_reposts=%d",
            imported_count,
            len(reopened.reposts),
            sum(item.has_video for item in reopened.reposts),
        )
        method = "X/Twitter-only cookie import from normal Chrome into dedicated Chromium profile"
    save_config(candidate, DEFAULT_CONFIG_PATH)
    ensure_layout(candidate)
    logger.info("Authentication method verified and saved: %s", method)
    return EXIT_OK


def _run_download_mode(args, mode: str) -> int:
    from .browser import BrowserCrawler
    from .downloader import VideoDownloader, is_x_protection_error

    config = _load_for_args(args)
    verify_storage(config, require_output_writable=False)
    ensure_layout(config)
    verify_storage(config)
    logger = configure_logging(config.log_dir, args.verbose)
    store = StateStore(config.state_path)
    repost_db = None if args.dry_run else RepostDB(config.repost_db_path)
    with exclusive_lock(config.lock_path):
        persisted_state = store.load()
        state = copy.deepcopy(persisted_state) if args.dry_run else persisted_state
        if not args.dry_run and state.cooldown_active():
            logger.info("X protection cooldown active until %s; no network request made", state.next_allowed_check_at)
            state.last_check = utc_now()
            state.last_mode = mode
            state.last_result = "cooldown_noop"
            store.save(state)
            return EXIT_OK
        seen_before = state.seen()
        max_scrolls = args.max_scrolls
        if max_scrolls is None:
            # A normal backfill has no arbitrary scroll ceiling. The legacy
            # max_scrolls_backfill setting is retained solely for config-file
            # compatibility; an explicit CLI override still provides a safe
            # bounded diagnostic run.
            max_scrolls = None if mode == "backfill" else config.max_scrolls_check

        started_at = utc_now()
        base_scrolls_total = state.backfill_scrolls_total
        if not args.dry_run:
            state.last_check = started_at
            state.last_mode = mode
            state.last_result = "running"
            state.last_error = None
            if mode == "backfill":
                state.backfill_runs += 1
                state.backfill_complete = False
                state.backfill_stop_reason = None
                state.backfill_technical_limit = False
                state.backfill_last_checkpoint = started_at
            store.save(state)

        def record_repost(repost) -> None:
            if not args.dry_run:
                assert repost_db is not None
                repost_db.upsert_seen(
                    repost.url,
                    repost.status_id,
                    DOWNLOAD_PENDING,
                )
            state.discover(
                status_id=repost.status_id,
                url=repost.url,
                author=repost.author,
                has_video=repost.has_video,
                published_at=repost.published_at,
            )

        def checkpoint(partial_result) -> None:
            if args.dry_run:
                return
            if mode == "backfill":
                state.backfill_last_checkpoint = utc_now()
                state.backfill_scrolls_total = base_scrolls_total + partial_result.scrolls
            store.save(state)

        try:
            result = BrowserCrawler(config, logger).crawl(
                seen_before,
                max_scrolls,
                backfill=mode == "backfill",
                on_repost=record_repost,
                on_checkpoint=checkpoint,
            )
        except (RateLimited, AuthenticationRequired, CheckpointRequired) as error:
            if not args.dry_run:
                state.mark_protection_blocked(str(error))
                store.save(state)
            raise
        discovered_this_run = len(state.seen() - seen_before)
        metrics = RunMetrics(
            visited_reposts=len(result.examined_repost_ids),
            video_reposts=sum(1 for repost in result.reposts if repost.has_video),
            discovered=discovered_this_run,
            scrolls=result.scrolls,
        )
        all_candidates = state.actionable(include_failed=True)
        download_limit = args.max_downloads
        if download_limit is not None and download_limit < 0:
            raise ValueError("--max-downloads must be zero or greater")
        candidates = all_candidates if download_limit is None else all_candidates[:download_limit]
        deferred = len(all_candidates) - len(candidates)
        if deferred:
            logger.info("Deferred %d pending video reposts to a future run", deferred)
        downloader = VideoDownloader(config, logger)
        protection_blocked = False
        for item in candidates:
            if args.dry_run:
                ok, media_count, error = downloader.probe(item.url)
                if ok:
                    logger.info("DRY-RUN status=%s video_items=%d", item.status_id, media_count)
                else:
                    logger.error("DRY-RUN status=%s failed: %s", item.status_id, error)
                    metrics.failed += 1
                continue
            state.mark_attempt(item.status_id)
            store.save(state)
            try:
                outcome = downloader.download(item.url)
            except Exception as error:
                logger.error("status=%s download invocation failed: %s", item.status_id, error)
                state.mark_failed(item.status_id, str(error))
                metrics.failed += 1
                store.save(state)
                continue
            if outcome.error or not outcome.validated:
                error = outcome.error or "download did not pass file existence and ffprobe validation"
                logger.error("status=%s failed: %s", item.status_id, error)
                if is_x_protection_error(error):
                    state.mark_protection_blocked(error)
                    protection_blocked = True
                    store.save(state)
                    break
                state.mark_failed(item.status_id, error)
                repost_db.mark_failed(item.url, item.status_id, error)
                metrics.failed += 1
            else:
                state.mark_downloaded(item.status_id, outcome.downloaded_paths, outcome.archive_ids)
                repost_db.mark_downloaded(item.url, item.status_id, outcome.downloaded_paths)
                if outcome.skipped:
                    logger.info("Validated existing archived status=%s", item.status_id)
                    metrics.duplicates += 1
                    state.duplicates_total += 1
                else:
                    for path in outcome.new_paths:
                        logger.info("Downloaded and validated status=%s file=%s", item.status_id, path.name)
                    metrics.downloaded += 1
                    metrics.downloaded_files += len(outcome.new_paths)
                    state.downloaded_files_total += len(outcome.new_paths)
            store.save(state)

        metrics.pending = state.counts()["pending"]

        logger.info(
            "%s complete: visited=%d video=%d discovered=%d pending=%d downloaded=%d "
            "files=%d duplicates=%d failed=%d scrolls=%d stop=%s",
            mode,
            metrics.visited_reposts,
            metrics.video_reposts,
            metrics.discovered,
            metrics.pending,
            metrics.downloaded,
            metrics.downloaded_files,
            metrics.duplicates,
            metrics.failed,
            metrics.scrolls,
            result.stopped_reason,
        )
        if args.dry_run:
            return EXIT_PARTIAL_FAILURE if metrics.failed else EXIT_OK

        state.last_check = utc_now()
        state.last_mode = mode
        state.last_metrics = metrics
        if protection_blocked:
            state.last_result = "x_protection_cooldown"
        else:
            state.last_result = "partial_failure" if metrics.failed else (
                "technical_limit" if result.technical_limit else "success"
            )
            state.last_error = None
        if not metrics.failed and not protection_blocked:
            state.last_success = state.last_check
            state.reset_protection_cooldown()
        if mode == "backfill":
            state.last_backfill_at = state.last_check
            state.last_backfill_metrics = metrics
            # Completion means the entire history that X exposed in this run
            # was traversed. Keep the technical-limit bit and stop reason so a
            # no-progress API/DOM boundary is never misreported as an explicit
            # end-of-timeline marker.
            state.backfill_complete = result.timeline_end_reached or result.technical_limit
            state.backfill_stop_reason = result.stopped_reason
            state.backfill_technical_limit = result.technical_limit
            state.backfill_last_checkpoint = state.last_check
            state.backfill_scrolls_total = base_scrolls_total + result.scrolls
        store.save(state)
        return EXIT_PARTIAL_FAILURE if metrics.failed else EXIT_OK


def _status(args) -> int:
    config = _load_for_args(args)
    state = StateStore(config.state_path).load()
    payload = asdict(state)
    payload["counts"] = state.counts()
    payload["output_dir"] = str(config.output_dir)
    if config.repost_db_path.exists():
        payload["repost_db"] = {
            "path": str(config.repost_db_path),
            **RepostDB(config.repost_db_path).integrity_summary(),
        }
    payload["timer_enabled"] = _timer_enabled()
    print(json.dumps(payload, indent=2, sort_keys=True))
    return EXIT_OK


def _db_backfill(args) -> int:
    config = _load_for_args(args)
    verify_storage(config, require_output_writable=False)
    ensure_layout(config)
    logger = configure_logging(config.log_dir, args.verbose)
    with exclusive_lock(config.lock_path):
        if args.dry_run:
            result = RepostDB.preview_existing_downloads(config.video_dir)
            logger.info("db-backfill dry-run associated=%d unassociated=%d", result.associated, result.unassociated)
            return EXIT_OK
        result = RepostDB(config.repost_db_path).backfill_existing_downloads(config.video_dir, config.metadata_dir)
    logger.info("db-backfill complete associated=%d unassociated=%d", result.associated, result.unassociated)
    return EXIT_OK


def _availability_check(args) -> int:
    from .downloader import VideoDownloader

    config = _load_for_args(args)
    if args.limit is not None and args.limit < 0:
        raise ValueError("--limit must be zero or greater")
    verify_storage(config, require_output_writable=False)
    ensure_layout(config)
    logger = configure_logging(config.log_dir, args.verbose)
    repost_db = RepostDB(config.repost_db_path)
    with exclusive_lock(config.lock_path):
        days = 0 if args.all else config.availability_recheck_days
        limit = args.limit if args.limit is not None else config.max_availability_checks_per_run
        due = repost_db.due_downloaded(days, limit)
        downloader = VideoDownloader(config, logger)
        failures = 0
        for item in due:
            result = downloader.check_availability(item.url)
            repost_db.mark_availability(item.url, item.status_id, result.status, result.error)
            if result.status == AVAILABILITY_UNKNOWN and result.error:
                failures += 1
                logger.warning("availability status=%s unknown: %s", item.status_id, result.error)
            else:
                logger.info("availability status=%s remote=%s", item.status_id, result.status)
    logger.info("availability-check complete checked=%d unknown_errors=%d", len(due), failures)
    return EXIT_PARTIAL_FAILURE if failures else EXIT_OK


def _timer_enabled() -> bool:
    import subprocess

    return (
        subprocess.run(
            ["systemctl", "--user", "is-enabled", "x-repost-downloader.timer"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        == "enabled"
    )


def _doctor(args) -> int:
    from .doctor import run_doctor

    config = _load_for_args(args)
    ensure_layout(config)
    logger = configure_logging(config.log_dir, args.verbose)
    checks = run_doctor(config, logger)
    for check in checks:
        print(f"{'PASS' if check.ok else 'FAIL'} {check.name}: {check.detail}")
    critical_failures = [check for check in checks if not check.ok and check.name != "timer-enabled"]
    return EXIT_PARTIAL_FAILURE if critical_failures else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "diagnose":
            from .capsules.timeline.diagnostic import run_diagnostic

            return run_diagnostic(_load_for_args(args), args.max_scrolls)
        if args.command == "setup":
            return _setup(args)
        if args.command in {"check", "backfill"}:
            return _run_download_mode(args, args.command)
        if args.command == "db-backfill":
            return _db_backfill(args)
        if args.command == "availability-check":
            return _availability_check(args)
        if args.command == "status":
            return _status(args)
        if args.command == "doctor":
            return _doctor(args)
        parser.error("unknown command")
    except BrowserProfileLocked:
        print(
            "Chiudi una sola volta tutte le finestre di Google Chrome, poi riesegui il comando.",
            file=sys.stderr,
        )
        return EXIT_AUTH_REQUIRED
    except AuthenticationRequired as error:
        print(f"Autenticazione richiesta: {error}. Esegui x-repost-downloader setup --reauthenticate", file=sys.stderr)
        return EXIT_AUTH_REQUIRED
    except RateLimited as error:
        print(f"Rate limit rilevato: {error}", file=sys.stderr)
        return EXIT_RATE_LIMITED
    except CheckpointRequired as error:
        print(f"Interazione manuale richiesta: {error}", file=sys.stderr)
        return EXIT_CHECKPOINT
    except LockBusy as error:
        print(str(error), file=sys.stderr)
        return EXIT_LOCKED
    except KeyboardInterrupt:
        print("Operazione interrotta; stato esistente preservato", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"Errore: {error}", file=sys.stderr)
        return EXIT_PARTIAL_FAILURE
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

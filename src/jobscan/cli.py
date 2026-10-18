"""Command line interface: scan, serve, status."""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

from . import __version__
from .client import LinkedInClient, PageFetcher
from .config import Config, ConfigError
from .db import SchemaTooNewError, connect, migrate
from .lock import AlreadyRunningError, RunLock
from .models import STATUSES, RoleFilter
from .report import Reporter, log_name
from .scanner import Scanner, ScanSetupError
from .store import RUN_FAILED, RUN_PARTIAL, RUN_SUCCESS, Store
from .timeutil import format_display, from_iso, now_ist, to_iso

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_PARTIAL = 3
EXIT_INTERRUPTED = 130

DB_ENV = "JOBSCAN_DB"
DEFAULT_DB = Path("~/.jobscan/jobscan.db")


def resolve_db_path(arg: str | None, env: dict[str, str] | None = None) -> Path:
    environment = os.environ if env is None else env
    raw = arg or environment.get(DB_ENV)
    return (Path(raw) if raw else DEFAULT_DB).expanduser()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobscan", description="LinkedIn job scanner")
    parser.add_argument("--version", action="version", version=f"jobscan {__version__}")
    parser.add_argument("--db", help=f"database file (default: {DEFAULT_DB}, or ${DB_ENV} if set)")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="fetch new roles from LinkedIn")
    scan.add_argument("--force", action="store_true", help="ignore the minimum gap between runs")

    serve = sub.add_parser("serve", help="start the local web app")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=5000)

    sub.add_parser("status", help="show last run and role counts")
    return parser


def open_store(db_path: Path) -> Store:
    conn = connect(db_path)
    try:
        migrate(conn)
    except BaseException:
        conn.close()
        raise
    return Store(conn)


def _minutes_since(iso: str) -> float:
    return (now_ist() - from_iso(iso)).total_seconds() / 60


def cmd_status(store: Store, out: TextIO) -> int:
    last_ok = store.last_successful_run()
    latest = store.latest_run()
    print(
        "Last successful run: "
        + (
            f"{format_display(last_ok.finished_on or last_ok.started_on)} IST"
            if last_ok
            else "none"
        ),
        file=out,
    )
    if latest:
        print(
            f"Latest run: #{latest.id} {latest.status} at {format_display(latest.started_on)} IST",
            file=out,
        )
    print(f"Roles: {store.count_roles()}", file=out)
    for status in STATUSES:
        print(f"  {status}: {store.count_roles(RoleFilter(status=status))}", file=out)
    return EXIT_OK


def cmd_scan(
    store: Store,
    db_path: Path,
    *,
    force: bool,
    out: TextIO,
    fetcher: PageFetcher | None,
    sleep: Callable[[float], None],
    rng: random.Random | None,
) -> int:
    try:
        config = Config.from_raw(store.get_config_raw())
    except ConfigError as exc:
        print("Configuration is invalid; nothing was fetched:", file=out)
        for key, message in exc.errors.items():
            print(f"  {key}: {message}", file=out)
        return EXIT_USAGE

    stale = store.fail_stale_runs()
    if stale:
        print(f"Marked {stale} unfinished earlier run(s) as failed", file=out)

    latest = store.latest_run()
    if latest and not force:
        gap = _minutes_since(latest.started_on)
        if gap < config.min_gap_between_runs_minutes:
            print(
                f"Refusing to run: the last run started {gap:.0f} min ago and the minimum gap "
                f"is {config.min_gap_between_runs_minutes} min. Use --force to override.",
                file=out,
            )
            return EXIT_USAGE

    reporter = Reporter(out, db_path.parent / "runs" / log_name(to_iso(now_ist())))
    own_client: LinkedInClient | None = None
    try:
        last_ok = store.last_successful_run()
        if last_ok is None:
            reporter.line("Last successful run: none")
        else:
            last_time = last_ok.finished_on or last_ok.started_on
            reporter.line(f"Last successful run: {format_display(last_time)} IST")
            age = (now_ist() - from_iso(last_time)).total_seconds()
            if age > config.time_window_seconds:
                reporter.line(
                    "WARNING: the last successful run is older than the "
                    f"{config.time_window_seconds}s search window, so roles posted in "
                    "between may be missed."
                )
        if not config.window_is_verified:
            reporter.line(
                "WARNING: the search window is not the verified 86400 seconds; "
                "LinkedIn may ignore it."
            )
        reporter.line(
            f"Config OK: window {config.time_window_seconds}s, match mode {config.match_mode}, "
            f"up to {config.max_pages_per_keyword} pages per keyword, "
            f"{config.delay_seconds_min:g}-{config.delay_seconds_max:g}s between requests"
        )
        if fetcher is None:
            own_client = LinkedInClient(config)
            fetcher = own_client
        scanner = Scanner(
            store=store,
            fetcher=fetcher,
            config=config,
            emit=reporter.line,
            sleep=sleep,
            rng=rng,
        )
        try:
            result = scanner.run()
        except ScanSetupError as exc:
            reporter.line(f"Cannot start: {exc}")
            return EXIT_USAGE
        except KeyboardInterrupt:
            reporter.line("Interrupted. Run marked failed; roles already saved are kept.")
            return EXIT_INTERRUPTED

        summary = store.get_run(result.run_id)
        assert summary is not None
        reporter.line(
            f"Run #{result.run_id} {result.status}: {summary.pages} pages, "
            f"{summary.cards_seen} cards, {summary.matched} matched, {summary.new_roles} new, "
            f"{summary.possible_dups} possible duplicates, {summary.throttled} throttled responses"
        )
        if result.note:
            reporter.line(f"Note: {result.note}")
        return {RUN_SUCCESS: EXIT_OK, RUN_PARTIAL: EXIT_PARTIAL, RUN_FAILED: EXIT_FAILED}[
            result.status
        ]
    finally:
        if own_client is not None:
            own_client.close()
        reporter.close()


def main(
    argv: Sequence[str] | None = None,
    *,
    out: TextIO | None = None,
    fetcher: PageFetcher | None = None,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> int:
    stream = out or sys.stdout
    args = build_parser().parse_args(argv)
    db_path = resolve_db_path(args.db)

    try:
        store = open_store(db_path)
    except SchemaTooNewError as exc:
        print(str(exc), file=stream)
        return EXIT_USAGE

    try:
        if args.command == "status":
            return cmd_status(store, stream)
        if args.command == "serve":
            from .web import create_app
            from .web.app import LOOPBACK_HOSTS

            if args.host not in LOOPBACK_HOSTS:
                print(
                    f"WARNING: {args.host} is not a loopback address; the app has no login.",
                    file=stream,
                )
            app = create_app(store, allowed_hosts=LOOPBACK_HOSTS | {args.host})
            app.run(host=args.host, port=args.port, debug=False, threaded=False)
            return EXIT_OK
        try:
            with RunLock(db_path.parent / "scan.lock"):
                return cmd_scan(
                    store,
                    db_path,
                    force=args.force,
                    out=stream,
                    fetcher=fetcher,
                    sleep=sleep,
                    rng=rng,
                )
        except AlreadyRunningError as exc:
            print(str(exc), file=stream)
            return EXIT_USAGE
    finally:
        store.close()

"""Command line interface: scan, score, eval, details, serve, status."""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from datetime import timedelta
from pathlib import Path
from typing import TextIO

from . import __version__
from . import schedule as scheduling
from .client import DetailFetcher, LinkedInClient, PageFetcher
from .config import Config, ConfigError
from .db import SchemaTooNewError, connect, migrate
from .details import fetch_descriptions, waiting_for_description
from .embeddings import Embedder
from .evaluation import build_report, load_labelled, roles_to_score
from .fit_filters import listed_salary, rupee_text
from .lock import AlreadyRunningError, RunLock
from .models import PORTAL_LINKEDIN, PORTAL_NAUKRI, STATUSES, Role, RoleFilter
from .naukri_browser import BrowserUnavailable
from .naukri_run import NaukriRunner, NaukriSource, reminder_text
from .report import Reporter, eval_summary, log_name, phrase_lines, scoring_summary
from .scanner import Scanner, ScanSetupError
from .scoring import (
    DAILY_WINDOW,
    TRIGGER_MANUAL,
    TRIGGER_SCAN,
    Scorer,
    ScoringUnavailable,
    gemini_from_env,
)
from .store import RUN_FAILED, RUN_PARTIAL, RUN_RUNNING, RUN_SUCCESS, Store
from .sync import MailSourceFactory, default_source_factory, sync_once, sync_problem
from .timeutil import format_display, from_iso, now_ist, to_iso

EmbedderFactory = Callable[[Config], Embedder]
DETAILS_BATCH = 60  # most descriptions one details run fetches

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

    sub.add_parser(
        "naukri",
        help="fetch new roles from Naukri in a visible browser window (you solve any captcha)",
    )

    sub.add_parser(
        "naukri-details",
        help="fetch missing Naukri job descriptions in a visible browser window",
    )

    serve = sub.add_parser("serve", help="start the local web app")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=5000)

    sub.add_parser("status", help="show last run and role counts")
    sub.add_parser("sync", help="read Gmail once and update job statuses (Email Sync)")
    sub.add_parser("score", help="score roles against your CV (also runs after each scan)")
    sub.add_parser("eval", help="measure how well the score separates good and bad roles")
    sub.add_parser("details", help="fetch missing descriptions for recent roles")
    sub.add_parser("salary-check", help="show how many stored descriptions list a salary")
    schedule = sub.add_parser("schedule", help="run the scan daily on this Mac (macOS only)")
    schedule.add_argument("action", choices=("on", "off", "status"))
    sub.add_parser("scheduled-run", help="what the daily job calls: one guarded, timed scan")
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
        if latest.status == RUN_RUNNING:
            print(
                f"  in progress: {latest.keywords_done} of {latest.keywords_total} keywords done, "
                f"{latest.new_roles} new roles so far, running for "
                f"{scheduling.elapsed_text(latest.started_on)}",
                file=out,
            )
    naukri_latest = store.latest_run(PORTAL_NAUKRI)
    if naukri_latest:
        print(
            f"Naukri latest run: #{naukri_latest.id} {naukri_latest.status} at "
            f"{format_display(naukri_latest.started_on)} IST",
            file=out,
        )
    reminder = reminder_text(store)
    if reminder:
        print(reminder, file=out)
    print(scheduling.describe_state(scheduling.schedule_state()), file=out)
    print(f"Roles: {store.count_roles()}", file=out)
    try:
        config = Config.from_raw(store.get_config_raw())
    except ConfigError:
        config = Config()
    print(
        f"Descriptions pending: {waiting_for_description(store, config)} "
        f"(roles from the last {config.detail_max_age_days} days)",
        file=out,
    )
    for status in STATUSES:
        print(f"  {status}: {store.count_roles(RoleFilter(status=status))}", file=out)
    return EXIT_OK


def cmd_schedule(store: Store, db_path: Path, action: str, out: TextIO) -> int:
    if action == "off":
        return scheduling.uninstall(out)
    if action == "status":
        print(scheduling.describe_state(scheduling.schedule_state()), file=out)
        return EXIT_OK
    try:
        config = Config.from_raw(store.get_config_raw())
    except ConfigError as exc:
        print(f"Configuration is invalid: {exc}", file=out)
        return EXIT_USAGE
    return scheduling.install(db_path, config, out)


def run_scoring(
    store: Store,
    config: Config,
    trigger: str,
    emit: Callable[[str], None],
    make_embedder: EmbedderFactory,
    sleep: Callable[[float], None],
    only: Sequence[Role] | None = None,
    source: str = "",
) -> bool:
    """Score pending roles (or just `only`) and print the summary.

    False when there was no CV to score against.
    """
    store.fail_stale_scoring_runs(when=to_iso(now_ist()))
    scorer = Scorer(store=store, config=config, make_embedder=make_embedder, emit=emit, sleep=sleep)
    try:
        run = scorer.run(trigger, only=only, source=source)
    except ScoringUnavailable:
        emit("Scoring skipped: no CV uploaded. Upload one on the CV page of the app.")
        return False
    used = store.texts_embedded_since(to_iso(now_ist() - DAILY_WINDOW))
    for line in scoring_summary(run, daily_used=used, daily_limit=config.embed_daily_limit):
        emit(line)
    return True


def score_after_scan(
    store: Store,
    config: Config,
    emit: Callable[[str], None],
    make_embedder: EmbedderFactory,
    sleep: Callable[[float], None],
    source: str = PORTAL_LINKEDIN,
) -> bool:
    """Score new roles at the end of a scan. A scoring problem never changes the scan's result.

    Returns True when the user interrupted scoring.
    """
    try:
        run_scoring(store, config, TRIGGER_SCAN, emit, make_embedder, sleep, source=source)
    except KeyboardInterrupt:
        return True
    except Exception as exc:  # the scan already succeeded; report the bug and keep that result
        emit(f"Scoring failed unexpectedly ({type(exc).__name__}: {exc}). The scan is unaffected.")
    return False


def cmd_score(
    store: Store,
    db_path: Path,
    *,
    out: TextIO,
    make_embedder: EmbedderFactory,
    sleep: Callable[[float], None],
) -> int:
    config = load_config(store, out, "nothing was scored")
    if config is None:
        return EXIT_USAGE
    reporter = Reporter(out, db_path.parent / "runs" / log_name(to_iso(now_ist())))
    try:
        try:
            found_cv = run_scoring(
                store, config, TRIGGER_MANUAL, reporter.line, make_embedder, sleep
            )
        except KeyboardInterrupt:
            return EXIT_INTERRUPTED
        if not found_cv:
            return EXIT_USAGE
        latest = store.list_scoring_runs(1)[0]
        return {"success": EXIT_OK, "partial": EXIT_PARTIAL}.get(latest.status, EXIT_FAILED)
    finally:
        reporter.close()


def load_config(store: Store, out: TextIO, what: str) -> Config | None:
    """The stored configuration, or None after printing why it cannot be used."""
    try:
        return Config.from_raw(store.get_config_raw())
    except ConfigError as exc:
        print(f"Configuration is invalid; {what}:", file=out)
        for key, message in exc.errors.items():
            print(f"  {key}: {message}", file=out)
        return None


def cmd_eval(
    store: Store,
    db_path: Path,
    *,
    out: TextIO,
    make_embedder: EmbedderFactory,
    sleep: Callable[[float], None],
) -> int:
    """Score the rated roles that need it, then report how well the score separates them."""
    config = load_config(store, out, "nothing was evaluated")
    if config is None:
        return EXIT_USAGE
    reporter = Reporter(out, db_path.parent / "runs" / log_name(to_iso(now_ist())))
    try:
        cv = store.get_cv()
        if cv is None:
            reporter.line("Eval needs a CV. Upload one on the CV page of the app.")
            return EXIT_USAGE
        labelled = load_labelled(store, config, cv)
        todo = roles_to_score(labelled)
        if todo:
            reporter.line(f"Eval: scoring {len(todo)} rated role(s) that have no current score")
            try:
                run_scoring(
                    store, config, TRIGGER_MANUAL, reporter.line, make_embedder, sleep, only=todo
                )
            except KeyboardInterrupt:
                return EXIT_INTERRUPTED
            labelled = load_labelled(store, config, cv)
        phrases = [p.phrase for p in store.list_phrases() if p.use_for_match]
        report = build_report(labelled, config, phrases)
        for line in (*eval_summary(report), *phrase_lines(report)):
            reporter.line(line)
        return EXIT_OK
    finally:
        reporter.close()


def cmd_salary_check(store: Store, out: TextIO) -> int:
    """How many stored descriptions list a salary, with examples to check the reading."""
    config = load_config(store, out, "nothing was checked")
    if config is None:
        return EXIT_USAGE
    described = [r for r in store.list_roles() if r.description]
    found = [(r, listed_salary(r.description)) for r in described]
    found = [(r, salary) for r, salary in found if salary is not None]
    print(f"Descriptions stored: {len(described)}", file=out)
    print(f"Listing a salary: {len(found)}", file=out)
    minimum = config.fit_min_salary
    below = sum(1 for _, salary in found if salary is not None and salary < minimum)
    print(
        f"Below the minimum ({rupee_text(minimum)}): {below}" if minimum else "Rule is off",
        file=out,
    )
    for role, salary in found[:10]:
        print(f"  {rupee_text(salary or 0)}  {role.title} at {role.company}", file=out)
    return EXIT_OK


def cmd_details(
    store: Store,
    db_path: Path,
    *,
    out: TextIO,
    detail_fetcher: DetailFetcher | None,
    sleep: Callable[[float], None],
    rng: random.Random | None,
) -> int:
    """Fetch missing descriptions for recent roles, the same ones a scan covers."""
    config = load_config(store, out, "nothing was fetched")
    if config is None:
        return EXIT_USAGE
    reporter = Reporter(out, db_path.parent / "runs" / log_name(to_iso(now_ist())))
    own_client: LinkedInClient | None = None
    try:
        since = to_iso(now_ist() - timedelta(days=config.detail_max_age_days))
        pending = store.pending_details(since, DETAILS_BATCH)
        if not pending:
            reporter.line("No recent roles are waiting for a description.")
            return EXIT_OK
        if detail_fetcher is None:
            own_client = LinkedInClient(config)
            detail_fetcher = own_client
        chance = rng or random.Random()

        def wait(low: float, high: float, why: str | None) -> None:
            seconds = chance.uniform(low, high)
            if why:
                reporter.line(f"Pausing {seconds:.0f}s before {why}")
            sleep(seconds)

        reporter.line(f"Descriptions: fetching {len(pending)} role(s)")
        try:
            outcome = fetch_descriptions(
                store=store,
                fetcher=detail_fetcher,
                pending=pending,
                config=config,
                emit=reporter.line,
                wait=wait,
                clock=now_ist,
            )
        except KeyboardInterrupt:
            reporter.line("Interrupted. Descriptions already saved are kept.")
            return EXIT_INTERRUPTED
        left = len(store.pending_details(since, 1_000_000)) - outcome.failed
        reporter.line(
            f"Descriptions: {outcome.saved} saved, {outcome.failed} without one "
            f"(posting gone), {max(left, 0)} left for later"
        )
        if outcome.stopped:
            reporter.line("Stopped after repeated throttling. Try again later.")
            return EXIT_PARTIAL
        return EXIT_OK
    finally:
        if own_client is not None:
            own_client.close()
        reporter.close()


def cmd_scan(
    store: Store,
    db_path: Path,
    *,
    force: bool,
    out: TextIO,
    fetcher: PageFetcher | None,
    detail_fetcher: DetailFetcher | None = None,
    make_embedder: EmbedderFactory = gemini_from_env,
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
            detail_fetcher = own_client
        scanner = Scanner(
            store=store,
            fetcher=fetcher,
            details=detail_fetcher,
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
        if config.score_after_scan and score_after_scan(
            store, config, reporter.line, make_embedder, sleep
        ):
            return EXIT_INTERRUPTED
        return {RUN_SUCCESS: EXIT_OK, RUN_PARTIAL: EXIT_PARTIAL, RUN_FAILED: EXIT_FAILED}[
            result.status
        ]
    finally:
        if own_client is not None:
            own_client.close()
        reporter.close()


NaukriSourceFactory = Callable[
    [Config, Callable[[str], None]], AbstractContextManager[NaukriSource]
]


def default_naukri_source(
    config: Config, emit: Callable[[str], None]
) -> AbstractContextManager[NaukriSource]:
    from .naukri_browser import BrowserSource

    return BrowserSource(config.naukri_location, emit, max_age_hours=config.naukri_max_age_hours)


def cmd_naukri(
    store: Store,
    db_path: Path,
    *,
    out: TextIO,
    open_source: NaukriSourceFactory,
    make_embedder: EmbedderFactory,
    sleep: Callable[[float], None],
    rng: random.Random | None,
) -> int:
    config = load_config(store, out, "nothing was fetched")
    if config is None:
        return EXIT_USAGE
    stale = store.fail_stale_runs()
    if stale:
        print(f"Marked {stale} unfinished earlier run(s) as failed", file=out)
    reporter = Reporter(out, db_path.parent / "runs" / log_name(to_iso(now_ist())))
    try:
        last_ok = store.last_successful_run(PORTAL_NAUKRI)
        reporter.line(
            "Naukri last successful run: "
            + (
                f"{format_display(last_ok.finished_on or last_ok.started_on)} IST"
                if last_ok
                else "none"
            )
        )
        reporter.line(
            f"A private browser window opens now. Solve any captcha there. Roles posted in the "
            f"last {config.naukri_max_age_hours} hours, up to {config.naukri_max_pages} pages "
            "per keyword."
        )
        try:
            with open_source(config, reporter.line) as source:
                runner = NaukriRunner(
                    store=store,
                    source=source,
                    config=config,
                    emit=reporter.line,
                    sleep=sleep,
                    rng=rng,
                )
                result = runner.run()
        except BrowserUnavailable as exc:
            reporter.line(str(exc))
            return EXIT_USAGE
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
            f"{summary.possible_dups} possible duplicates"
        )
        if result.note:
            reporter.line(f"Note: {result.note}")
        if config.score_after_scan and score_after_scan(
            store, config, reporter.line, make_embedder, sleep, source=PORTAL_NAUKRI
        ):
            return EXIT_INTERRUPTED
        return {RUN_SUCCESS: EXIT_OK, RUN_PARTIAL: EXIT_PARTIAL, RUN_FAILED: EXIT_FAILED}[
            result.status
        ]
    finally:
        reporter.close()


def cmd_naukri_details(
    store: Store,
    db_path: Path,
    *,
    out: TextIO,
    open_source: NaukriSourceFactory,
    make_embedder: EmbedderFactory,
    sleep: Callable[[float], None],
    rng: random.Random | None,
) -> int:
    config = load_config(store, out, "nothing was fetched")
    if config is None:
        return EXIT_USAGE
    stale = store.fail_stale_runs()
    if stale:
        print(f"Marked {stale} unfinished earlier run(s) as failed", file=out)
    reporter = Reporter(out, db_path.parent / "runs" / log_name(to_iso(now_ist())))
    try:
        since = to_iso(now_ist() - timedelta(days=config.detail_max_age_days))
        waiting = len(store.pending_details(since, 1_000_000, PORTAL_NAUKRI))
        if not waiting or config.naukri_max_job_pages <= 0:
            reporter.line("Naukri descriptions: nothing to fetch")
            return EXIT_OK
        reporter.line(
            f"{waiting} Naukri role(s) lack a description. A private browser window opens now; "
            "solve any captcha there. Only roles that pass the Fit filters are opened, up to "
            f"{config.naukri_max_job_pages}."
        )
        try:
            with open_source(config, reporter.line) as source:
                runner = NaukriRunner(
                    store=store,
                    source=source,
                    config=config,
                    emit=reporter.line,
                    sleep=sleep,
                    rng=rng,
                )
                read, _closed, _failed = runner.read_pending()
        except BrowserUnavailable as exc:
            reporter.line(str(exc))
            return EXIT_USAGE
        except KeyboardInterrupt:
            reporter.line("Interrupted. Descriptions already saved are kept.")
            return EXIT_INTERRUPTED
        if (
            read
            and config.score_after_scan
            and score_after_scan(
                store, config, reporter.line, make_embedder, sleep, source=PORTAL_NAUKRI
            )
        ):
            return EXIT_INTERRUPTED
        return EXIT_OK
    finally:
        reporter.close()


def cmd_sync(store: Store, out: TextIO, make_mail_source: MailSourceFactory) -> int:
    """Read the mailbox once. Prints what changed and what needs review."""
    config = Config.from_raw(store.get_config_raw())
    problem = sync_problem(config)
    if problem:
        print(problem, file=out)
        return EXIT_USAGE
    result = sync_once(store, config, make_mail_source)
    if result is None:
        print("A sync is already running.", file=out)
        return EXIT_USAGE
    state = store.get_sync_state()
    if state.error:
        print(f"Sync failed: {state.error}", file=out)
        return EXIT_FAILED
    print(
        f"Checked {result.checked} emails: {result.applied} applied, {result.review} to review, "
        f"{result.ignored} not about a tracked role.",
        file=out,
    )
    for note in result.notes:
        print(note, file=out)
    return EXIT_OK


def main(
    argv: Sequence[str] | None = None,
    *,
    out: TextIO | None = None,
    fetcher: PageFetcher | None = None,
    detail_fetcher: DetailFetcher | None = None,
    make_embedder: EmbedderFactory = gemini_from_env,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
    open_source: NaukriSourceFactory = default_naukri_source,
    make_mail_source: MailSourceFactory = default_source_factory,
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
        if args.command == "salary-check":
            return cmd_salary_check(store, stream)
        if args.command == "sync":
            return cmd_sync(store, stream, make_mail_source)
        if args.command == "serve":
            from .web import create_app
            from .web.app import LOOPBACK_HOSTS

            if args.host not in LOOPBACK_HOSTS:
                print(
                    f"WARNING: {args.host} is not a loopback address; the app has no login.",
                    file=stream,
                )
            from .sync import start_scheduler

            start_scheduler(db_path)
            app = create_app(store, allowed_hosts=LOOPBACK_HOSTS | {args.host})
            app.run(host=args.host, port=args.port, debug=False, threaded=False)
            return EXIT_OK
        if args.command == "scheduled-run":
            return scheduling.run_scheduled(store, db_path, stream)
        if args.command == "schedule":
            return cmd_schedule(store, db_path, args.action, stream)
        try:
            with RunLock(db_path.parent / "scan.lock"):
                if args.command == "eval":
                    return cmd_eval(
                        store, db_path, out=stream, make_embedder=make_embedder, sleep=sleep
                    )
                if args.command == "details":
                    return cmd_details(
                        store,
                        db_path,
                        out=stream,
                        detail_fetcher=detail_fetcher,
                        sleep=sleep,
                        rng=rng,
                    )
                if args.command == "naukri":
                    return cmd_naukri(
                        store,
                        db_path,
                        out=stream,
                        open_source=open_source,
                        make_embedder=make_embedder,
                        sleep=sleep,
                        rng=rng,
                    )
                if args.command == "naukri-details":
                    return cmd_naukri_details(
                        store,
                        db_path,
                        out=stream,
                        open_source=open_source,
                        make_embedder=make_embedder,
                        sleep=sleep,
                        rng=rng,
                    )
                if args.command == "score":
                    return cmd_score(
                        store,
                        db_path,
                        out=stream,
                        make_embedder=make_embedder,
                        sleep=sleep,
                    )
                return cmd_scan(
                    store,
                    db_path,
                    force=args.force,
                    out=stream,
                    fetcher=fetcher,
                    detail_fetcher=detail_fetcher,
                    make_embedder=make_embedder,
                    sleep=sleep,
                    rng=rng,
                )
        except AlreadyRunningError as exc:
            print(str(exc), file=stream)
            return EXIT_USAGE
    finally:
        store.close()

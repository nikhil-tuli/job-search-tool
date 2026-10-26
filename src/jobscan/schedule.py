"""Run the scan once a day on this Mac, with a launchd job.

Nothing here runs in the cloud. `jobscan schedule on` writes a launchd job that calls
`jobscan scheduled-run` at the set time and at login. `scheduled-run` skips the day if a scan
already finished, reads the Gemini key from the Keychain (never from a file), runs the scan with a
time limit and sends a macOS notification when something needs attention.
"""

from __future__ import annotations

import os
import plistlib
import signal
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from .config import Config, ConfigError
from .models import PORTAL_LINKEDIN
from .scoring import ScoreModel
from .store import RUN_FAILED, RUN_PARTIAL, RUN_RUNNING, RUN_SUCCESS, Store
from .timeutil import format_display, from_iso, now_ist, to_iso

LABEL = "com.jobscan.daily"
KEYCHAIN_SERVICE = "jobscan-gemini"
KEY_ENV = "GEMINI_API_KEY"
LAUNCH_AGENTS = Path("~/Library/LaunchAgents")
SECURITY = "/usr/bin/security"
OSASCRIPT = "/usr/bin/osascript"
LAUNCHCTL = "/bin/launchctl"
INTERRUPT_GRACE_SECONDS = 60  # time a scan gets to finish its run record after Ctrl-C

# A scan exits 2 when it declines to start (another scan running, minimum gap, bad config).
EXIT_DECLINED = 2

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]
Notifier = Callable[[str, str], None]
Spawner = Callable[[Sequence[str], Mapping[str, str], float], int | None]


def plist_path(home: Path | None = None) -> Path:
    base = (home / "Library/LaunchAgents") if home else LAUNCH_AGENTS.expanduser()
    return base / f"{LABEL}.plist"


def log_path(db_path: Path) -> Path:
    return db_path.parent / "schedule.log"


def split_time(clock: str) -> tuple[int, int]:
    hour, minute = clock.split(":")
    return int(hour), int(minute)


def build_plist(*, python: str, db_path: Path, clock: str, log: Path) -> bytes:
    """The launchd job: at the set time and at login, call `jobscan scheduled-run`."""
    hour, minute = split_time(clock)
    job = {
        "Label": LABEL,
        "ProgramArguments": [python, "-m", "jobscan", "--db", str(db_path), "scheduled-run"],
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "RunAtLoad": True,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }
    return plistlib.dumps(job)


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False)


def keychain_has_key(runner: Runner = _run) -> bool:
    """True when the Keychain holds the key. The key itself is read and thrown away."""
    result = runner([SECURITY, "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"])
    return result.returncode == 0 and bool(result.stdout.strip())


def keychain_key(runner: Runner = _run) -> str | None:
    result = runner([SECURITY, "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"])
    key = result.stdout.strip()
    return key if result.returncode == 0 and key else None


def notify(title: str, message: str, runner: Runner = _run) -> None:
    """A macOS notification. The text is ours (counts and short notes), escaped for AppleScript."""
    text = message.replace("\\", "\\\\").replace('"', '\\"')
    name = title.replace("\\", "\\\\").replace('"', '\\"')
    runner([OSASCRIPT, "-e", f'display notification "{text}" with title "{name}"'])


# ---- install and remove -------------------------------------------------------------------


@dataclass(frozen=True)
class ScheduleState:
    installed: bool
    loaded: bool
    clock: str | None


def schedule_state(
    runner: Runner = _run, *, home: Path | None = None, uid: int | None = None
) -> ScheduleState:
    path = plist_path(home)
    if not path.exists():
        return ScheduleState(False, False, None)
    try:
        job = plistlib.loads(path.read_bytes())
        when = job["StartCalendarInterval"]
        clock: str | None = f"{when['Hour']:02d}:{when['Minute']:02d}"
    except (plistlib.InvalidFileException, KeyError, ValueError):
        clock = None
    domain = f"gui/{os.getuid() if uid is None else uid}"
    try:
        loaded = runner([LAUNCHCTL, "print", f"{domain}/{LABEL}"]).returncode == 0
    except OSError:  # no launchctl here
        loaded = False
    return ScheduleState(True, loaded, clock)


def install(
    db_path: Path,
    config: Config,
    out: TextIO,
    *,
    runner: Runner = _run,
    python: str | None = None,
    home: Path | None = None,
    uid: int | None = None,
    platform: str | None = None,
) -> int:
    if (platform or sys.platform) != "darwin":
        print("Scheduling uses launchd, so it works on macOS only.", file=out)
        return 2
    path = plist_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path(db_path)
    log.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        build_plist(
            python=python or sys.executable,
            db_path=db_path.resolve(),
            clock=config.schedule_time,
            log=log,
        )
    )
    domain = f"gui/{os.getuid() if uid is None else uid}"
    runner([LAUNCHCTL, "bootout", f"{domain}/{LABEL}"])  # fine if it was not loaded
    result = runner([LAUNCHCTL, "bootstrap", domain, str(path)])
    if result.returncode != 0:
        print(f"launchctl could not load the job: {result.stderr.strip()}", file=out)
        return 1
    print(f"Scheduled: a scan runs daily at {config.schedule_time} IST and at login.", file=out)
    print(f"Log: {log}", file=out)
    if not keychain_has_key(runner):
        print(
            "The Gemini key is not in the Keychain, so scheduled scans will fetch roles but "
            "skip scoring. Store it once (it asks for the key, so it is not typed here):\n"
            f'  security add-generic-password -a "$USER" -s {KEYCHAIN_SERVICE} -w',
            file=out,
        )
    window_days = config.time_window_seconds / 86_400
    if config.fit_recency_days < window_days:
        print(
            f"Note: the scan looks back {window_days:g} days but Fit recency is "
            f"{config.fit_recency_days} day(s), so older roles are excluded from scoring.",
            file=out,
        )
    return 0


def uninstall(
    out: TextIO,
    *,
    runner: Runner = _run,
    home: Path | None = None,
    uid: int | None = None,
) -> int:
    path = plist_path(home)
    domain = f"gui/{os.getuid() if uid is None else uid}"
    runner([LAUNCHCTL, "bootout", f"{domain}/{LABEL}"])
    if path.exists():
        path.unlink()
        print("Schedule removed.", file=out)
    else:
        print("No schedule was set.", file=out)
    return 0


# ---- the scheduled run --------------------------------------------------------------------


def ran_today(store: Store, now_iso: str) -> bool:
    """True when a scan already ended well today (IST), so another is not needed."""
    today = now_iso[:10]
    return any(
        run.started_on[:10] == today and run.status in (RUN_SUCCESS, RUN_PARTIAL)
        for run in store.list_runs(10, PORTAL_LINKEDIN)
    )


def new_strong(store: Store, config: Config, since_iso: str) -> int:
    """Roles fetched since `since_iso` whose score is in the Strong band."""
    model = ScoreModel.from_config(config)
    scores = store.get_scores()
    strong = 0
    for role in store.list_roles(limit=500):
        if role.fetched_on < since_iso:
            break
        score = scores.get(role.key)
        if score is not None and model.final(score) >= config.score_strong_min:
            strong += 1
    return strong


def spawn_with_timeout(
    command: Sequence[str], env: Mapping[str, str], timeout_seconds: float
) -> int | None:
    """Run a command; None if it had to be stopped. Ctrl-C first, so a scan can close its run."""
    process = subprocess.Popen(list(command), env=dict(env))
    try:
        return process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=INTERRUPT_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        return None


def run_scheduled(
    store: Store,
    db_path: Path,
    out: TextIO,
    *,
    env: Mapping[str, str] | None = None,
    runner: Runner = _run,
    spawner: Spawner = spawn_with_timeout,
    notifier: Notifier | None = None,
    python: str | None = None,
) -> int:
    environment = dict(os.environ if env is None else env)
    send: Notifier = notifier or (lambda title, message: notify(title, message, runner))
    try:
        config = Config.from_raw(store.get_config_raw())
    except ConfigError as exc:
        print(f"Scheduled scan not started, configuration is invalid: {exc}", file=out)
        send("Job scanner", "Scheduled scan not started: the configuration is invalid.")
        return 1

    now = to_iso(now_ist())
    if ran_today(store, now):
        print(f"{format_display(now)} IST: a scan already finished today, nothing to do.", file=out)
        return 0

    if not environment.get(KEY_ENV):
        key = keychain_key(runner)
        if key:
            environment[KEY_ENV] = key
        else:
            print("Gemini key not found in the Keychain; scoring will be skipped.", file=out)
            send("Job scanner", "Gemini key is not in the Keychain, so roles will not be scored.")

    command = [python or sys.executable, "-m", "jobscan", "--db", str(db_path), "scan"]
    print(f"{format_display(now)} IST: starting the scheduled scan", file=out)
    out.flush()
    code = spawner(command, environment, config.schedule_timeout_minutes * 60)

    if code is None:
        minutes = config.schedule_timeout_minutes
        print(f"The scan ran past {minutes} min and was stopped.", file=out)
        send("Job scanner", f"Scan stopped after {minutes} min. Run jobscan status.")
        return 1
    if code == EXIT_DECLINED:
        print("The scan declined to start (already running or minimum gap).", file=out)
        return 0

    latest = store.latest_run()
    if code != 0 or latest is None or latest.status in (RUN_FAILED, RUN_RUNNING):
        note = latest.note if latest and latest.note else f"exit code {code}"
        send("Job scanner", f"Scan did not finish: {note[:120]}")
        return 1
    if latest.status != RUN_SUCCESS:
        send("Job scanner", f"Scan ended early: {latest.note[:120] or latest.status}")
        return 0
    if config.schedule_notify_finish:
        strong = new_strong(store, config, latest.started_on)
        send("Job scanner", f"{latest.new_roles} new roles, {strong} Strong.")
    return 0


def describe_state(state: ScheduleState) -> str:
    if not state.installed:
        return "Schedule: off (jobscan schedule on)"
    loaded = "loaded" if state.loaded else "installed but not loaded"
    return f"Schedule: daily at {state.clock or '?'} IST and at login ({loaded})"


def elapsed_text(started_on: str) -> str:
    minutes = int((now_ist() - from_iso(started_on)).total_seconds() // 60)
    return f"{minutes} min"

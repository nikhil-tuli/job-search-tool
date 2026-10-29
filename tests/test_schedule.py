from __future__ import annotations

import io
import plistlib
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from jobscan import cli, schedule
from jobscan.config import Config, ConfigError
from jobscan.models import NewRole
from jobscan.store import RUN_FAILED, RUN_PARTIAL, RUN_SUCCESS, Store
from jobscan.timeutil import now_ist, to_iso

SECRET = "AIza-test-secret-value"


def done(*, returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, "")


@dataclass
class FakeRunner:
    """Stands in for launchctl, security and osascript."""

    key: str | None = SECRET
    load_fails: bool = False
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(command))
        if command[0] == schedule.SECURITY:
            return done(stdout=self.key + "\n") if self.key else done(returncode=44)
        if command[:2] == [schedule.LAUNCHCTL, "bootstrap"] and self.load_fails:
            return done(returncode=5)
        return done()


@dataclass
class FakeSpawner:
    code: int | None = 0
    seen_env: dict[str, str] = field(default_factory=dict)
    calls: int = 0
    seen_timeout: float = 0

    def __call__(
        self, command: Sequence[str], env: Mapping[str, str], timeout: float
    ) -> int | None:
        self.calls += 1
        self.seen_env = dict(env)
        self.seen_timeout = timeout
        return self.code


class Notes:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def __call__(self, title: str, message: str) -> None:
        self.sent.append(message)


def finish_run(store: Store, status: str, note: str = "", *, new_role: bool = False) -> int:
    run = store.start_run(window_seconds=86400, keywords_total=1)
    if new_role:
        store.insert_role(
            NewRole(
                portal="LinkedIn",
                job_id="1",
                title="TPM",
                company="Acme",
                location="Pune",
                link="https://www.linkedin.com/jobs/view/1",
                posted_on=None,
                posted_precision="day",
                fetched_on=to_iso(now_ist()),
                actively_hiring=False,
                matched_phrase="TPM",
                possible_duplicate_of=None,
                dup_key=None,
                raw_datetime=None,
                raw_relative=None,
                run_id=run,
            )
        )
    store.finish_run(run, status, note)
    return run


def go(
    store: Store,
    tmp_path: Path,
    spawner: FakeSpawner,
    notes: Notes,
    *,
    runner: FakeRunner | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[int, str]:
    out = io.StringIO()
    code = schedule.run_scheduled(
        store,
        tmp_path / "jobscan.db",
        out,
        env={} if env is None else env,
        runner=runner or FakeRunner(),
        spawner=spawner,
        notifier=notes,
        python="/usr/bin/python3",
    )
    return code, out.getvalue()


# ---- config -------------------------------------------------------------------------------


def test_schedule_time_is_tidied_and_checked() -> None:
    assert Config.from_raw({"schedule_time": "7:30"}).schedule_time == "07:30"
    for bad in ("25:00", "8", "08:60", "eight", ""):
        with pytest.raises(ConfigError) as caught:
            Config.from_raw({"schedule_time": bad})
        assert "schedule_time" in caught.value.errors


def test_schedule_timeout_bounds() -> None:
    assert Config.from_raw({"schedule_timeout_minutes": "90"}).schedule_timeout_minutes == 90
    with pytest.raises(ConfigError):
        Config.from_raw({"schedule_timeout_minutes": "1"})
    with pytest.raises(ConfigError):
        Config.from_raw({"schedule_notify_finish": "2"})


# ---- the launchd job ----------------------------------------------------------------------


def test_plist_runs_at_the_set_time_and_at_login(tmp_path: Path) -> None:
    raw = schedule.build_plist(
        python="/opt/py/bin/python",
        db_path=tmp_path / "jobscan.db",
        clock="08:05",
        log=tmp_path / "schedule.log",
    )
    job = plistlib.loads(raw)
    assert job["Label"] == schedule.LABEL
    assert job["StartCalendarInterval"] == {"Hour": 8, "Minute": 5}
    assert job["RunAtLoad"] is True
    assert job["ProgramArguments"][0] == "/opt/py/bin/python"
    assert job["ProgramArguments"][-1] == "scheduled-run"
    assert SECRET not in raw.decode()
    assert schedule.KEY_ENV not in raw.decode()


def test_install_writes_and_loads_the_job(tmp_path: Path) -> None:
    runner = FakeRunner()
    out = io.StringIO()
    config = Config.from_raw({"schedule_time": "07:45"})
    code = schedule.install(
        tmp_path / "data" / "jobscan.db",
        config,
        out,
        runner=runner,
        python="/usr/bin/python3",
        home=tmp_path,
        uid=501,
        platform="darwin",
    )
    assert code == 0
    job = plistlib.loads(schedule.plist_path(tmp_path).read_bytes())
    assert job["StartCalendarInterval"] == {"Hour": 7, "Minute": 45}
    verbs = [c[1] for c in runner.calls if c[0] == schedule.LAUNCHCTL]
    assert verbs == ["bootout", "bootstrap"]
    assert "daily at 07:45" in out.getvalue()
    assert SECRET not in out.getvalue()


def test_install_explains_a_missing_keychain_key(tmp_path: Path) -> None:
    out = io.StringIO()
    schedule.install(
        tmp_path / "jobscan.db",
        Config(),
        out,
        runner=FakeRunner(key=None),
        home=tmp_path,
        uid=501,
        platform="darwin",
    )
    assert "security add-generic-password" in out.getvalue()
    assert "-w" in out.getvalue()


def test_install_warns_when_recency_is_shorter_than_the_window(tmp_path: Path) -> None:
    out = io.StringIO()
    config = Config.from_raw({"time_window_seconds": "259200", "fit_recency_days": "1"})
    schedule.install(
        tmp_path / "jobscan.db", config, out, runner=FakeRunner(), home=tmp_path, uid=1,
        platform="darwin",
    )  # fmt: skip
    assert "Fit recency" in out.getvalue()


def test_install_reports_a_load_failure(tmp_path: Path) -> None:
    out = io.StringIO()
    code = schedule.install(
        tmp_path / "jobscan.db",
        Config(),
        out,
        runner=FakeRunner(load_fails=True),
        home=tmp_path,
        uid=501,
        platform="darwin",
    )
    assert code == 1
    assert "could not load" in out.getvalue()


def test_install_refuses_off_macos(tmp_path: Path) -> None:
    out = io.StringIO()
    code = schedule.install(
        tmp_path / "jobscan.db", Config(), out, runner=FakeRunner(), home=tmp_path,
        platform="linux",
    )  # fmt: skip
    assert code == 2
    assert not schedule.plist_path(tmp_path).exists()


def test_uninstall_removes_the_job(tmp_path: Path) -> None:
    runner = FakeRunner()
    schedule.install(
        tmp_path / "jobscan.db", Config(), io.StringIO(), runner=runner, home=tmp_path, uid=1,
        platform="darwin",
    )  # fmt: skip
    out = io.StringIO()
    assert schedule.uninstall(out, runner=runner, home=tmp_path, uid=1) == 0
    assert not schedule.plist_path(tmp_path).exists()
    assert "removed" in out.getvalue()
    again = io.StringIO()
    schedule.uninstall(again, runner=runner, home=tmp_path, uid=1)
    assert "No schedule" in again.getvalue()


def test_state_text(tmp_path: Path) -> None:
    runner = FakeRunner()
    off = schedule.schedule_state(runner, home=tmp_path, uid=1)
    assert "off" in schedule.describe_state(off)
    schedule.install(
        tmp_path / "jobscan.db", Config(), io.StringIO(), runner=runner, home=tmp_path, uid=1,
        platform="darwin",
    )  # fmt: skip
    on = schedule.schedule_state(runner, home=tmp_path, uid=1)
    assert on.installed
    assert on.loaded
    assert "daily at 08:00" in schedule.describe_state(on)


def test_notify_escapes_quotes() -> None:
    runner = FakeRunner()
    schedule.notify('Job "x"', 'it said "no" \\ done', runner)
    script = runner.calls[0][2]
    assert 'it said \\"no\\" \\\\ done' in script
    assert runner.calls[0][0] == schedule.OSASCRIPT


# ---- the scheduled run --------------------------------------------------------------------


def test_skips_when_a_scan_already_finished_today(store: Store, tmp_path: Path) -> None:
    finish_run(store, RUN_SUCCESS)
    spawner, notes = FakeSpawner(), Notes()
    code, text = go(store, tmp_path, spawner, notes)
    assert code == 0
    assert spawner.calls == 0
    assert "already finished today" in text
    assert notes.sent == []


def test_a_failed_scan_today_does_not_block_the_next(store: Store, tmp_path: Path) -> None:
    finish_run(store, RUN_FAILED, "boom")
    spawner = FakeSpawner()
    go(store, tmp_path, spawner, Notes())
    assert spawner.calls == 1


def test_key_comes_from_the_keychain_and_stays_out_of_output(store: Store, tmp_path: Path) -> None:
    spawner, notes = FakeSpawner(), Notes()
    _, text = go(store, tmp_path, spawner, notes)
    assert spawner.seen_env[schedule.KEY_ENV] == SECRET
    assert SECRET not in text
    assert all(SECRET not in n for n in notes.sent)


def test_an_environment_key_is_used_without_asking_the_keychain(
    store: Store, tmp_path: Path
) -> None:
    runner = FakeRunner()
    spawner = FakeSpawner()
    go(store, tmp_path, spawner, Notes(), runner=runner, env={schedule.KEY_ENV: "from-env"})
    assert spawner.seen_env[schedule.KEY_ENV] == "from-env"
    assert not any(c[0] == schedule.SECURITY for c in runner.calls)


def test_missing_key_notifies_but_still_scans(store: Store, tmp_path: Path) -> None:
    spawner, notes = FakeSpawner(), Notes()
    go(store, tmp_path, spawner, notes, runner=FakeRunner(key=None))
    assert spawner.calls == 1
    assert schedule.KEY_ENV not in spawner.seen_env
    assert any("Keychain" in n for n in notes.sent)


def test_timeout_comes_from_config(store: Store, tmp_path: Path) -> None:
    store.set_config_raw({"schedule_timeout_minutes": "20"})
    spawner = FakeSpawner()
    go(store, tmp_path, spawner, Notes())
    assert spawner.seen_timeout == 20 * 60


def test_a_scan_past_the_limit_notifies(store: Store, tmp_path: Path) -> None:
    store.set_config_raw({"schedule_timeout_minutes": "30"})
    notes = Notes()
    code, text = go(store, tmp_path, FakeSpawner(code=None), notes)
    assert code == 1
    assert "stopped" in text
    assert any("30 min" in n for n in notes.sent)


def test_a_failed_scan_notifies_with_its_note(store: Store, tmp_path: Path) -> None:
    class Failing(FakeSpawner):
        def __call__(
            self, command: Sequence[str], env: Mapping[str, str], timeout: float
        ) -> int | None:
            finish_run(store, RUN_FAILED, "Unexpected error: boom")
            return 1

    notes = Notes()
    code, _ = go(store, tmp_path, Failing(), notes)
    assert code == 1
    assert any("boom" in n for n in notes.sent)


def test_a_declined_scan_is_silent(store: Store, tmp_path: Path) -> None:
    notes = Notes()
    code, _ = go(store, tmp_path, FakeSpawner(code=schedule.EXIT_DECLINED), notes)
    assert code == 0
    assert notes.sent == []


def test_success_is_silent_unless_asked(store: Store, tmp_path: Path) -> None:
    class Succeeding(FakeSpawner):
        def __call__(
            self, command: Sequence[str], env: Mapping[str, str], timeout: float
        ) -> int | None:
            finish_run(store, RUN_SUCCESS, new_role=True)
            return 0

    quiet = Notes()
    go(store, tmp_path, Succeeding(), quiet)
    assert quiet.sent == []


def test_success_notifies_when_asked(store: Store, tmp_path: Path) -> None:
    store.set_config_raw({"schedule_notify_finish": "1"})

    class Succeeding(FakeSpawner):
        def __call__(
            self, command: Sequence[str], env: Mapping[str, str], timeout: float
        ) -> int | None:
            finish_run(store, RUN_SUCCESS, new_role=True)
            return 0

    notes = Notes()
    go(store, tmp_path, Succeeding(), notes)
    assert notes.sent
    assert "new roles" in notes.sent[0]


def test_a_partial_scan_notifies(store: Store, tmp_path: Path) -> None:
    class Partial(FakeSpawner):
        def __call__(
            self, command: Sequence[str], env: Mapping[str, str], timeout: float
        ) -> int | None:
            finish_run(store, RUN_PARTIAL, "Stopped after repeated throttling")
            return 3

    notes = Notes()
    go(store, tmp_path, Partial(), notes)
    assert any("throttling" in n for n in notes.sent)


def test_invalid_config_notifies_and_does_not_scan(store: Store, tmp_path: Path) -> None:
    store.set_config_raw({"schedule_time": "nonsense"})
    spawner, notes = FakeSpawner(), Notes()
    code, _ = go(store, tmp_path, spawner, notes)
    assert code == 1
    assert spawner.calls == 0
    assert notes.sent


# ---- stopping a scan that runs too long -----------------------------------------------------


def test_spawn_returns_the_exit_code() -> None:
    assert schedule.spawn_with_timeout([sys.executable, "-c", "raise SystemExit(3)"], {}, 30) == 3


def test_spawn_stops_a_scan_past_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(schedule, "INTERRUPT_GRACE_SECONDS", 10)
    code = schedule.spawn_with_timeout(
        [sys.executable, "-c", "import time; time.sleep(60)"], {}, 0.5
    )
    assert code is None


# ---- command line -------------------------------------------------------------------------


def test_status_shows_a_running_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / "jobscan.db"
    store = cli.open_store(path)
    store.start_run(window_seconds=86400, keywords_total=4)
    store.close()
    out = io.StringIO()
    assert cli.main(["--db", str(path), "status"], out=out) == 0
    text = out.getvalue()
    assert "running" in text
    assert "0 of 4 keywords done" in text
    assert "Schedule:" in text


def test_schedule_status_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    out = io.StringIO()
    assert cli.main(["--db", str(tmp_path / "jobscan.db"), "schedule", "status"], out=out) == 0
    assert "Schedule:" in out.getvalue()


def test_summary_shows_a_running_scan_but_not_the_schedule(client: object, store: Store) -> None:
    store.start_run(window_seconds=86400, keywords_total=5)
    page = client.get("/summary").get_data(as_text=True)  # type: ignore[attr-defined]
    assert "Scan running:" in page
    assert "0 of 5 keywords done" in page
    assert "Schedule:" not in page


def test_summary_day_table_shows_scan_status_and_time(client: object, store: Store) -> None:
    store.insert_role(
        NewRole(
            portal="LinkedIn",
            job_id="7",
            title="TPM",
            company="Acme",
            location="Pune",
            link="https://www.linkedin.com/jobs/view/7",
            posted_on=None,
            posted_precision="day",
            fetched_on=to_iso(now_ist()),
            actively_hiring=False,
            matched_phrase="TPM",
            possible_duplicate_of=None,
            dup_key=None,
            raw_datetime=None,
            raw_relative=None,
            run_id=store.start_run(window_seconds=86400, keywords_total=1),
        )
    )
    page = client.get("/summary").get_data(as_text=True)  # type: ignore[attr-defined]
    head = page.split("<thead>")[1].split("</thead>")[0]
    assert "<th>Scan</th>" in head
    assert '<th class="num">Took</th>' in head
    assert "running" in page


def test_scans_by_day_adds_up_time() -> None:
    from jobscan.web.app import scans_by_day

    @dataclass
    class R:
        started_on: str
        finished_on: str | None
        status: str

    days = scans_by_day(
        [
            R("2026-10-22T08:00:00+05:30", "2026-10-22T08:10:30+05:30", "success"),
            R("2026-10-22T09:00:00+05:30", "2026-10-22T09:00:20+05:30", "failed"),
            R("2026-10-21T08:00:00+05:30", "2026-10-21T08:00:45+05:30", "partial"),
        ]
    )
    assert days["2026-10-22"] == {"status": "failed", "took": "10m 50s"}
    assert days["2026-10-21"] == {"status": "partial", "took": "45s"}


def test_default_time_limit_covers_a_long_scan() -> None:
    assert Config().schedule_timeout_minutes == 180

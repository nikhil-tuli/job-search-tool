from __future__ import annotations

import io
import runpy
from datetime import timedelta
from pathlib import Path

import pytest

from jobscan import cli
from jobscan.cli import EXIT_FAILED, EXIT_INTERRUPTED, EXIT_OK, EXIT_PARTIAL, EXIT_USAGE, main
from jobscan.db import SCHEMA_VERSION
from jobscan.lock import AlreadyRunningError, RunLock
from jobscan.store import RUN_SUCCESS
from jobscan.timeutil import now_ist, to_iso
from tests.fakes import TPM, ScriptedFetcher, card, end, layout, ok, throttled

SCRIPT_OK = {(TPM, 0): [ok(card("1"), card("2", "Chef"))], (TPM, 1): [end()]}


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "data" / "jobscan.db"
    store = cli.open_store(path)
    store.replace_phrases([(TPM, True, True)])
    store.close()
    return path


def run(
    db_path: Path,
    *args: str,
    script: dict | None = None,  # type: ignore[type-arg]
) -> tuple[int, str]:
    out = io.StringIO()
    fetcher = ScriptedFetcher(script if script is not None else {})
    code = main(["--db", str(db_path), *args], out=out, fetcher=fetcher, sleep=lambda _s: None)
    return code, out.getvalue()


def age_runs(db_path: Path, days: int = 2) -> None:
    """Pretend every stored run started and finished some days ago."""
    store = cli.open_store(db_path)
    old = to_iso(now_ist() - timedelta(days=days))
    store.conn.execute("UPDATE runs SET started_on = ?, finished_on = ?", (old, old))
    store.close()


# ---- scan ---------------------------------------------------------------------------------


def test_scan_success(db_path: Path) -> None:
    code, text = run(db_path, "scan", script=SCRIPT_OK)
    assert code == EXIT_OK
    assert "Last successful run: none" in text
    assert "Config OK" in text
    assert "Run #1 success" in text
    assert "1 new" in text
    store = cli.open_store(db_path)
    assert store.count_roles() == 1
    last = store.last_successful_run()
    store.close()
    assert last is not None
    assert last.status == RUN_SUCCESS


def test_scan_writes_a_run_log_with_the_terminal_output(db_path: Path) -> None:
    _, text = run(db_path, "scan", script=SCRIPT_OK)
    logs = list((db_path.parent / "runs").glob("*.log"))
    assert len(logs) == 1
    body = logs[0].read_text()
    assert "Run #1 success" in body
    assert "IST" not in logs[0].name
    assert "Run #1 success" in text


def test_second_scan_inside_min_gap_is_refused(db_path: Path) -> None:
    run(db_path, "scan", script=SCRIPT_OK)
    code, text = run(db_path, "scan", script={})
    assert code == EXIT_USAGE
    assert "Refusing to run" in text
    assert "--force" in text


def test_force_overrides_min_gap(db_path: Path) -> None:
    run(db_path, "scan", script=SCRIPT_OK)
    code, text = run(db_path, "scan", "--force", script=SCRIPT_OK)
    assert code == EXIT_OK
    assert "Last successful run: 20" in text
    assert "0 new" in text


def test_old_last_run_prints_a_window_warning(db_path: Path) -> None:
    run(db_path, "scan", script=SCRIPT_OK)
    age_runs(db_path)
    code, text = run(db_path, "scan", script=SCRIPT_OK)
    assert code == EXIT_OK
    assert "older than the 86400s search window" in text


def test_recent_last_run_has_no_window_warning(db_path: Path) -> None:
    run(db_path, "scan", script=SCRIPT_OK)
    _, text = run(db_path, "scan", "--force", script=SCRIPT_OK)
    assert "WARNING" not in text


def test_stale_running_run_is_marked_failed(db_path: Path) -> None:
    store = cli.open_store(db_path)
    store.start_run(window_seconds=86400, keywords_total=1)
    store.close()
    age_runs(db_path)
    code, text = run(db_path, "scan", script=SCRIPT_OK)
    assert code == EXIT_OK
    assert "Marked 1 unfinished earlier run(s) as failed" in text
    store = cli.open_store(db_path)
    first = store.get_run(1)
    store.close()
    assert first is not None
    assert first.status == "failed"


def test_invalid_config_is_reported_and_nothing_runs(db_path: Path) -> None:
    store = cli.open_store(db_path)
    store.set_config_raw({"max_pages_per_keyword": "0", "match_mode": "fuzzy"})
    store.close()
    code, text = run(db_path, "scan", script={})
    assert code == EXIT_USAGE
    assert "max_pages_per_keyword" in text
    assert "match_mode" in text
    store = cli.open_store(db_path)
    assert store.latest_run() is None
    store.close()


def test_unverified_window_warns(db_path: Path) -> None:
    store = cli.open_store(db_path)
    store.set_config_raw({"time_window_seconds": "604800"})
    store.close()
    _, text = run(db_path, "scan", script=SCRIPT_OK)
    assert "not the verified 86400" in text


def test_partial_run_exit_code(db_path: Path) -> None:
    script = {
        (TPM, 0): [throttled()] * 4,
        (TPM, 1): [ok(card("1"))],
        (TPM, 2): [end()],
    }
    code, text = run(db_path, "scan", script=script)
    assert code == EXIT_PARTIAL
    assert "Run #1 partial" in text
    assert "Note:" in text


def test_failed_run_exit_code(db_path: Path) -> None:
    code, text = run(db_path, "scan", script={(TPM, 0): [layout()]})
    assert code == EXIT_FAILED
    assert "LAYOUT CHANGED" in text
    assert "Run #1 failed" in text


def test_ctrl_c_exits_130_and_marks_run_failed(db_path: Path) -> None:
    code, text = run(db_path, "scan", script={(TPM, 0): [KeyboardInterrupt()]})
    assert code == EXIT_INTERRUPTED
    assert "Interrupted" in text
    store = cli.open_store(db_path)
    latest = store.latest_run()
    store.close()
    assert latest is not None
    assert latest.status == "failed"


def test_no_search_keywords_cannot_start(db_path: Path) -> None:
    store = cli.open_store(db_path)
    store.replace_phrases([(TPM, False, True)])
    store.close()
    code, text = run(db_path, "scan", script={})
    assert code == EXIT_USAGE
    assert "Cannot start" in text


def test_scan_refuses_when_another_scan_holds_the_lock(db_path: Path) -> None:
    with RunLock(db_path.parent / "scan.lock"):
        code, text = run(db_path, "scan", script=SCRIPT_OK)
    assert code == EXIT_USAGE
    assert "already running" in text
    code, _ = run(db_path, "scan", script=SCRIPT_OK)
    assert code == EXIT_OK


def test_lock_is_released_after_a_scan(db_path: Path) -> None:
    run(db_path, "scan", script=SCRIPT_OK)
    with RunLock(db_path.parent / "scan.lock"):
        pass


# ---- status -------------------------------------------------------------------------------


def test_status_on_empty_database(db_path: Path) -> None:
    code, text = run(db_path, "status")
    assert code == EXIT_OK
    assert "Last successful run: none" in text
    assert "Roles: 0" in text
    assert "Not Applied: 0" in text


def test_status_after_a_scan(db_path: Path) -> None:
    run(db_path, "scan", script=SCRIPT_OK)
    _, text = run(db_path, "status")
    assert "Last successful run: 20" in text
    assert "Latest run: #1 success" in text
    assert "Roles: 1" in text
    assert "Not Applied: 1" in text


# ---- plumbing -----------------------------------------------------------------------------


def test_newer_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "new.db"
    store = cli.open_store(path)
    store.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    store.close()
    out = io.StringIO()
    assert main(["--db", str(path), "status"], out=out) == EXIT_USAGE
    assert "newer" in out.getvalue()


def test_db_path_resolution(tmp_path: Path) -> None:
    assert cli.resolve_db_path("a.db", {"JOBSCAN_DB": "b.db"}) == Path("a.db")
    assert cli.resolve_db_path(None, {"JOBSCAN_DB": "b.db"}) == Path("b.db")
    assert cli.resolve_db_path(None, {}) == Path("~/.jobscan/jobscan.db").expanduser()


def test_lock_raises_when_held_twice(tmp_path: Path) -> None:
    first = RunLock(tmp_path / "x.lock")
    second = RunLock(tmp_path / "x.lock")
    first.acquire()
    try:
        with pytest.raises(AlreadyRunningError):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()
    second.release()  # releasing twice is harmless


def test_missing_command_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as info:
        main([], out=io.StringIO())
    assert info.value.code == 2


def test_scan_uses_and_closes_the_real_client_when_none_is_injected(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[ClosingFetcher] = []

    class ClosingFetcher(ScriptedFetcher):
        closed = False

        def close(self) -> None:
            self.closed = True

    def factory(config: object) -> ClosingFetcher:
        fetcher = ClosingFetcher(SCRIPT_OK)
        created.append(fetcher)
        return fetcher

    monkeypatch.setattr(cli, "LinkedInClient", factory)
    out = io.StringIO()
    assert main(["--db", str(db_path), "scan"], out=out, sleep=lambda _s: None) == EXIT_OK
    assert len(created) == 1
    assert created[0].closed


def test_serve_starts_the_app_on_loopback(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "flask.Flask.run",
        lambda self, **kwargs: calls.append(kwargs),  # type: ignore[misc]
    )
    out = io.StringIO()
    assert main(["--db", str(db_path), "serve", "--port", "5055"], out=out) == EXIT_OK
    assert calls == [{"host": "127.0.0.1", "port": 5055, "debug": False, "threaded": False}]
    assert "WARNING" not in out.getvalue()


def test_serve_warns_on_a_non_loopback_host(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("flask.Flask.run", lambda self, **kwargs: None)  # type: ignore[misc]
    out = io.StringIO()
    main(["--db", str(db_path), "serve", "--host", "0.0.0.0"], out=out)
    assert "no login" in out.getvalue()


def test_python_dash_m_entry_point(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["jobscan", "--db", str(db_path), "status"])
    with pytest.raises(SystemExit) as info:
        runpy.run_module("jobscan", run_name="__main__")
    assert info.value.code == EXIT_OK

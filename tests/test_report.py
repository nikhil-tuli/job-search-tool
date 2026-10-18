from __future__ import annotations

import io
from pathlib import Path

from jobscan.report import Reporter, log_name


def test_reporter_without_log_only_prints() -> None:
    out = io.StringIO()
    reporter = Reporter(out)
    reporter.line("hello")
    reporter.line()
    reporter.close()
    reporter.close()
    assert out.getvalue() == "hello\n\n"


def test_reporter_tees_to_a_timestamped_log(tmp_path: Path) -> None:
    out = io.StringIO()
    path = tmp_path / "runs" / "a.log"
    reporter = Reporter(out, path)
    reporter.line("first")
    reporter.close()
    reporter.close()
    assert out.getvalue() == "first\n"
    (entry,) = path.read_text().splitlines()
    assert entry.endswith("  first")
    assert entry[4] == "-"
    assert entry[13] == ":"


def test_log_name_is_filesystem_safe() -> None:
    assert log_name("2026-10-15T09:05:00+05:30") == "2026-10-15T09-05-00.log"

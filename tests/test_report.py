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


# ---- eval summary -------------------------------------------------------------------------


def _report(good: int = 6, bad: int = 6):  # type: ignore[no-untyped-def]
    from jobscan.config import Config
    from jobscan.evaluation import build_report
    from tests.test_evaluation import BAD_LOW, GOOD_HIGH, item, make_role

    found = [item(make_role(f"g{i}", status="Applied"), *GOOD_HIGH) for i in range(good)]
    found += [
        item(make_role(f"b{i}", status="Not relevant", title="Staffing Lead"), *BAD_LOW)
        for i in range(bad)
    ]
    return build_report(found, Config(), ["Technical Program Manager"])


def test_eval_summary_lists_methods_cutoff_and_misses() -> None:
    from jobscan.report import eval_summary

    lines = eval_summary(_report())
    text = "\n".join(lines)
    assert lines[0] == "Eval: 12 labelled roles (6 good, 6 bad), 12 with a current score"
    assert "Directional only" in text
    assert "Keywords only" in text
    assert "Meaning only" in text
    assert "1.00" in text
    assert "Good in top 10" in text
    assert "Best cut-off: score" in text
    assert "keeps 100% of good roles and lets through 0% of bad ones" in text
    assert "Good roles scored lowest:" in text
    assert "Bad roles scored highest:" in text
    assert "Keyword share vs AUC: 0%: 1.00" in text


def test_eval_summary_is_not_flagged_when_reliable() -> None:
    from jobscan.report import eval_summary

    assert "Directional only" not in "\n".join(eval_summary(_report(12, 12)))


def test_eval_summary_when_nothing_can_be_measured() -> None:
    from jobscan.report import eval_summary

    lines = eval_summary(_report(3, 0))
    assert len(lines) == 2
    assert "Need at least one good and one bad" in lines[1]


def test_eval_summary_points_at_the_commands_for_missing_scores() -> None:
    from jobscan.config import Config
    from jobscan.evaluation import build_report
    from jobscan.report import eval_summary
    from tests.test_evaluation import item, make_role

    found = [
        item(make_role("1", status="Applied", description=None), scored=False),
        item(make_role("2", status="Not relevant"), scored=False),
    ]
    text = "\n".join(eval_summary(build_report(found, Config(), [])))
    assert (
        "1 without a description (run jobscan details), 1 not yet scored (run jobscan eval)" in text
    )


def test_phrase_lines_show_yield_and_deny_ideas() -> None:
    from jobscan.report import phrase_lines

    lines = phrase_lines(_report())
    assert "  Search phrases (good / bad roles):" in lines
    assert any("Technical Program Manager: 6 / 6" in line for line in lines)
    assert any(line.startswith("  Deny word ideas") and "staffing (6)" in line for line in lines)


def test_phrase_lines_include_industry_ideas() -> None:
    from dataclasses import replace

    from jobscan.config import Config
    from jobscan.evaluation import build_report
    from jobscan.report import phrase_lines
    from tests.test_evaluation import item, make_role

    found = [
        item(replace(make_role(str(i), status="Not relevant"), industries="Construction"))
        for i in range(2)
    ]
    lines = phrase_lines(build_report(found, Config(), []))
    assert "  Industry ideas (on bad roles, never on good ones): Construction (2)" in lines

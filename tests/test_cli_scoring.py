from __future__ import annotations

import io
from pathlib import Path

import pytest

from jobscan import cli
from jobscan.cli import EXIT_FAILED, EXIT_INTERRUPTED, EXIT_OK, EXIT_PARTIAL, EXIT_USAGE, main
from jobscan.embeddings import EmbedAuthError
from jobscan.models import JobDetail, NewRole, ScoringRun
from jobscan.report import scoring_summary
from jobscan.timeutil import now_ist, to_iso
from tests.fakes import (
    TPM,
    BagEmbedder,
    ScriptedDetails,
    ScriptedFetcher,
    card,
    detail_none,
    detail_throttled,
    end,
    ok,
)
from tests.test_scoring import CV_TEXT, PAYMENTS_JD, WAREHOUSE_JD, upload_cv

SCRIPT = {(TPM, 0): [ok(card("1"))], (TPM, 1): [end()]}


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "data" / "jobscan.db"
    store = cli.open_store(path)
    store.replace_phrases([(TPM, True, True)])
    store.close()
    return path


def seed(db_path: Path, *, cv: bool = True, described: bool = True) -> None:
    """A stored role (with a description) and optionally an uploaded CV."""
    store = cli.open_store(db_path)
    run = store.start_run(window_seconds=86400, keywords_total=1)
    store.insert_role(
        NewRole(
            portal="LinkedIn",
            job_id="900",
            title="Technical Program Manager",
            company="Acme",
            location="Pune",
            link="https://www.linkedin.com/jobs/view/900",
            posted_on=None,
            posted_precision="day",
            fetched_on=to_iso(now_ist()),
            actively_hiring=False,
            matched_phrase=TPM,
            possible_duplicate_of=None,
            dup_key=None,
            raw_datetime=None,
            raw_relative=None,
            run_id=run,
        )
    )
    if described:
        store.save_detail("LinkedIn", "900", JobDetail(PAYMENTS_JD), when=to_iso(now_ist()))
    if cv:
        upload_cv(store, CV_TEXT)
    store.close()


def run_cli(
    db_path: Path,
    *args: str,
    embedder: BagEmbedder | None = None,
    script: dict | None = None,  # type: ignore[type-arg]
) -> tuple[int, str, BagEmbedder]:
    bag = embedder or BagEmbedder()
    out = io.StringIO()
    code = main(
        ["--db", str(db_path), *args],
        out=out,
        fetcher=ScriptedFetcher(script or {}),
        make_embedder=lambda _config: bag,
        sleep=lambda _s: None,
    )
    return code, out.getvalue(), bag


def scores(db_path: Path) -> int:
    store = cli.open_store(db_path)
    try:
        return len(store.get_scores())
    finally:
        store.close()


# ---- jobscan score ---------------------------------------------------------------------------


def test_score_without_cv_is_a_usage_error(db_path: Path) -> None:
    seed(db_path, cv=False)
    code, text, bag = run_cli(db_path, "score")
    assert code == EXIT_USAGE
    assert "no CV uploaded" in text
    assert bag.calls == []


def test_score_success_prints_summary_and_logs_run(db_path: Path) -> None:
    seed(db_path)
    code, text, _ = run_cli(db_path, "score")
    assert code == EXIT_OK
    assert "Scoring run #1 success: 1 scored" in text
    assert "Embeddings:" in text
    assert scores(db_path) == 1
    store = cli.open_store(db_path)
    assert store.list_scoring_runs(1)[0].trigger == "manual"
    store.close()
    assert list((db_path.parent / "runs").glob("*.log"))


def test_score_second_run_reuses_scores(db_path: Path) -> None:
    seed(db_path)
    run_cli(db_path, "score")
    code, _text, bag = run_cli(db_path, "score")
    assert code == EXIT_OK
    assert bag.calls == []


def test_score_partial_when_service_stops(db_path: Path) -> None:
    seed(db_path)
    bag = BagEmbedder(failures=[EmbedAuthError("API key rejected")])
    code, text, _ = run_cli(db_path, "score", embedder=bag)
    assert code in {EXIT_PARTIAL, EXIT_FAILED}
    assert "key" in text.lower()
    assert scores(db_path) == 0


def test_score_with_invalid_config(db_path: Path) -> None:
    seed(db_path)
    store = cli.open_store(db_path)
    store.set_config_raw({"score_sem_floor": "0.9", "score_sem_ceiling": "0.5"})
    store.close()
    code, text, bag = run_cli(db_path, "score")
    assert code == EXIT_USAGE
    assert "score_sem_floor" in text or "score_sem_ceiling" in text
    assert bag.calls == []


def test_score_interrupt_exits_130(db_path: Path) -> None:
    seed(db_path)
    bag = BagEmbedder(failures=[KeyboardInterrupt()])
    code, _text, _ = run_cli(db_path, "score", embedder=bag)
    assert code == EXIT_INTERRUPTED
    store = cli.open_store(db_path)
    assert store.list_scoring_runs(1)[0].status == "failed"
    store.close()


# ---- scoring after a scan ----------------------------------------------------------------------


def test_scan_scores_afterwards(db_path: Path) -> None:
    seed(db_path)
    code, text, bag = run_cli(db_path, "scan", "--force", script=SCRIPT)
    assert code == EXIT_OK
    assert "Scoring run #1" in text
    assert text.index("Run #") < text.index("Scoring run #1")
    assert bag.calls
    store = cli.open_store(db_path)
    assert store.list_scoring_runs(1)[0].trigger == "scan"
    store.close()


def test_scan_can_skip_scoring(db_path: Path) -> None:
    seed(db_path)
    store = cli.open_store(db_path)
    store.set_config_raw({"score_after_scan": "0"})
    store.close()
    code, text, bag = run_cli(db_path, "scan", "--force", script=SCRIPT)
    assert code == EXIT_OK
    assert "Scoring" not in text
    assert bag.calls == []


def test_scan_without_cv_says_scoring_was_skipped(db_path: Path) -> None:
    code, text, _ = run_cli(db_path, "scan", script=SCRIPT)
    assert code == EXIT_OK
    assert "Scoring skipped: no CV uploaded" in text


def test_scoring_failure_does_not_change_the_scan_result(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(db_path)

    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(cli, "run_scoring", explode)
    code, text, _ = run_cli(db_path, "scan", "--force", script=SCRIPT)
    assert code == EXIT_OK
    assert "Scoring failed unexpectedly (RuntimeError: bug)" in text


def test_interrupt_during_post_scan_scoring_exits_130(db_path: Path) -> None:
    seed(db_path)
    bag = BagEmbedder(failures=[KeyboardInterrupt()])
    code, _text, _ = run_cli(db_path, "scan", "--force", embedder=bag, script=SCRIPT)
    assert code == EXIT_INTERRUPTED


# ---- terminal summary --------------------------------------------------------------------------


def make_run(**overrides: object) -> ScoringRun:
    fields: dict[str, object] = {
        "id": 3,
        "trigger": "scan",
        "started_on": "2026-10-19T10:00:00+05:30",
        "finished_on": "2026-10-19T10:01:00+05:30",
        "status": "success",
        "note": "",
        "considered": 10,
        "excluded": 4,
        "selected": 6,
        "no_description": 1,
        "scored": 5,
        "failed": 0,
        "strong": 1,
        "partial": 2,
        "weak": 2,
        "texts_embedded": 30,
        "texts_cached": 7,
        "requests": 2,
        "tokens": 1200,
        "tokens_reported": False,
        "excluded_by_reason": {"recency": 3, "status": 1},
    }
    fields.update(overrides)
    return ScoringRun(**fields)  # type: ignore[arg-type]


def test_summary_lines_cover_scores_skips_and_spend() -> None:
    lines = scoring_summary(make_run(), daily_used=37, daily_limit=1000)
    assert (
        lines[0] == "Scoring run #3 success: 5 scored (1 strong, 2 partial, 2 weak), 0 not scored"
    )
    assert "4 excluded by rule (3 recency, 1 status), 1 without a description" in lines[1]
    assert "30 texts in 2 requests (7 reused), 1200 tokens estimated" in lines[2]
    assert "37 of 1000 texts used in the last 24 hours" in lines[2]
    assert len(lines) == 3


def test_summary_reports_tokens_basis_and_note_and_omits_idle_spend() -> None:
    reported = scoring_summary(make_run(tokens_reported=True), daily_used=0, daily_limit=10)
    assert "tokens reported" in reported[2]
    idle = make_run(requests=0, texts_cached=0, note="Daily limit reached", excluded_by_reason={})
    lines = scoring_summary(idle, daily_used=0, daily_limit=10)
    assert not any("Embeddings" in line for line in lines)
    assert lines[-1] == "  Note: Daily limit reached"
    assert lines[1] == "  Skipped: 4 excluded by rule, 1 without a description"


# ---- jobscan eval ------------------------------------------------------------------------------


def add_labelled(
    db_path: Path, job_id: str, rating: str, title: str, *, description: str | None
) -> None:
    store = cli.open_store(db_path)
    run = store.start_run(window_seconds=86400, keywords_total=1)
    store.insert_role(
        NewRole(
            portal="LinkedIn",
            job_id=job_id,
            title=title,
            company="Acme",
            location="Pune",
            link=f"https://www.linkedin.com/jobs/view/{job_id}",
            posted_on=None,
            posted_precision="day",
            fetched_on=to_iso(now_ist()),
            actively_hiring=False,
            matched_phrase=TPM,
            possible_duplicate_of=None,
            dup_key=None,
            raw_datetime=None,
            raw_relative=None,
            run_id=run,
        )
    )
    if description is not None:
        store.save_detail("LinkedIn", job_id, JobDetail(description), when=to_iso(now_ist()))
    store.set_fit_rating("LinkedIn", job_id, rating)
    store.close()


def test_eval_without_cv_is_a_usage_error(db_path: Path) -> None:
    code, text, bag = run_cli(db_path, "eval")
    assert code == EXIT_USAGE
    assert "needs a CV" in text
    assert bag.calls == []


def test_eval_with_invalid_config(db_path: Path) -> None:
    store = cli.open_store(db_path)
    store.set_config_raw({"score_sem_floor": "0.9", "score_sem_ceiling": "0.5"})
    store.close()
    code, text, _ = run_cli(db_path, "eval")
    assert code == EXIT_USAGE
    assert "nothing was evaluated" in text


def test_eval_scores_labelled_roles_then_reports(db_path: Path) -> None:
    seed(db_path, described=False)
    add_labelled(db_path, "1", "good", "Technical Program Manager", description=PAYMENTS_JD)
    add_labelled(db_path, "2", "bad", "Warehouse Supervisor", description=WAREHOUSE_JD)
    add_labelled(db_path, "3", "good", "Program Manager", description=None)
    code, text, bag = run_cli(db_path, "eval")
    assert code == EXIT_OK
    assert "Eval: scoring 2 rated role(s)" in text
    assert "Eval: 3 rated roles (2 good, 1 bad), 2 with a current score" in text
    assert "1 without a description" in text
    assert "Directional only" in text
    assert "Keywords only" in text
    assert "Meaning only" in text
    assert "Current mix (40% keywords)" in text
    assert "Search phrases (good / bad roles):" in text
    assert bag.calls
    assert scores(db_path) == 2


def test_eval_does_not_rescore_current_scores(db_path: Path) -> None:
    seed(db_path, described=False)
    add_labelled(db_path, "1", "good", "Technical Program Manager", description=PAYMENTS_JD)
    run_cli(db_path, "eval")
    code, text, bag = run_cli(db_path, "eval")
    assert code == EXIT_OK
    assert "scoring" not in text.lower().split("eval: 1 rated")[0]
    assert bag.calls == []


def test_eval_with_nothing_labelled(db_path: Path) -> None:
    seed(db_path)
    code, text, _ = run_cli(db_path, "eval")
    assert code == EXIT_OK
    assert "Eval: 0 rated roles" in text
    assert "Need at least one good and one bad" in text


def test_eval_interrupt_exits_130(db_path: Path) -> None:
    seed(db_path, described=False)
    add_labelled(db_path, "1", "good", "Technical Program Manager", description=PAYMENTS_JD)
    code, _text, _ = run_cli(db_path, "eval", embedder=BagEmbedder(failures=[KeyboardInterrupt()]))
    assert code == EXIT_INTERRUPTED


def test_eval_reports_scoring_trouble_and_still_prints(db_path: Path) -> None:
    seed(db_path, described=False)
    add_labelled(db_path, "1", "good", "Technical Program Manager", description=PAYMENTS_JD)
    bag = BagEmbedder(failures=[EmbedAuthError("API key rejected")])
    code, text, _ = run_cli(db_path, "eval", embedder=bag)
    assert code == EXIT_OK
    assert "key" in text.lower()
    assert "1 not yet scored (run jobscan eval)" in text


# ---- jobscan details ---------------------------------------------------------------------------


def run_details(db_path: Path, details: ScriptedDetails) -> tuple[int, str]:
    out = io.StringIO()
    code = main(
        ["--db", str(db_path), "details"],
        out=out,
        detail_fetcher=details,
        sleep=lambda _s: None,
    )
    return code, out.getvalue()


def test_details_with_nothing_waiting(db_path: Path) -> None:
    seed(db_path, described=False)
    store = cli.open_store(db_path)
    store.conn.execute("UPDATE roles SET fetched_on = '2025-01-01T00:00:00+05:30'")
    store.close()  # too old to be covered
    details = ScriptedDetails()
    code, text = run_details(db_path, details)
    assert code == EXIT_OK
    assert "No recent roles are waiting" in text
    assert details.calls == []


def test_details_fetches_recent_roles_only(db_path: Path) -> None:
    add_labelled(db_path, "1", "good", "Technical Program Manager", description=None)
    add_labelled(db_path, "2", "bad", "Warehouse Supervisor", description=None)
    add_labelled(db_path, "3", "bad", "Old Role", description=None)
    store = cli.open_store(db_path)
    store.conn.execute(
        "UPDATE roles SET fetched_on = '2025-01-01T00:00:00+05:30' WHERE job_id = '3'"
    )
    store.close()
    details = ScriptedDetails({"2": [detail_none("HTTP 404")]})
    code, text = run_details(db_path, details)
    assert code == EXIT_OK
    assert sorted(details.calls) == ["1", "2"]  # the old role is left alone
    assert "1 saved, 1 without one (posting gone), 0 left for later" in text
    store = cli.open_store(db_path)
    role = store.get_role("LinkedIn", "1")
    store.close()
    assert role is not None
    assert role.description == "Description of 1"


def test_details_stops_on_repeated_throttling(db_path: Path) -> None:
    for n in ("1", "2", "3"):
        add_labelled(db_path, n, "good", "Technical Program Manager", description=None)
    details = ScriptedDetails({n: [detail_throttled()] * 9 for n in ("1", "2", "3")})
    code, text = run_details(db_path, details)
    assert code == EXIT_PARTIAL
    assert "Stopped after repeated throttling" in text
    assert len(details.calls) == 5  # "Abort after throttled responses in a row"


def test_details_with_invalid_config(db_path: Path) -> None:
    store = cli.open_store(db_path)
    store.set_config_raw({"max_pages_per_keyword": "0"})
    store.close()
    code, text = run_details(db_path, ScriptedDetails())
    assert code == EXIT_USAGE
    assert "nothing was fetched" in text


def test_details_interrupt_exits_130(db_path: Path) -> None:
    add_labelled(db_path, "1", "good", "Technical Program Manager", description=None)

    class Interrupting:
        def fetch_detail(self, job_id: str):  # type: ignore[no-untyped-def]
            raise KeyboardInterrupt

    out = io.StringIO()
    code = main(
        ["--db", str(db_path), "details"],
        out=out,
        detail_fetcher=Interrupting(),
        sleep=lambda _s: None,
    )
    assert code == EXIT_INTERRUPTED
    assert "Interrupted" in out.getvalue()


def test_the_scan_still_fetches_descriptions_through_the_shared_code(db_path: Path) -> None:
    out = io.StringIO()
    details = ScriptedDetails()
    code = main(
        ["--db", str(db_path), "scan"],
        out=out,
        fetcher=ScriptedFetcher(SCRIPT),
        detail_fetcher=details,
        make_embedder=lambda _config: BagEmbedder(),
        sleep=lambda _s: None,
    )
    assert code == EXIT_OK
    assert details.calls == ["1"]
    assert "Descriptions: 1 saved, 0 without one, 0 left for later" in out.getvalue()


def test_salary_check_counts_descriptions_that_list_a_salary(db_path: Path) -> None:
    seed(db_path, cv=False, described=False)
    store = cli.open_store(db_path)
    store.save_detail("LinkedIn", "900", JobDetail("CTC 6-9 LPA, Pune"), when=to_iso(now_ist()))
    store.close()
    code, text, _ = run_cli(db_path, "salary-check")
    assert code == EXIT_OK
    assert "Descriptions stored: 1" in text
    assert "Listing a salary: 1" in text
    assert "Below the minimum (\u20b910 lakh): 1" in text
    assert "\u20b96 lakh  Technical Program Manager at Acme" in text


def test_status_shows_how_many_descriptions_are_pending(db_path: Path) -> None:
    seed(db_path, cv=False, described=False)
    code, text, _ = run_cli(db_path, "status")
    assert code == EXIT_OK
    assert "Descriptions pending: 1 (roles from the last 3 days)" in text
    store = cli.open_store(db_path)
    store.save_detail("LinkedIn", "900", JobDetail("CTC 6-9 LPA"), when=to_iso(now_ist()))
    store.close()
    assert "Descriptions pending: 0" in run_cli(db_path, "status")[1]

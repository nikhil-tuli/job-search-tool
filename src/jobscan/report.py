"""Terminal progress output that is also written to a per-run log file."""

from __future__ import annotations

from pathlib import Path
from typing import IO, TextIO

from .evaluation import MIN_EACH, MIN_TOTAL, EvalReport
from .models import ScoringRun
from .timeutil import format_display, now_ist, to_iso


class Reporter:
    """Prints each line to the terminal and appends it to a log file with an IST timestamp."""

    def __init__(self, out: TextIO, log_path: Path | None = None) -> None:
        self._out = out
        self._log: IO[str] | None = None
        self.log_path = log_path
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log = log_path.open("a", encoding="utf-8")

    def line(self, text: str = "") -> None:
        stamp = format_display(to_iso(now_ist()))
        print(text, file=self._out, flush=True)
        if self._log is not None:
            self._log.write(f"{stamp}  {text}\n")
            self._log.flush()

    def close(self) -> None:
        if self._log is not None:
            self._log.close()
            self._log = None


def log_name(started: str) -> str:
    """File name for a run log, from an ISO timestamp: 2026-10-15T09-05-00.log."""
    return started[:19].replace(":", "-") + ".log"


def scoring_summary(run: ScoringRun, *, daily_used: int, daily_limit: int) -> list[str]:
    """Terminal lines for a finished scoring run: what was scored, skipped and spent."""
    lines = [
        f"Scoring run #{run.id} {run.status}: {run.scored} scored "
        f"({run.strong} strong, {run.partial} partial, {run.weak} weak), {run.failed} not scored"
    ]
    skipped = [f"{n} {reason}" for reason, n in sorted(run.excluded_by_reason.items())]
    lines.append(
        f"  Skipped: {run.excluded} excluded by rule"
        + (f" ({', '.join(skipped)})" if skipped else "")
        + f", {run.no_description} without a description"
    )
    if run.requests or run.texts_cached:
        basis = "reported" if run.tokens_reported else "estimated"
        lines.append(
            f"  Embeddings: {run.texts_embedded} texts in {run.requests} requests "
            f"({run.texts_cached} reused), {run.tokens} tokens {basis}; "
            f"{daily_used} of {daily_limit} texts used in the last 24 hours"
        )
    if run.note:
        lines.append(f"  Note: {run.note}")
    return lines


def _pct(value: float) -> str:
    return f"{round(value * 100)}%"


def _auc(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def eval_summary(report: EvalReport) -> list[str]:
    """Terminal lines for the eval: how well the score tells good roles from bad ones."""
    lines = [
        f"Eval: {report.labelled} rated roles ({report.good} good, {report.bad} bad), "
        f"{report.scored} with a current score"
    ]
    if report.no_description or report.unscored:
        lines.append(
            f"  Not scored: {report.no_description} without a description, "
            f"{report.unscored} not yet scored (run jobscan eval)"
        )
    if not report.measurable:
        lines.append("  Need at least one good and one bad role with a score to measure anything.")
        return lines
    if not report.reliable:
        lines.append(
            f"  Directional only: want {MIN_EACH}+ good, {MIN_EACH}+ bad and {MIN_TOTAL}+ scored "
            "roles before trusting these numbers."
        )
    lines.append(
        f"  {'Method':<28}{'AUC':>6}{'Avg good':>10}{'Avg bad':>9}  Good in top {report.top_n}"
    )
    for v in report.variants:
        lines.append(
            f"  {v.name:<28}{_auc(v.auc):>6}{v.mean_good:>10.0f}{v.mean_bad:>9.0f}  "
            f"{v.top_hits} (random: {report.random_hits:.1f})"
        )
    lines.append(
        "  AUC: chance a good role outscores a bad one (0.5 is a coin toss, 1.0 is perfect)."
    )
    if report.best_weight is not None:
        sweep = ", ".join(f"{w}%: {_auc(a)}" for w, a in report.sweep)
        lines.append(f"  Keyword share vs AUC: {sweep}")
        lines.append(f"  Best keyword share on this data: {report.best_weight}%")
    if report.cutoff is not None:
        c = report.cutoff
        lines.append(
            f"  Best cut-off: score {c.score} keeps {_pct(c.caught_good)} of good roles "
            f"and lets through {_pct(c.let_through_bad)} of bad ones"
        )
    for title, misses in (
        ("Good roles scored lowest", report.missed_good),
        ("Bad roles scored highest", report.false_good),
    ):
        if misses:
            lines.append(f"  {title}:")
            lines.extend(f"    {m.final:>3}  {m.role.title} ({m.role.company})" for m in misses)
    return lines


def phrase_lines(report: EvalReport) -> list[str]:
    """Terminal lines on which search phrases bring good and bad roles, and deny-word ideas."""
    lines = []
    if report.phrases:
        lines.append("  Search phrases (good / bad roles):")
        lines.extend(f"    {p.phrase}: {p.good} / {p.bad}" for p in report.phrases)
    if report.suggestions:
        words = ", ".join(f"{s.word} ({s.bad_roles})" for s in report.suggestions)
        lines.append(f"  Deny word ideas (in bad titles, never in good ones): {words}")
    if report.industry_suggestions:
        names = ", ".join(f"{s.word} ({s.bad_roles})" for s in report.industry_suggestions)
        lines.append(f"  Industry ideas (on bad roles, never on good ones): {names}")
    return lines

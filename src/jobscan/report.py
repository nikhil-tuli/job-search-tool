"""Terminal progress output that is also written to a per-run log file."""

from __future__ import annotations

from pathlib import Path
from typing import IO, TextIO

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

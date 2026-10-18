"""Terminal progress output that is also written to a per-run log file."""

from __future__ import annotations

from pathlib import Path
from typing import IO, TextIO

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

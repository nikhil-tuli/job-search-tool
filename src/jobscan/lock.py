"""Single-instance guard for scan runs (advisory file lock, released when the process ends)."""

from __future__ import annotations

import fcntl
from pathlib import Path
from types import TracebackType
from typing import IO

from .timeutil import format_display, now_ist, to_iso


class AlreadyRunningError(RuntimeError):
    """Another scan holds the lock."""


class RunLock:
    def __init__(self, path: Path, label: str = "") -> None:
        self._path = path
        self._label = label
        self._handle: IO[str] | None = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            who = describe_holder(self._path)
            raise AlreadyRunningError(
                f"another run is already running ({who})"
                if who
                else "another run is already running"
            ) from None
        handle.seek(0)
        handle.truncate()
        handle.write(f"{self._label}|{to_iso(now_ist())}")
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None

    def __enter__(self) -> RunLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


def holder(path: Path) -> tuple[str, str] | None:
    """(command, start time) of the process holding the lock, or None when it is free."""
    try:
        handle = path.open("a+")
    except OSError:
        return None
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.seek(0)
            label, _, started = handle.read().partition("|")
            return label, started
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return None
    finally:
        handle.close()


def describe_holder(path: Path) -> str:
    found = holder(path)
    if found is None or not found[0]:
        return ""
    label, started = found
    return f"jobscan {label}" + (f", started {format_display(started)} IST" if started else "")

#!/bin/sh
# Run a scan and keep the Mac awake until it finishes. Extra arguments go to `jobscan scan`.
set -eu
cd "$(dirname "$0")/.."
if [ -x .venv/bin/jobscan ]; then JOBSCAN=.venv/bin/jobscan; else JOBSCAN=jobscan; fi
if command -v caffeinate >/dev/null 2>&1; then
  exec caffeinate -i "$JOBSCAN" scan "$@"
fi
exec "$JOBSCAN" scan "$@"

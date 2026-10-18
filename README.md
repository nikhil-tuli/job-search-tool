# jobscan

Personal LinkedIn job scanner. Fetches new roles from LinkedIn's logged-out guest search,
keeps only titles you list, removes duplicates, and tracks application status in a local
web app. Everything stays on your machine (SQLite). Nothing is scheduled; you run it.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

Python 3.10 or newer.

## Use

```sh
scripts/scan.sh                 # scan, keeping the Mac awake (caffeinate)
.venv/bin/jobscan scan          # same without caffeinate
.venv/bin/jobscan scan --force  # ignore the minimum gap between runs
.venv/bin/jobscan serve         # web app at http://127.0.0.1:5000
.venv/bin/jobscan status        # last run and counts, in the terminal
```

- Data lives in `~/.jobscan/` (`jobscan.db`, per-run logs in `runs/`). Override with
  `--db PATH` or `JOBSCAN_DB`.
- A scan prints progress per page and writes the same lines to `runs/<timestamp>.log`.
- Exit codes: 0 success, 3 partial (some pages or keywords incomplete), 1 failed, 2 refused
  (bad config, too soon, another scan running).
- Ctrl-C is safe: the run is marked failed and roles already saved are kept.

Title phrases, pacing, page cap and the other settings are edited on the web app's Config
page and apply from the next scan.

On the Roles list, clicking a row opens its details in a side panel (edit status, Applied On,
contact and comments without leaving the list). Esc or the dimmed area closes it, and the up and
down arrow keys move between roles. Each role also keeps its own page at
`/roles/<portal>/<job_id>`, which is what you get with JavaScript off or in a new tab.

Changing a status dropdown saves in place without reloading, so your scroll position stays. The
bulk bar sticks to the top while you scroll, and confirmation messages float at the bottom.

The Summary tab counts roles by the day they were first fetched (IST) and their current status.
Every number links to the matching roles, and days with no new roles show as zero so a missed
scan is visible. Status is the current one, since no status history is kept.

## How it behaves

- Searches India, posted in the last 24 hours, one search per phrase marked "Search".
- A role is kept only if its title contains a phrase marked "Match" (whole words,
  case-insensitive, "Sr" read as "Senior"). Everything else is dropped and listed on the Run
  Log for a few days so you can see what was filtered.
- Same LinkedIn job ID is never inserted twice; your status and notes are never overwritten.
  Same company + title + city under a new ID is stored and flagged "Possible duplicate".
- All times are IST. Posted On is computed from LinkedIn's relative text ("22 hours ago"),
  so it is accurate to the hour for recent posts and to the day or coarser for older ones.
- Throttling: 8-12 s between requests, 30-60 s between keywords, up to 3 retries per page
  after a 30-60 s wait, run aborts after 5 throttled responses in a row.

## First live run (not yet verified)

LinkedIn could not be reached from where this was built, so live behaviour is unverified.
On your Mac:

1. `.venv/bin/jobscan scan` and watch the terminal.
2. Expect pages of 10 cards and a final "end of results" line per keyword. Any
   `throttled` lines mean LinkedIn is pushing back; raise the delays on the Config page.
3. `LAYOUT CHANGED` means LinkedIn altered its HTML; the run fails instead of saving
   anything wrong, and the parser in `src/jobscan/parser.py` needs updating.
4. Open `jobscan serve`, check the Roles list against what linkedin.com shows.

## Development

```sh
.venv/bin/python -m pytest --cov=jobscan   # tests (warnings are errors)
.venv/bin/ruff format . && .venv/bin/ruff check .
.venv/bin/mypy
```

Layout: `parser` (HTML to cards), `matching` (title filter, duplicate key), `client` (one HTTP
request, classified), `scanner` (paging, pacing, retries, stop rules), `store`/`db`
(SQLite and migrations), `cli`, `web/` (Flask app). Schema changes go in a new migration in
`db.py`; never edit a shipped one.

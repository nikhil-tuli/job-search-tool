# jobscan

A personal job-search tracker. It finds new roles on LinkedIn, filters them to the titles you
care about, and helps you track every application in one place. Everything stays on your computer.

## Problem statement

- Finding relevant roles means searching LinkedIn by hand, every day, with many keywords.
- The same job appears again and again, so you review it more than once.
- Application progress ends up scattered across notes and spreadsheets.
- Nothing shows how many roles came in each day or how many are still untouched.

## Objectives

- **Find** recently posted roles (last 24 hours by default; the time window is set on the Config
  page) for the job titles you choose.
- **Filter** out titles that don't match, so you only see relevant roles.
- **Avoid repeats:** never store the same job twice, and flag likely re-posts.
- **Track** each role through its status, with the date applied, a contact and comments.
- **Stay private and simple:** runs on your machine, no account, no cloud, no schedule.
- **Be safe with LinkedIn:** scan slowly, back off when throttled, never lose saved data.

## Current status

**Working end to end** on a Mac. The first live run fetched real roles.

What it does today:

- **Scan:** LinkedIn only (public, logged-out search), India, started by you from the terminal.
  The posting window is 24 hours by default and can be changed on the Config page.
- **Titles:** 13 default title phrases (for example Technical Program Manager, TPM, Delivery
  Manager). Edit them on the Config page.
- **Duplicates:** the same job is never stored twice. Same company, title and city under a new
  ID is flagged "Possible duplicate".
- **Roles list:** search, filter by status, export to CSV, and change a status without leaving
  the page. Clicking a row opens a side panel to edit status, date applied, contact and comments.
- **Statuses:** Applied, Closed, Initial screening rejected, Interview
  Ongoing, Interview cleared, Interview phase rejected, Not Applied, Not relevant, Skipped.
- **Summary tab:** roles per fetch day by status. Every number links to those roles.
- **Run Log:** what each scan did, plus the titles it filtered out.
- **Quality:** automated tests, linting and type checks all pass.

Known limits:

- LinkedIn only. No other job portals yet.
- Work type (remote, hybrid, on-site) is not captured.
- A re-post with a differently worded location (for example "Pune District" versus
  "Pune/Pimpri-Chinchwad Area") can slip past the duplicate flag.
- Status history isn't kept. The Summary shows each role's current status.
- Only the 24-hour search window has been verified to work with LinkedIn.
- Scans are slow on purpose (8 to 12 seconds between requests) to avoid being throttled.

## How to use this app

**1. Set up (once).** You need Python 3.10 or newer.

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

**2. Fetch new roles.** Run this in a terminal, ideally once a day.

```sh
scripts/scan.sh                 # scan and keep the Mac awake
.venv/bin/jobscan scan          # same, without keeping the Mac awake
```

- It prints progress and takes a while, because it pauses between requests.
- Ctrl-C is safe. Roles already saved are kept.

**3. Open the app.**

```sh
.venv/bin/jobscan serve         # then open http://127.0.0.1:5000
```

**4. Work through the roles.**

- **Roles tab:** newest first. Click a row to open its details in a side panel.
    - Set a status, the date applied, a contact and comments, then Save.
    - Esc closes the panel, and the up and down arrow keys move between roles.
- **Status dropdown on a row:** saves instantly and keeps your scroll position.
- **Several roles at once:** tick the boxes, pick a status in the bar at the top, then click
  Apply to selected.
- **Summary tab:** see how many roles each day brought in and where they stand. Click a number
  to jump to those roles.
- **Run Log tab:** check that scans ran properly and see which titles were filtered out.
- **Config tab:** edit title phrases, search settings and scan pacing. Changes apply from the
  next scan.

**Other commands**

```sh
.venv/bin/jobscan status        # last run and counts, in the terminal
.venv/bin/jobscan scan --force  # scan even if one ran very recently
```

**Where your data is**

- Everything is in `~/.jobscan/` (`jobscan.db` plus a log per run in `runs/`).
- Use `--db PATH` or the `JOBSCAN_DB` setting to keep it somewhere else.
- To back up the database while the app is running, use
  `sqlite3 ~/.jobscan/jobscan.db ".backup backup.db"`. Don't just copy the file.

**If something looks wrong**

- `throttled` lines mean LinkedIn is slowing you down. Raise the delays on the Config page.
- `LAYOUT CHANGED` means LinkedIn changed its page. The run stops without saving anything wrong,
  and the parser in `src/jobscan/parser.py` needs updating.
- Exit codes: 0 success, 3 partial, 1 failed, 2 not started (bad config, too soon, or another
  scan running).

  **Disclaimer - Uses LinkedIn’s public, logged-out listings. For personal use. Check LinkedIn’s terms before using.**

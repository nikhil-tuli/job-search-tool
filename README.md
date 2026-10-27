# jobscan

A personal job-search assistant. It finds new roles, ranks them against your CV, tailors your
resume and cover letter, and keeps your application statuses up to date. Everything runs on
your own computer.

## What it does

- **Finds roles:** scans LinkedIn and Naukri for the job titles you choose.
- **Removes noise:** skips repeats and flags roles that don't fit (too junior, wrong company, etc.).
- **Ranks by fit:** scores each role 0 to 100 against your CV (Strong, Partial, Weak).
- **Tailors documents:** suggests resume and cover-letter changes for a role. You review and
  approve every change before a Word file is made.
- **Updates statuses:** reads your Gmail for rejections, interview invites and closures, and
  proposes status changes.
- **Tracks everything:** status, date applied, contact and comments for every role.
- **Tracks follow-ups:** mark a role as needing a follow-up with a recruiter or hiring manager,
  set the next date, and filter for what is due, upcoming or overdue. No key needed.

## The screens

- **Roles:** all roles, newest first. Click one for details, status and tailoring buttons.
- **Summary:** roles per day and where they stand.
- **Eval:** checks how well the score matches your own ratings.
- **Manage CV:** upload your CV and cover letter template; History lists tailored documents.
- **Email Sync:** status changes found in your mail, waiting for review or applied.
- **Config:** titles, filters, scan speed and other settings.

## Get started

You need Python 3.10 or newer. Built and tested on a Mac; Linux should work. Windows should work
for everything except the daily scheduler, but it has not been tested. On Windows, use
`.venv\Scripts\jobscan` in place of `.venv/bin/jobscan`.

1. **Install (once).** In a terminal, inside the project folder:
   ```sh
   python3 -m venv .venv
   .venv/bin/pip install -e '.[dev]'
   ```
2. **Open the app.**
   ```sh
   .venv/bin/jobscan serve
   ```
   Then visit http://127.0.0.1:5000 in your browser.
3. **Fetch roles.** In a second terminal (Naukri needs `pip install -e '.[naukri]'` once):
   ```sh
   .venv/bin/jobscan scan        # LinkedIn
   .venv/bin/jobscan naukri      # Naukri (opens a browser; you solve any captcha)
   ```
   Scans are slow on purpose to avoid being blocked. Stopping with Ctrl-C is safe.

   - **LinkedIn** can run daily on its own on a Mac (`jobscan schedule on`).
   - **Naukri** is manual only: run the command whenever you want fresh roles.
4. **Upload your CV** on Manage CV. Use a Word (.docx) file if you want tailoring.

## Optional features and their keys

Each needs a free key or password, set in the terminal before running `jobscan serve`.
Never paste keys into chats or files.

| Feature | What you need | Variable |
|---|---|---|
| Fit scores | Google Gemini key | `GEMINI_API_KEY` |
| Resume and cover letter tailoring | Cerebras key | `CEREBRAS_API_KEY` |
| Email Sync | Gmail address (set in Config) and a Gmail app password | `JOBSYNC_APP_PASSWORD` |

```sh
export GEMINI_API_KEY=your-key
```

- **Scoring:** run `.venv/bin/jobscan score` after uploading a CV, or let the next scan do it.
- **Tailoring:** upload your Word CV and, for letters, your Word cover letter template. On a
  role, choose **Tailor resume** or **Tailor cover letter**, then follow progress in History.
- **Email Sync:** it only reads mail; it never sends or deletes. Syncs run while `jobscan serve`
  is open, or on demand with the Sync button.

## Privacy

- Your CV, roles and settings stay on your computer in `~/.jobscan/`.
- Name, phone, email and links are never sent to any AI service.
- Scoring sends CV and role text to Google; tailoring sends role and CV experience text to Cerebras.

## Good to know

- Fit scores and AI suggestions are aids, not decisions. Check them before you rely on them.
- Tailored documents never add skills, employers, dates or numbers that are not in your CV.
- Free-tier limits cap how many roles can be scored per day; leftovers are scored next time.
- If LinkedIn or Naukri changes its page layout, the scan stops safely and the parser needs an update.

## More detail

- Full requirements and decisions: `docs/REQUIREMENTS.md`.
- Commands (all start with `jobscan`):
  - `serve`: open the app
  - `scan`: fetch LinkedIn roles
  - `naukri`: fetch Naukri roles
  - `naukri-details`, `details`: fetch missing job descriptions (Naukri, LinkedIn)
  - `score`: score roles against your CV
  - `sync`: read Gmail once and propose status changes
  - `status`: show the last run and counts
  - `eval`: check how well scores match your ratings
  - `schedule on|off|status`: daily LinkedIn scan (Mac only)
- Back up your data by copying `~/.jobscan/` while the app is closed.

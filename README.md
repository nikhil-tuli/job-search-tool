# jobscan

A personal job-search assistant. It finds new roles, ranks them against your CV, tailors your
resume and cover letter, and keeps your application statuses up to date. Everything runs on
your own computer.

## What it does

- **Finds roles:** scans LinkedIn, Naukri and the career pages of companies you list, for the job titles you choose.
- **Removes noise:** skips repeats and flags roles that don't fit (too junior, wrong company, etc.).
- **Ranks by fit:** an AI (Mistral) reads each role against your CV and rates it Good, Partial or
  Bad, with a one-line reason and a checklist of the role's must-haves.
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
- **Eval:** compares the AI's rating with your own 👍 and 👎 on roles.
- **Manage CV:** upload your CV and cover letter template; History lists tailored documents.
- **Email Sync:** status changes found in your mail, waiting for review or applied.
- **Config:** titles, filters, scan speed, the company list for Company sites and other settings.

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
   - **Company sites:** add companies and their career links on the Config page (Test checks each link), then run `.venv/bin/jobscan sites`. It also runs daily with the LinkedIn scan.
4. **Upload your CV** on Manage CV. Use a Word (.docx) file if you want tailoring.

## Optional features and their keys

Each needs a free key or password, set in the terminal before running `jobscan serve`.
Never paste keys into chats or files.

| Feature | What you need | Variable |
|---|---|---|
| Fit scores (AI) | Mistral key (free plan) | `MISTRAL_API_KEY` |
| Resume and cover letter tailoring | Groq key | `GROQ_API_KEY` |
| Email Sync | Gmail address (set in Config) and a Gmail app password | `JOBSYNC_APP_PASSWORD` |

```sh
read -s MISTRAL_API_KEY && export MISTRAL_API_KEY   # paste the key, press Enter; nothing is shown
```

- **Scoring:** a Mistral key is not tied to a model. The model is a setting: Config, Fit scoring
  (AI), Model (currently `mistral-small-2603`). Run `.venv/bin/jobscan score` after uploading a CV,
  or let the next scan do it. Only roles that pass your Fit filters and have a description are scored.
  Changing the role focus, model or CV does not re-score old roles by itself: use `jobscan score --stale`,
  or press Score on a single role.
- **Tailoring:** upload your Word CV and, for letters, your Word cover letter template. On a
  role, choose **Tailor resume** or **Tailor cover letter**, then follow progress in History.
- **Email Sync:** it only reads mail; it never sends or deletes. Syncs run while `jobscan serve`
  is open, or on demand with the Sync button.

## Challenges and how they were handled

**Portals offer no job-search API**
- No portal offers a usable job-search API for individuals, so roles are read from the public pages.
- **LinkedIn:** guest search, with deliberately slow scans to avoid being blocked.
- **Naukri:** its captcha blocks plain requests, so it is a manual, semi-automatic run in a visible browser. You may need to watch the window and solve a captcha.
- **Hosting:** the tool stays local for now. Cloud hosting is postponed.

**Fit scores were not trustworthy**
- The old keyword-and-meaning score is replaced by an AI rating: Good, Partial or Bad.
- The AI reads your CV, the role description and your Role focus text, and returns the rating with a reason.
- The Eval tab compares the AI's rating with your own 👍 and 👎.
- Tuning is ongoing. The Role focus text and Extra rules are adjusted from roles the AI got wrong.

**Free-tier limits**
- **Gemini:**
  - The old scoring used Gemini embeddings: 100 requests and 30,000 tokens a minute, 1,000 requests a day.
  - For AI scoring, the stronger Flash models allow about 20 requests a day, and Flash-Lite about 500.
- **Mistral:**
  - Switched to a Mistral model because of these limits.
  - `ministral-14b` allows 40,000 tokens a minute and generous daily limits, and is now used for the AI role scoring.
  - `mistral-large-4` is a reasoning model and failed with errors (HTTP 400), so use a non-reasoning model.
- **Pacing:** the default is 4 AI calls a minute, one 60-second wait and retry after a rate-limit error, and a daily cap of 150 calls.

## Privacy

- Your CV, roles and settings stay on your computer in `~/.jobscan/`.
- Name, phone, email and links are never sent to any AI service.
- Scoring sends CV and role text to Mistral (the free plan allows Mistral to train on it);
  tailoring sends role and CV experience text to Groq.

## Good to know

- Fit scores and AI suggestions are aids, not decisions. Check them before you rely on them.
- Tailored documents never add skills, employers, dates or numbers that are not in your CV.
- Free-tier limits cap how many roles can be scored per day (Config sets a daily cap, default 150
  AI calls); leftovers are scored next time.
- The old keyword-and-meaning scoring is still in the code. Turn it back on with Config, Legacy
  scoring.
- Company career pages on custom or unsupported systems are listed as "Not supported yet".
- If LinkedIn or Naukri changes its page layout, the scan stops safely and the parser needs an update.

## More detail

- Full requirements and decisions: `docs/REQUIREMENTS.md`.
- Email Sync logic, flowcharts and states: `docs/EMAIL_SYNC_REQUIREMENTS.md`.
- Commands (all start with `jobscan`):
  - `serve`: open the app
  - `scan`: fetch LinkedIn roles
  - `naukri`: fetch Naukri roles
  - `sites`: fetch roles from the company career pages in Config
  - `naukri-details`, `details`: fetch missing job descriptions (Naukri, LinkedIn)
  - `score`: score new roles against your CV with the AI (`score --stale` also redoes roles scored under older settings)
  - `sync`: read Gmail once and propose status changes
  - `status`: show the last run and counts
  - `eval`: check how well old-style scores match your ratings (only with Legacy scoring on; the
    Eval screen covers AI scores)
  - `salary-check`: show how many stored descriptions mention a salary
  - `push-traces`: send AI call details and your ratings to Langfuse (optional, off by default)
  - `schedule on|off|status`: daily LinkedIn scan and company sites fetch (Mac only)
- Eval also shows how your AI calls went (errors, time, tokens, what the service said). To view them in
  Langfuse as well, turn on Config, Tracing (Langfuse) and run `jobscan push-traces`; role text and
  the CV are not sent unless you choose to send role text.
- Back up your data by copying `~/.jobscan/` while the app is closed.

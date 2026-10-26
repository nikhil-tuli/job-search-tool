# Job Scanner: Requirements

Last updated 19 Oct 2026. Kept in the repo and updated with each change.

**Build status:** All four phases are built: the rule filter, CV upload, chunking, keyword and embedding scores, the scoring run log, the Score column, filter and sort, and Phase 4 (Fit rating, `jobscan eval` and the Eval page). Calibrating the weights and bands from your own ratings is the step that remains, and it needs your data. Not yet checked against the live Gemini service (the build sandbox cannot reach Google), so the first real run is the test of the request format and the quota counting.

## Purpose and scope

A personal, local-first LinkedIn job scanner and tracker for a Technical Program Manager job search in Bengaluru. The new goal is to rank roles by how well they fit the CV, at zero cost.

- Runs on one Mac: Python, SQLite, Flask. Scans are started from the terminal.
- For personal use only, not hosted for others.
- Nothing is silently deleted or hidden. Every filter leaves the role visible with a reason.
- Everything new must be free to run.

## Already built

- **Scan:** LinkedIn guest search with paced, throttle-aware requests. 13 default title phrases, editable on the Config page. Time window is configurable (default last 24 hours).
- **Tracking:** 9 statuses in workflow order: Not Applied, Not relevant, Closed, Skipped, Applied, Initial screening rejected, Interview phase rejected, Interview Ongoing, Interview cleared. Dropdowns and filters list them A to Z.
- **Side panel:** clicking a role opens a panel instead of a new page. Up/down arrows and Esc work. Status changes save in the background and the scroll position is kept.
- **Summary tab:** a pivot with one row per fetch date, a Fetched count and every status as a column. Only the cells are clickable.
- **Job descriptions:** fetched for new roles (one request per role, 8 to 12 seconds apart). Shown in the panel with seniority, employment type, industries and job function, and included in search. Config: per-run cap (default 60, 0 turns it off) and recent-roles window. A failed fetch is retried up to 3 times and never fails a scan.
- **Rule filter (Phase 1):** see the next section.
- **README:** problem statement, objectives, current status, how to use.
- **Known limitation:** the same job can show twice when the location text differs, for example "Pune District" and "Pune/Pimpri-Chinchwad Area". Fix deferred.

## CV upload and rule filter

**CV (built)**

- Upload the CV (PDF, DOCX or text) on the CV page. The extracted text is stored locally. Limits: 5 MB, 20 PDF pages, 100,000 characters.
- Replacing the CV marks every score stale. Stale scores are recomputed by `jobscan score` or at the next scan, cheaply, because the role vectors are cached. Re-uploading the same text changes nothing.
- The CV page shows how the CV was split, so missing content is visible. The name and contact lines are dropped before anything is sent.

**Rule filter (built).** Runs before scoring. Every value is a setting in the "Fit filters" group on the Config page and applies at once.

| Rule | Behaviour | Default |
| --- | --- | --- |
| Recency | Skip roles fetched more than X days ago (fetched date, not posted date) | 1 day |
| Years of experience | Skip when the role asks for fewer years than the minimum. The highest minimum named in the description counts. No stated years means keep | 3 years |
| Seniority | Skip intern, junior and trainee in the title, and LinkedIn levels Internship and Entry level. "Associate" counts as junior unless followed by director, VP, vice, principal, partner or head | Editable word and level lists |
| Excluded companies | Case-insensitive whole words, so "Amazon" also catches "Amazon Development Centre" but not "Amazonian Foods" | Amazon, Intuit |
| Already actioned | Skip roles with these statuses | Applied, Closed, Skipped, Not relevant |
| Deny keywords | Matched on the title only, as whole words or phrases, never the description | sales, warehouse, SAP functional |
| Excluded industries | LinkedIn's own Industries line for the role, matched by whole name, any case (for example Staffing and Recruiting, Construction). Only roles whose description has been fetched have one | None (you choose) |

**What a filtered role looks like:** it stays in the list, unscored, with a grey tag such as "Excluded: company". Hover shows the full reason. A "+2" means two more reasons. The side panel lists them all. The Roles page has an Any fit, Eligible and Excluded filter, and the CSV export follows it.

## Scoring pipeline (Phases 2 and 3)

Roles that pass the rule filter get one fit score from 0 to 100, built from a keyword score and a meaning score.

1. **Prepare text (built).** Descriptions are split by heading into requirement, responsibility, company and other parts, then bullets are packed into chunks of about 40 words (setting; 100 at most, and fragments under 6 words are merged). Packing replaced one chunk per bullet because real descriptions have about 13 chunks that way against about 25 one per bullet, which would halve the daily quota. Without clear headings every chunk gets weight 1. CV chunks are one per achievement bullet with employer and title in front, plus paragraphs and unused sections such as education (36 on the real CV).
2. **Keyword score (built, a deviation).** The plan was the `rank-bm25` library. BM25 ranks documents against a query, but here the CV is a single document and the roles are the queries, so it does not fit. Instead: each role term is weighted by IDF over all stored descriptions and by its chunk's section weight, then coverage = the weighted share of the role's important terms that appear in the CV, with BM25-style saturation (k1 1.2). Terms are lowercased, stemmed lightly and stripped of stop words and digits. Local, no library, no API.
3. **Embeddings (built).** Gemini `gemini-embedding-001` via REST, key from `GEMINI_API_KEY`, 768 dimensions (truncated and re-normalised in our code), task type SEMANTIC_SIMILARITY. Texts are sent in batches of 20 and cached by text hash and model, so an unchanged text is never sent twice.
4. **Cosine similarity (built).** For each role chunk, the closest CV chunk (top K, default 1), averaged with section weights: requirement 2, responsibility 1, company 0 (settings). Company chunks are not embedded.
5. **Merge (built).** Each raw score is mapped to 0 to 100 between a floor and a ceiling (keyword 0.08 to 0.20, meaning 0.50 to 0.85), then combined: 40% keyword, 60% meaning. Bands: Strong 70 or more, Partial 50 to 69, Weak below 50. All of these are settings.
6. **LLM review (not planned for now).** Left out of the first release. Revisit if the eval shows costly misses, such as ops roles scoring high.

**Storage (built).** All in SQLite, no vector database. Only the raw keyword and meaning values are stored per role, with a fingerprint of everything that produced them (chunker version, CV, model and dimensions, chunk weights, top K, chunk size, description). Weights, floors, ceilings and bands are applied when read, so changing them takes effect at once with no re-scoring. A fingerprint mismatch shows the score as Stale. Vectors are float32 BLOBs keyed by text hash and `model@dimensions`, so vectors from different models are never compared.

**Limits (built).** A sliding-window limiter keeps to 90 texts and 25,000 tokens a minute. The daily limit (1,000 texts, counted over a rolling 24 hours from the run log) stops a run cleanly as partial. Rate-limit and temporary errors are retried up to 4 times with growing waits. A bad key stops the run at once. Roles not reached are scored on the next run. Measured on the real database with a stand-in embedder: 21 eligible roles took 283 texts in 19 requests.

**Reading the score.** It is a relative rank, not a percentage of fit. A role scoring 78 looks closer to the CV than one scoring 60. The floors, ceilings, weights and bands are guesses until calibrated by marking about 20 roles as good or bad (Phase 4).

## When scoring runs

- **Automatic (built):** at the end of every scan, after descriptions are fetched, unless `score_after_scan` is 0. It scores roles that pass the rule filter and have a description, plus any whose score is stale. With no CV it prints "Scoring skipped".
- **Terminal (built):** `jobscan score` does the same on demand, for example after a CV change. Re-scoring after a CV change is deliberately done here, not by the web app, so a long run is never tied to a browser request.
- **On demand (built):** a Score / Re-score button in the side panel. It works on any role, including excluded ones.
- **No description yet:** the role shows no score until one is fetched.
- A scoring failure never fails a scan, and Ctrl-C during scoring exits 130 and marks the run failed.

## Scoring run log and visibility

Every scoring run (automatic, button or re-score) is recorded and shown in the app.

- **Where (built):** a "Scoring" block on the Summary page, a table of the last 10 runs, and the summary lines at the end of the scan or `score` output.
- **Headline:** last scored time, trigger, and duration.
- **Counts per run:**
    - roles considered
    - excluded by rule, split by reason (recency, years, seniority, company, status, title keyword)
    - unscored because no description yet
    - scored, split into strong, partial and weak fit (bands from calibration)
    - failed or throttled
- **Usage:** chunks embedded, requests made, tokens used (taken from the API if returned, otherwise estimated), and the share of the daily request limit used.
- Token counts are marked estimated (characters divided by 3) unless the API reported them.
- Scores are saved wave by wave, so an interrupted run keeps what it finished.

## Labels and evals (Phase 4, built)

**Labels**

- A role is Good or Bad only when you rate it yourself: 👍 Good or 👎 Poor in the side panel. Clear removes the rating.
- A status never makes a role Good or Bad.
- A rated role shows a thumb next to its title. The panel says "Counts in the eval" or "Not in the eval".
- The rating is stored on the role (schema version 8).

**Eval (`jobscan eval` and the Eval page)**

- It checks whether the fit score ranks your Good roles above your Poor roles. Only rated roles count, and only those with a score can be tested.
- `jobscan eval` scores the rated roles that have a description but no current score, then prints the report.
- The Eval page shows the same report. It never scores anything itself.
- `jobscan details` fetches missing descriptions for recent roles, the same ones a scan covers (the last 3 days by default), for example after a throttle. Older roles are not fetched.
- The order is: descriptions, then scores, then ratings.
- The report shows:
    - a funnel: roles with a current score (Strong, Partial, Weak), then those that are also rated (good, bad)
    - for keywords only, meaning only and the current mix: AUC, the average score of good roles, the average score of bad roles, and how many good roles are in the top 10
    - AUC at every 10% keyword share, and the best share
    - the cut-off score that keeps the most good roles and lets through the fewest bad ones
    - the good roles scored lowest and the bad roles scored highest
    - "Directional only" until there are at least 5 good, 5 bad and 20 scored roles
- The eval never changes settings. It only shows what to change and where.

**Cutting irrelevant roles**

- **Search phrase yield:** per search phrase, how many rated roles were good and bad, so an over-broad phrase shows up.
- **Deny word ideas:** title words in at least 2 bad roles and no good one, not already denied, and not part of a search phrase. One click adds the word to the title deny words.
- **Industry ideas:** LinkedIn industries on at least 2 bad roles and no good one. One click adds the industry to Excluded industries.
- Both only tag roles Excluded. Nothing is hidden or deleted, and each can be removed on the Config page.

## Display, config, privacy, errors

- **Roles list (built):** a Fit column (Eligible or the exclusion tag) and a Score column with a band pill. Filters: Any score, Strong, Partial, Weak, Unscored. Sort: Best fit first (unscored last). The CSV export follows both and adds Fit Score and Fit Band.
- **Advanced filters (built):** the Roles page keeps search, status and possible duplicates in the main bar. Fit, Score and the sort (Newest first or Best fit first) sit in a collapsible Advanced filters section. It opens by itself, with an "on" badge, whenever one of them is active.
- **Side panel (built):** the Fit filter result with reasons, the score with its parts, a Score / Re-score button, and the Good fit / Poor fit rating.
- **Eval page (built):** a nav tab with the report above.
- **Config (built):** the Fit filters group, an Excluded industries setting in the Fit filters group, a Scoring group (weights, floors, ceilings, bands, chunking) and an Embeddings (Gemini) group (model, dimensions, batch size, rate and daily limits). Invalid pairs such as floor above ceiling are rejected.
- **API key:** an environment variable or an untracked local file. Never in the database or git.
- **Privacy:** the CV and descriptions go to Google's free tier. Accepted for now. Google's pricing page says free-tier content is used to improve its products for most models, and this is not confirmed for embeddings.
- **Rate limits and failures:** small batches, back off when throttled, retry on the next scan. A scoring failure never fails a scan.
- **Cost:** zero.

## Deferred items, open questions, build order

**Deferred**

- Duplicate-location fix (declined for now).
- Tech versus ops filter for "Program Manager" titles. Open: auto-mark ops titles "Not relevant" or only tag them. Fit scoring may cover most of this.
- Work type (remote, hybrid, on-site): not in the posting page.
- Hosting for others: advised against because of LinkedIn data.
- Git commit and push: commands given, to be run by Nikhil.

**Open questions**

- LLM review: not in the first release. Revisit after the eval.
- Whether a batched embedding request counts as one request or one per text against the Gemini quota (100 per minute, 30K tokens per minute, 1K per day) is not verified. The build assumes the worse case, one per text, so it can only under-use the quota. Check the AI Studio usage meter after the first real run. The batch request body and response shape were written from the API reference and are also unverified.
- Score weights and bands: calibrate on about 20 rated roles, using the Eval page.

**Build order**

1. Rule filter, Config group and exclusion tags. **Built.**
2. CV upload, chunking and keyword score. **Built.**
3. Gemini embeddings, cosine, merge and the scoring run log. **Built.**
4. Fit rating, `jobscan eval`, `jobscan details`, the Eval page and the relevance tools. **Built.** Calibrating the weights and bands needs about 20 or more rated roles.
5. LLM review: not planned.

## Round 5: scheduled scan and panel changes (built)

Agreed on 22 Oct 2026 and built the same day. Not yet checked on a real Mac: the launchd, Keychain and notification steps were tested against stand-ins, because the build sandbox is Linux. The README is not changed, and from now on it is only changed after the user has reviewed the proposed text.

### A. Scheduled daily scan (opt-in, built)

This reverses the earlier "no schedule" goal, so it stays off until switched on.

- **Where it runs:** on the user's Mac, not in the cloud. The database, CV and browser session are local, and a cloud IP is more likely to be blocked. A macOS `launchd` job runs `jobscan scan` once a day, at a time the user sets (for example 8am). The scan already scores new roles at the end.
- **Commands:** `jobscan schedule on` and `jobscan schedule off` write and remove the job. `jobscan status` shows the schedule state.
- **Missed runs:** `launchd` runs a job missed during sleep when the Mac wakes. It does not run a job missed while the Mac was off, so the job also runs at login, and skips the run if a scan already completed that day.
- **Search window (configurable):** the scheduled scan does not default to the last 24 hours. It uses a Config value for how far back to look (the existing `time_window_seconds`, sent to LinkedIn as `f_TPR=r<seconds>`), so a skipped day can be covered by widening it. Two cautions: only 86400 seconds has been confirmed against LinkedIn, and the scan prints a warning at the start for any other value, so a wider value must be tested on the first run. The scan already warns when the last successful run is older than the window, which is how a gap is flagged. `fit_recency_days` (default 1) also drops roles older than that from scoring, so it must be raised to match, or older roles found by a wider scan are excluded. The wider the window, the more roles and texts each run uses, against the 1,000 daily limit.
- **Gemini key:** the job reads it from the macOS Keychain at run time. It is never written to a file, a log, the job definition or a URL.
- **Absolute paths:** the job uses absolute paths to Python and the project, because it has no shell setup.
- **Time limit:** a wrapper stops the scan after a set time (default 180 minutes, since scans have taken up to about 2.5 hours), so a hang cannot block the next day, and sends a notification.
- **Overlap:** the existing `scan.lock` file refuses a second scan while one runs, so a scheduled run and a manual run cannot overlap.
- **Throttling:** unchanged. Listing pages retry `retry_count` times (3) and abort after `abort_after_consecutive_throttles` (5) consecutive throttles. Description fetching stops after 2 throttles and the roles are retried on the next run. No claim is made that a schedule is throttled less than manual use.
- **Notifications:** a macOS notification on failure or timeout. An optional setting, off by default, adds one when a scan ends, for example "6 new roles, 2 Strong".
- **Logs:** each run writes a log file with no secrets in it.
- **Status of a running job:** a run is recorded as `running` from its start, so `jobscan status` shows "Latest run: #N running". Live progress, such as keywords done and roles found so far, is not stored yet. The plan is to update the run record as the scan advances and show it in `jobscan status` and on the Summary page, with start time, elapsed time and the last result.
- **Not included:** Naukri. Its terms prohibit scraping and it uses bot detection. If wanted later, the first option to check is parsing Naukri's job alert emails.

**How it works as built**

- **Commands:** `jobscan schedule on`, `schedule off` and `schedule status`. `jobscan scheduled-run` is what the job calls. `jobscan status` and the Summary page show the schedule state.
- **Config group "Scheduled scan":** run time (HH:MM, IST), stop-after minutes (default 180) and notify-when-a-scan-ends (0 or 1). Changing the time needs `jobscan schedule on` again, because launchd holds the time.
- **Keychain:** store the key once with `security add-generic-password -a "$USER" -s jobscan-gemini -w`, which asks for the key so it is not typed on the command line. `schedule on` prints this if the key is missing.
- **Skip rule:** the run is skipped when a scan that ended well (success or partial) started today. A failed scan does not block the next attempt.
- **Time limit:** at the limit the scan gets Ctrl-C, so it can close its run record. If it is still running 60 seconds later it is killed.
- **Notifications:** failure, timeout, a partial run, a missing key and an invalid config always notify. Success notifies only if the setting is 1, and says how many new roles and how many are Strong. A scan that declines to start (another running, or the minimum gap) is silent.
- **Running status:** no new storage. A run is saved as running at its start and each keyword's result is saved as it finishes, so `jobscan status` and the Summary page show keywords done, new roles so far and elapsed time. The Summary page needs a refresh to update.
- **Window note:** `schedule on` prints a note when Fit recency is shorter than the scan window.
- **Panel changes (B):** the Link row reads "Open on LinkedIn" (the address is in the tooltip). Status and Applied On share a row. The eval note under Your rating is one short line. The comments box grows as the user types, up to a limit.

### B. Side panel: less scrolling (built)

- Posted date: remove the "(LinkedIn said ...)" text from the column. It stays in the tooltip.
- Fit filter: show all exclusion reasons on one line as short tags, wrapping only when there is no room.
- Score this role: move it onto the same row as the Fit score.
- Your rating: fit in one or two lines (Good and Bad buttons and a short label).
- Role description: a smaller default height, since it has its own scroll.
- Comments: a shorter box that grows as the user types.
- Aim: remove or reduce the panel scroll without removing needed content. The full description stays reachable.

### C. Config page (built in the last build)

- "Texts per request" is no longer a setting. It is fixed at 20.
- The Title phrases table has no Remove column. Clearing the text deletes a phrase. The Phrase boxes are wider.
- The hints for "Texts per minute" and "Texts per 24 hours" say Google counts each text as one request. A dashboard check showed about 400 requests for 364 texts, so the app counts texts.

### D. Settled answers

- Chunk size stays at 40 words. 100 words would halve the texts but blur each chunk's meaning, mismatch the short CV chunks, compress scores, and force a re-embed. 60 is the safer step if quota ever matters.
- Raising texts per request does not reduce usage, because Google counts each text separately.
- A text is one chunk of a description or CV (about 290 characters, 39 words), not a word. A role averages about 11 to 13 chunks, and the cap is 40.

## Q&A

**Is "reduce to 768 numbers" quantization?**

- No. It is dimension reduction: keep the first 768 of the 3072 numbers.
- Quality barely drops. Google's benchmark for `gemini-embedding-001` shows 67.99 at 768 against 68.16 at 2048.
- With `-001`, our code re-normalises the shorter vector before comparing.

**When does scoring run?**

- Automatically after every scan, for new roles that pass the rule filter.
- On demand with the "Score this role" button.
- Roles with no description stay unscored until one is fetched.

**How is the meaning score calculated?**

- Split the role and the CV into pieces. For each role piece, find the closest CV piece. Average these best matches, counting requirements twice, responsibilities once and company text not at all.
- Example, with made-up numbers:

| Role piece | Type | Closest CV piece | Similarity | Weight |
| --- | --- | --- | --- | --- |
| Own delivery of payment gateway integrations | Requirement | Led PSP integrations at Swiggy | 0.82 | 2 |
| Manage stakeholders across engineering teams | Requirement | Cross-functional delivery with 5 teams | 0.78 | 2 |
| Drive cloud migration programs | Responsibility | Cloud cost optimization program | 0.60 | 1 |
| We are a fast-growing fintech | About the company | Nothing close | 0.30 | 0 |

- Weighted average: (0.82 x 2 + 0.78 x 2 + 0.60 x 1) / 5 = 0.76. The company line has weight 0, so it is ignored.
- That raw value is mapped to 0 to 100 using the calibration, and then merged with the keyword score.

**Will semantic search already separate ops roles from tech roles?**

- Mostly for domain. A supply chain ops role usually sits further from a payments or software CV than a tech program role does.
- It is weaker on borderline roles, on numbers such as years, and on negations.
- Years: the rule filter handles the minimum. Nothing handles "too senior" yet.
- The eval will show whether ops roles still score high.

**Why no LLM review for now?**

- Keyword score and embeddings give a score only, with no reasons.
- An LLM could add reasons and catch ops, seniority and years mismatches.
- It is left out of the first release. Revisit if the eval shows costly misses.

**How are roles marked good or bad?**

- From statuses: Applied and interview statuses are good, Not relevant is bad.
- From a thumbs up/down Fit rating on every role, which overrides the status.

**What are the default weights, and can they be changed?**

- 40% keyword, 60% embeddings. Chunk weights: requirement 2, responsibility 1, company 0.
- All are settings. They are starting guesses, tuned with the eval.

**Why show filtered roles instead of hiding them?**

- Nothing is lost, and the grey tag tells you why a role was skipped.

**Is a vector database needed?**

- No. At hundreds to a few thousand roles, vectors sit in SQLite and cosine runs in Python.

**What leaves the machine?**

- The CV text and role descriptions are sent to Google to create embeddings.
- Rule filter, keyword score, cosine and scoring run locally.

**What is the Gemini free-tier quota?**

- Checked in AI Studio for Gemini Embedding 1: 100 requests per minute, 30,000 tokens per minute, 1,000 requests per day.
- The code is built for low limits: small batches, backoff, retry on the next scan.

**How are descriptions chunked, and how many chunks can a role have?**

- The description is split by section heading: requirement ("Requirements", "Qualifications"), responsibility ("What you'll do") or company ("About us", "Benefits").
- Each section is cut into chunks of about 100 words or fewer, one bullet or short paragraph each. A long section gives several chunks.
- Every chunk keeps its section's weight: requirement 2, responsibility 1, company 0.
- A role can have many chunks, for example 3 requirement, 5 responsibility and 2 company. A cap per role bounds the number of API calls.
- Company chunks are not embedded at all, which saves API calls.
- No clear headings means every chunk gets weight 1.

**Why do requirement chunks get 2x?**

- Requirements are what the employer screens on, so a match there matters most.
- 2, 1 and 0 are starting guesses, not derived numbers. They are settings and the eval can tune them.

**Will the score rise if a role chunk matches several CV chunks?**

- No. Only the best CV match per role chunk counts. One strong match answers "is this need covered?".
- This stops a CV that repeats a topic in many bullets from beating one that says it once.
- "Matches per chunk" (k) is a setting, default 1. The eval compares k = 1 against 2 or 3.

**How long does scoring take for about 50 new roles?**

- Estimate, not measured: about 4 to 8 minutes. About 750 chunks is roughly 100K tokens, and the 30K tokens-per-minute limit sets the floor at about 4 minutes.
- Keyword score and cosine take milliseconds. Almost all the time is waiting on the API.
- Descriptions are fetched first (8 to 12 seconds per role), then scoring runs.
- To be measured after the build.

**Does scoring start automatically, and can I use the app meanwhile?**

- Yes, it starts at the end of each scan, after descriptions are fetched.
- The scan and scoring run in the terminal, separate from the web app, so the app stays usable.
- Scores are saved in small batches, so they appear as they finish.
- The "Score this role" button runs inside the web app for about 1 to 3 seconds. The app handles one request at a time, so other clicks wait briefly.

**How will I see when scoring last ran, and its numbers?**

- A "Scoring" block on the Summary page, a run history table, and a line at the end of the scan output.
- It shows last run time, roles considered, excluded by reason, scored, strong, partial and weak counts, failures, chunks embedded, tokens used and share of the daily limit.

**Where does this document live?**

- In the repo as docs/REQUIREMENTS.md, updated with each code change.

**What did the real data show?**

- 23 of 123 roles in the first database copy had descriptions, from 174 to 1,424 words, median about 550.
- Section headings vary a lot ("Key responsibilities", "Minimum qualifications", "What we look for"), so heading detection needs a wide list and a fallback.
- Years appear in many shapes ("1-5 years", "5 to 10 years", "8+ years", "Minimum 5 years"). The highest minimum named is used, so a role is only excluded when every figure is low.

## Roles list paging (built)
- Shows 20 roles per page by default.
- "Rows per page" offers 20, 50 or 100. Changing it returns to page 1.
- The choice is remembered in the browser (cookie).
- Previous and Next links appear above and below the table. Filters carry over.

## Roles list sorting (built)
- Click a column header to sort: Title, Company, Location, Posted, Fetched, Fit, Score, Status, Applied.
- First click: A to Z for text, newest or highest first for dates and Score. A second click flips it. An arrow marks the active column.
- Sorting covers the whole filtered list, not only the rows on the page. Paging and filters carry the sort.
- Empty values always go last. Equal values keep newest-fetched first.
- Default is Fetched, newest first. The "Best fit first" dropdown is gone; click Score instead.
- Export CSV follows the same sort.

## Minimum salary filter (built)
- Config, Fit filters: "Minimum salary (INR per year)", default 10 lakh. 0 turns it off.
- The salary is read from the stored job description. Nothing is added to the scan.
- It reads amounts like "6-9 LPA", "8 to 12 lakh", "Rs 6,00,000 per year", "INR 50,000 per month".
- A bare amount with no unit or period is ignored, as are other currencies and hourly pay.
- The lower end of the range is compared with the minimum. Below it, the role shows "Excluded: salary".
- If several salaries appear, the highest lower end is used, so a stray small figure cannot exclude a role.
- A role with no salary found stays eligible.
- `jobscan salary-check` shows how many stored descriptions list a salary, how many fall below the minimum, and some examples.

## Description throttling (built)
- Job descriptions use the same two Config settings as the listing pages: "Retries per page or description" and "Abort after throttled responses in a row". The retry wait is shared too.
- A throttled description is retried after the retry wait, up to the retry limit. If it is still throttled, it is left for the next run and does not count as a failure.
- Any success resets the count. The description step stops once the abort limit is reached in a row. The scan itself is not affected.

## Descriptions pending (built)
- `jobscan status` and the Summary page show "Descriptions pending: N".
- N counts roles without a description from the last few days (Config "detail_max_age_days", 3 by default), the same ones `jobscan details` fetches. Roles it gave up on are not counted.

## Summary page now holds the run log (built)
- The Run Log tab is gone. The Summary page has four parts, top to bottom: roles by day, Scans, run details, Scoring.
- Scans is the old run table. It shows the last 10 runs, with a "Show more runs" link for 30 and "Show fewer runs" to go back.
- Click a run number to see its details. They sit in a collapsed "Details of run #N" section, like Advanced filters, and open when you choose a run. By default they are for the latest run.
- The details are the same as before: by keyword, match rate by page depth, matches by phrase, skipped titles.
- Old `/runs` links redirect to the Summary page.

## Skipped titles section (built)
- Skipped titles are no longer part of "Details of run #N". The run details keep the status line, note, by keyword, match rate by page depth and matches by phrase.
- A separate collapsed section "Skipped titles, last 7 days (N)" lists titles that matched no phrase, across all scans, each role once, newest first.
- It shows 10 rows. "Show more" opens up to 200, and "Show fewer" goes back.
- The number of days follows Config "skipped_titles_retention_days" (7 by default).

## Export of rated roles (built)
- The Eval tab has "Export rated roles (CSV)". It appears once at least one role is rated.
- One row per role you rated. Columns: title, company, location, matched phrase, your rating, comments, status, final score and band, keyword and meaning scores (0 to 100 and raw), fit filter result, level, employment type, job function, industries, description.
- Contact names, application dates, links and the CV are left out.
- A score is blank when it is out of date or the role has not been scored.
- It is meant for working out why the score gets some roles wrong.

## Fit refreshes after a status change (built)
- Changing a role's status from the Roles table or the side panel now updates its Fit cell, and the Fit line in the panel, straight away. Before, they showed the old result until the page was reloaded.

## Round 6: follow-ups and multi-select filters (built)

### Follow-up tracking
- Each role gets a "Follow-up needed" checkbox and a "Next follow-up" date. The date stays blank until you set it.
- Each role also keeps contact details: name (the existing Contact person field), email, phone and LinkedIn URL. All are optional.
- Notes about a follow-up go in the existing Comments field.
- In the side panel, the follow-up row sits under Status and Applied On. The contact block sits above Comments.
- Email and LinkedIn show as links once filled. The LinkedIn link must be a linkedin.com address.
- A role with the box ticked has one follow-up state:
  - Due: the date is today.
  - Upcoming: the date is after today.
  - Overdue: the date is before today. It shows in red.
  - No date set: the box is ticked and the date is blank.
- The Roles table shows the state as a tag in the Title cell, for example "Follow-up: Overdue (24 Oct)". There is no new column.
- If the box is not ticked, the date is kept but ignored.
- Export CSV includes the new fields.

### Multi-select filters
- Status, Fit, Score, Follow-up and Fetched date are tick-box dropdowns. You can tick one or more values.
- Nothing ticked means All. So the "Any ..." entries, "Scored only" and "All follow-ups" are removed.
- Fetched date lists the last 14 days, newest first, with a count for each. Nothing ticked means every date, including older ones.
- Follow-up filter values: Due, Upcoming, Overdue, No date set.
- When Due, Upcoming or Overdue is ticked, the list sorts by the earliest follow-up date.
- Paging, sorting, Export CSV and the Summary page links keep working with several values.

## Round 7: Naukri semi-automatic run (built; the live browser part is not yet tested against Naukri)

### How it gets the data
- Naukri has no public API. Its own search and job calls (`jobapi/v3/search`, `jobapi/v4/job/<id>`) refuse plain requests: a direct call returns "recaptcha required" (HTTP 406), and from the address bar a 400 asking for App Id and SystemId headers.
- So the run uses a real, visible Chrome window. It loads Naukri pages the way you would, and reads the JSON that the page itself receives from those calls. It does not call the API directly and it reads no page layout.
- It never solves or avoids a captcha. If one appears, the run pauses and waits while you solve it in the window, then carries on.
- The session is logged out. Every run opens a fresh private window (like an incognito window): no sign-in, no saved cookies, nothing kept afterwards. It is separate from your normal Chrome.
- The browser package is an optional install. LinkedIn-only use is unchanged.

### When it runs
- Manual only, with `jobscan naukri`. No daily schedule, because it needs the visible window and you at the Mac.
- Reminder: when the last Naukri run is more than 2 days old, a line shows at the top of the Roles page and in `jobscan status`, for example "Naukri last run: 3 days ago. Run jobscan naukri." Nothing shows otherwise. It is passive: no notification or email.
- First run covers the last 24 hours only.

### What it does
1. Searches Naukri for India with the existing title-phrase keywords, one results page at a time. It moves to the next page by opening that page's own address (the same as clicking Next), with a delay between pages.
2. Keeps roles posted in the run window and applies the existing title rules and Fit filters.
3. For roles that pass, opens the job's own page to read the full description. The search list gives only a short snippet for most roles when logged out.
4. Saves each role with Portal = Naukri, keyed by Naukri job ID, so a role is never saved twice.
5. Marks a role Closed when its job page says it is closed or expired.
6. Scoring then runs as it does after a LinkedIn scan.

### Data kept per role
- From the search list: job ID, title, company, link, location, experience range, salary (when shown), posted time, skills.
- From the job page: full description, exact posted time, industry, job role, functional area, employment type.
- Salary is shown on only about 15 to 20 percent of roles. Roles without it stay in, as for LinkedIn.
- Experience range (minimum and maximum years) is saved as numbers. This allows a maximum-years Fit rule later, which is not part of this round.
- Company rating and review count, vacancies, and apply and view counts are not stored in this round.

### Duplicates
- A Naukri role with the same company, title and city as an existing role from either portal is flagged "Possible duplicate". Nothing is hidden.

### Limits and failures
- Pages per run are capped (default 5). A job-page visit is also paced.
- The run stops after repeated captchas, or when the page data is not in the expected shape. It then fails with a clear message and saves nothing from the broken page.
- "tpm" also matches unrelated roles (for example Total Productive Maintenance). The existing title phrases and Fit rules filter these out, so many roles will be skipped.

### Changes by tab
- **Roles:** a Portal column, with the first three columns narrowed so there is no horizontal scroll. A Portal tick-box filter sits before the search box. Existing LinkedIn data is not affected, since roles are already keyed by portal and job ID.
- **Summary:**
  - The first two tables get a Portal selector (All, LinkedIn, Naukri) in place of extra columns, to avoid horizontal scroll.
  - Column headings wrap after a set length and show the full name on hover.
  - The latest run for each portal is shown side by side. Older runs stay under "show more".
  - The Skipped titles table gets a Portal column.
- **Scoring:** the Trigger column says what started the scoring, for example "After LinkedIn scan" or "After Naukri run". Naukri roles share the same daily embedding quota.
- **Config:** a Naukri section with pages per run, delay between pages, location (default India), the cap on job-page visits, the maximum age of roles to fetch, and the number of captchas before stopping. Keywords reuse the current title phrases. Fit filters and Scoring stay shared.
- **Eval:** stays combined, with a Portal filter.

### Built notes
- Start with `pip install playwright` (the `jobscan[naukri]` extra only works after `pip install -e .` is re-run). It drives the Chrome already on the Mac, so there is no separate browser download.
- Run with `jobscan naukri`. `jobscan status` shows the latest Naukri run and the reminder.
- Experience and salary from Naukri are written as two plain lines at the top of the saved description, so the existing Fit rules (minimum years, minimum salary) read them.
- A list description of 1,500 characters or more is taken as the full text and saves a job-page visit. Shorter ones are treated as snippets.
- The page-data check: if a search reply does not have the expected shape, the run stops as "Naukri data layout changed" and saves nothing from it.
- Closed detection only trusts clear signals (page not found, or an "expired" notice in the data). Naukri's real closed-job reply has not been seen yet, so it may need adjusting.
- Scoring after a Naukri run is labelled "After Naukri run". Older scoring runs keep "After a scan".
- Descriptions pending, the Roles header's last successful run, the minimum gap between runs and the daily schedule still refer to LinkedIn only.

### Still to check on the real site
- That a fresh private window gets Naukri's search data without a captcha on every page.
- That the next-page addresses (`<keyword>-jobs-in-<place>-2`) work as built.
- That the in-page fallback request, which sends the same app id header the site uses, is accepted if the page does not load the data itself.
- Compare the same search logged in and logged out (first 20 job IDs, description length) to confirm logged out loses nothing that matters.

## Round 8: Config sections and Naukri paging

### Config page (built)
- Three sections: **Common**, **LinkedIn**, **Naukri**. Every setting is in exactly one. A setting used by both portals is in Common, so no extra label is needed.
- **Common:** title phrases, title match mode, stop after empty pages, descriptions window (days), Fit filters, Scoring, Embeddings, keep skipped titles (days).
- **LinkedIn:** location, geo ID, posted within, page cap, pacing, throttling, descriptions per scan, minimum gap between runs, scheduled scan.
- **Naukri:** the Naukri settings from Round 7.

### Naukri paging (built; needs a live check)
- Page 1 is opened in the window. Pages 2 and on are asked for through the same search call the page made, with only the page number changed. The next-page address did not give different results.
- If a page returns only roles already seen on the page before, that keyword stops, the run is marked Partial, and a line says so.

### Sort by date (built; needs a live check)
- Searches use Naukri's own `sort=f` (Freshness, newest first) and `jobAge=<days>`, taken from the address and request URLs you sent.
- `jobAge` counts whole days, so the "Only roles posted within (hours)" setting is rounded up to days for Naukri (24 hours is 1 day, 25 to 48 is 2). The exact hour limit is still applied to each role.
- No new setting.

### `jobscan naukri-details` (built)
- Opens the same private browser window as `jobscan naukri` but does no searching. It opens job pages only for Naukri roles that lack a description, were fetched within the descriptions window, and pass the Fit filters, up to "Job pages to open per run".
- `jobscan naukri` still reads job pages at the end of its run. The new command catches up on what was left over.
- It does not create a run entry, so it does not reset the "Naukri last run" reminder. Progress is printed and kept in the run log file. New descriptions are scored afterwards.
- With nothing missing it says so and opens no window. No new settings.

### Portal pickers on Eval and Config (built)
- **Eval:** the Portal tick-box filter is replaced by the same pills as Summary (All, LinkedIn, Naukri), one at a time. The choice is remembered in a cookie until you pick another. The CSV export follows the choice.
- **Config:** pills All, Common, LinkedIn only, Naukri only show one section at a time. The choice is remembered in the browser. Hidden sections stay in the form, so a save still sends every setting.
## Round 9: Tailored resume (built)

### Goal
For a chosen role, propose edits to your Word CV that match the role's description, let you approve them, then produce a Word file that looks exactly like your CV. Cover letter is deferred (a template will be shared later).

### Flow
1. **Role side panel:** one button, "Tailor resume". It only navigates. No status is kept in the panel.
   - No Word CV uploaded: opens Manage CV, Upload CV, with a message. Nothing is sent to the AI.
   - Word CV exists and the role has no earlier request: starts preparing changes, opens Manage CV, History.
   - The role already has an active request (preparing or waiting for review): opens History at that row. Nothing new starts.
   - The role has only finished requests (Ready, Failed, Discarded): starts a new request and a new row, keeping the old ones.
   - The button is disabled, with a one-line hint, only when the role has no description yet.
2. **History** shows the request move through: Preparing changes, Needs your review, Ready. It can also end as Failed (with the reason) or Discarded.
3. **Review changes:** each proposed change has a tick box, ticked by default, with before and after and a length check. A "Gaps" list shows what the role asks for that your CV lacks (never added).
4. **Generate resume with selected changes:** the Word file is made locally from the ticked changes. No AI is used in this step.
5. **Download** the .docx from History.

### Manage CV (renamed from CV)
- Two tabs: **Upload CV** and **History**.
- A red count on the "Manage CV" menu item shows how many requests need your review.

### Upload CV
- The original uploaded file is now kept on your machine, not only its text.
- Word (.docx) is the format for tailoring. PDF and text files can still be uploaded for scoring, but tailoring is off for them, with a message saying so.
- Uploading a new CV replaces the current one. A request still waiting for review that was based on the old CV is marked "CV changed, start again". Finished files are kept.

### History
- One row per request. A role can have several rows over time, each with its own files. Only one request per role can be active (preparing or waiting for review) at a time.
- Columns: requested time (IST), company, role (with portal and a link to the role), status, action.
- Statuses and actions:
  - Preparing changes: nothing to do. The page refreshes by itself.
  - Needs your review: Review changes.
  - Ready: Download .docx, and View changes.
  - Failed: the reason, and Try again (a new row).
  - Discarded: Start again (a new row).
- The role's title, company and description are saved with the row, since roles close.

### What may be changed
- **Professional summary:** wording only.
- **Core competencies:** the same items in a new order. No item added or removed.
- **Experience bullets:** the wording after the bold label, and the order of bullets within one job.
- **Never changed:** name and contact line, employer lines, job titles, dates, bold labels, every number and percentage, education, certifications, fonts, bullets, spacing.
- **New skills or experience are never added.** Anything the role wants that the CV lacks goes to the Gaps list only.
- **Check:** every number in a proposed text must already appear in the original paragraph. A proposal that fails is dropped.

### Format and length (very important)
- The Word file is edited in place: only text inside existing paragraphs changes, and the existing formatting of each paragraph is kept. Bullets are moved, not rebuilt.
- **Length rule:** a changed paragraph may never have more characters than the original. Anything longer is rejected before you see it. Reordered competencies keep the same length.
- **Page check:** if LibreOffice is installed on the computer, the original and the new file are both rendered and their page counts compared. A longer result is blocked.
- **Honest limit:** equal character count does not guarantee equal line breaks in every font. Open the file in Word once before sending.
- **Bold inside a sentence** (other than the label) is not carried over and can be fixed by hand.
- **Tested** on your real Word CV: bullet rewording, summary rewording, bullet reordering and competency reordering all kept the layout identical.

### AI service and privacy
- Any OpenAI-compatible service. First choice: Cerebras, model gpt-oss-120b. Free limits seen: 5 requests a minute and 1M tokens a day. These may be trial credits that expire, so the address and model are settings.
- The key is read from a shell variable (default name CEREBRAS_API_KEY). It is never stored by the app.
- Sent: the role's title, company and description, and your experience text. Not sent: name, phone, email, links.
- One AI call per request. Reviewing, ticking and generating cost nothing more. Discarding costs nothing more.
- Runs in the background, so the web app stays usable while it works.

### Failures (each shown in History with a plain reason)
- The AI service was busy (rate limit), or unreachable, or slow.
- The key is missing or refused.
- The reply could not be read, or every proposed change failed the checks.
- The role has no description, or the CV is not a Word file, or the CV file is missing.
- No change is made to your CV in any failure.

### Config
- New group "Tailoring" in Common: AI service address, model, key variable name. No other settings.

### Deferred
- Cover letter (template to come), chat modifications, PDF output (only the Word file is kept), tone options.

### Open points
- Whether Cerebras's free tier stays free. Switching provider is a setting change.
- Page-length check needs LibreOffice. Without it only the character rule applies.

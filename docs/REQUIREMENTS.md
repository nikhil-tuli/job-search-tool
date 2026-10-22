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

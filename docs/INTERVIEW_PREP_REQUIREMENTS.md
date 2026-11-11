# Interview Prep: requirements (v3, approved for build)

Status: approved on 11 Nov 2026. Build started.
Date: 11 Nov 2026.

Changes from v2
- Question and feedback model is now **Mistral** (the same one used for fit scoring).
- Added: user stories, functional requirements, non-functional requirements, data model, routes, AI contracts, edge cases, out of scope, test plan, traceability.
- Added: app-side checks on model output (quote check, number check).
- Added: status **In progress** (shown only while an interview is running).

---

## 1. Overview

**Goal:** let the user practise a spoken interview inside jobscan and get specific, usable feedback.

**Terms (used the same way everywhere)**

| Term | Meaning |
|---|---|
| Interview | One session of up to 5 questions |
| Answer | The spoken reply to one question |
| Interview type | **Company role**, **General role**, or **My questions** |
| Rubric | A group of ratings. Two in v1: **How you spoke**, **Answer feedback** |
| Sub-aspect | One rated item in a rubric (for example Pace) |
| Rating | The value of a sub-aspect: Good, Needs work, Weak (spoken) or 0 to 100 (answer) |
| Rubric score | The 0 to 100 score of one rubric for one interview |
| Band | Word for a score: Strong, Good, Needs work, Weak |
| Role, must-haves, role focus | As in fit scoring |

**Shape of the feature**
- New tab **Interview Prep** with sub-tabs **Online Interview** and **Past History**.
- Voice only. No camera, no video.
- Speech to text: Groq `whisper-large-v3`.
- Questions and feedback: Mistral `ministral-14b` (the fit scoring model).
- Everything else runs locally.

---

## 2. Scope

**In v1**
- Three ways to start an interview.
- Question writing from CV and role.
- Spoken answers with a time guide.
- Two feedback rubrics, with benchmarks, tips and examples.
- Past History with status, details and feedback.
- Config section "Interview Prep".

**Out of scope:** see section 12.

---

## 3. User stories

One persona: **the candidate**, a job seeker who tracks roles in jobscan and wants to practise before real interviews.

Format: *As a candidate, I want ... so that ...*, then acceptance criteria (AC).

| ID | Story | Acceptance criteria |
|---|---|---|
| US-1 | I want to practise for a role I track, so that questions match that job. | Given a role in the Roles tab, when I click "Practice interview", then Interview Prep opens with that role selected and type Company role. |
| US-2 | I want a general interview for a job title, so that I can practise without a specific role. | Given the allowed list, when I pick a title, then the interview has type General role and its questions fit the title. |
| US-3 | I want to ask my own questions, so that I can rehearse specific ones. | Given 1 to 5 typed questions, when I start, then exactly those questions are asked in order. |
| US-4 | I want questions based on my CV, so that they are about my background. | Given a CV is linked, then the introduction and behavioural questions refer to it. Given no CV, then Start is disabled and I am asked to upload one. |
| US-5 | I want to check my microphone first, so that I do not lose an answer. | Given I click Start, then I see a live voice level and I can continue only after permission is granted. |
| US-6 | I want to hear and read each question. | Given "Read questions aloud" is on, then the question is spoken and shown. Given it is off, then it is only shown. |
| US-7 | I want a time guide per answer, so that I stay near 60 to 90 seconds. | Given I am answering, then I see a timer. At 2:00 it turns amber with "Time to wrap up". At 3:00 it says "Over 3 minutes". |
| US-8 | I want to skip a question or stop early. | Given an interview, when I click Skip, then the question is unanswered. When I click End interview and confirm, then I get feedback on answers so far. |
| US-9 | I want my work kept if my browser closes. | Given I close the browser mid-interview, then the interview shows as Incomplete and I can get feedback on answers already made. |
| US-10 | I want to see how I spoke, with benchmarks. | Given feedback is ready, then I see seven measured sub-aspects, each with value, rating, benchmark and tip. |
| US-11 | I want feedback on each answer's content, so that I know what to fix. | Given feedback is ready, then each answer shows a score, five sub-aspect ratings, what worked, what to work on, and an improved answer. |
| US-12 | I want to understand how scores are made. | Given the feedback page, then each rubric shows its label (Measured or AI estimate) and a "?" tip that explains it. |
| US-13 | I want to see progress since my last interview. | Given an earlier interview of the same kind, then each rubric score shows "up n" or "down n" or "about the same". |
| US-14 | I want a list of past interviews. | Given Past History, then I see date, type, details, answered count, scores, status and feedback button, newest first, with a type filter. |
| US-15 | I want to open the role from history without changing it. | Given a Company role row, when I click "open role", then the read-only role panel opens. |
| US-16 | I want to recover from problems. | Given status Incomplete, then I can get feedback. Given Failed, then I can try again. Given Preparing, then the button is disabled. |
| US-17 | I want to delete an interview. | Given a row, when I confirm delete, then its transcripts, feedback and thumbs are removed. |
| US-18 | I want to know what leaves my computer. | Given the start screen, then a one-line privacy note is shown. No audio is stored by the app. |
| US-19 | I want to manage roles and settings. | Given Config, then I can edit the allowed roles, models, key variables, time limits and the read-aloud default. |
| US-20 | I want to rate the feedback. | Given an answer's feedback, then I can mark it helpful or not. The mark is saved. |
| US-21 | I want to practise again quickly. | Given a feedback page, when I click "Practice again", then a new interview of the same kind starts with new questions. |
| US-22 | I want clear messages when limits are hit. | Given a provider limit, then I see what happened and when to try again. |

---

## 4. Functional requirements

Each requirement has an ID and the stories it serves.

### 4.A Navigation and entry

| ID | Requirement | Stories |
|---|---|---|
| FR-A1 | Add menu item **Interview Prep** beside "Modify CV & Cover Letter". It opens Online Interview. | US-1, US-2, US-3 |
| FR-A2 | Sub-tabs **Online Interview** and **Past History**, styled like the Modify CV sub-tabs. | US-14 |
| FR-A3 | Add a **Practice interview** button on every row of the Roles list and in the role panel (editable version only). It opens Online Interview with that role selected and type Company role. | US-1 |
| FR-A4 | Only one interview can be In progress. Starting another shows "Finish or end your current interview first" with a link to it. | US-9 |

### 4.B Start screen

| ID | Requirement | Stories |
|---|---|---|
| FR-B1 | Show "Using CV: <file name>, uploaded <date>". If no CV is linked, show "Upload your CV to start", link to the Upload CV tab, and disable Start. | US-4 |
| FR-B2 | Card 1, **A role you track**: search by company or title over all roles in the Roles tab. List shows company and title. Selected role is highlighted. | US-1 |
| FR-B3 | Card 2, **A general role**: a drop-down of the allowed roles from Config. If the list is empty, the card is disabled with "Add roles in Config". | US-2, US-19 |
| FR-B4 | Card 3, **My own questions**: 1 to 5 text boxes. Each question is 5 to 300 characters. Blank rows are ignored. Exact duplicates are removed. "Add question" is disabled at 5. | US-3 |
| FR-B5 | Only one card is active. Choosing one clears the others. Start is enabled only when the active card is valid. | US-1, US-2, US-3 |
| FR-B6 | Switch **Read questions aloud**. Default comes from Config. | US-6 |
| FR-B7 | Show: "Your voice is sent for transcription and is not saved." | US-18 |
| FR-B8 | **Start interview** creates the interview, writes questions (not for My questions), then opens the microphone check. Shows "Writing your questions" while waiting. | US-1, US-2, US-3 |

### 4.C Question generation

| ID | Requirement | Stories |
|---|---|---|
| FR-C1 | Company role and General role interviews have 5 questions in these slots. | US-1, US-2, US-4 |

| Slot | Type | Source |
|---|---|---|
| 1 | Introduction | CV and role |
| 2 | Behavioural | A must-have marked Met, or a role need |
| 3 | Behavioural | Another role need |
| 4 | Situational or role-specific | Role description, or typical work of the title |
| 5 | Gap | A must-have marked Partly or Missing. General role: a common gap for the title |

| ID | Requirement | Stories |
|---|---|---|
| FR-C2 | Input to the model: CV text, role title, company, role description, must-haves with status (Company role only), allowed question types, the last 5 question lists for the same kind. | US-1, US-2, US-4 |
| FR-C3 | Before sending, remove phone numbers, email addresses and links from the CV text. | US-18 |
| FR-C4 | The model may use only facts in the CV. It must not invent employers, titles or numbers. | US-4 |
| FR-C5 | Each question is one sentence of at most 30 words. One question per slot. | US-4 |
| FR-C6 | Reply must be valid JSON (section 8.1). If invalid, retry once. If still invalid, show "Could not write questions. Try again." and keep the interview unsaved. | US-22 |
| FR-C7 | Questions already asked in the last 5 interviews of the same kind are avoided. | US-21 |
| FR-C8 | Role with no description: use title and company only. Show a note on the start of the interview. | US-1 |
| FR-C9 | Role with no fit verdict: slots 2, 3 and 5 use role needs and a common gap for the title. | US-1 |
| FR-C10 | My questions: no model call. Questions are asked exactly as typed. | US-3 |
| FR-C11 | The user cannot edit generated questions in v1. | – |

### 4.D Microphone check and interview screen

| ID | Requirement | Stories |
|---|---|---|
| FR-D1 | Ask for microphone permission. Show a live voice level bar and "Say a few words". Show "Voice detected" when level stays above a threshold for 0.5 s. **Start** is enabled after permission is granted. | US-5 |
| FR-D2 | If permission is refused or no microphone exists, show how to allow it. The interview cannot start. | US-5 |
| FR-D3 | Interview screen shows "Question n of N", the question text, a voice level bar, a timer, and buttons **Next question**, **Skip**, **End interview**. | US-6, US-7, US-8 |
| FR-D4 | If reading aloud is on, speak the question with the browser's speech synthesis. Prefer an English (India or UK or US) voice. Show a **Skip reading** button. If synthesis is unavailable, show text only and a short note. | US-6 |
| FR-D5 | Recording starts when the question has been read, or when Skip reading is clicked, or at once if reading is off. | US-6, US-7 |
| FR-D6 | One recording per answer, as one file, in the format the browser supports. | US-7 |
| FR-D7 | Time guide: timer amber at 2:00 with "Time to wrap up". At 3:00 shows "Over 3 minutes". Hard stop at 5:00: the answer ends and the next question starts. All three values come from Config. | US-7 |
| FR-D8 | **Next question** is enabled 3 seconds after recording starts. **Skip** marks the answer skipped and discards its audio. | US-8 |
| FR-D9 | **End interview** asks "End now? You will get feedback on n answered questions." If no answer is long enough to score, it says "Nothing to score. The interview will not be saved." | US-8 |
| FR-D10 | While an interview is running, warn on leaving the page. If the page unloads, mark the interview Incomplete (using a beacon). | US-9 |
| FR-D11 | Audio is kept only in browser memory until its upload succeeds. | US-18 |
| FR-D12 | There is no pause button in v1. | – |

### 4.E Transcription

| ID | Requirement | Stories |
|---|---|---|
| FR-E1 | Each answer is uploaded as soon as it ends. The user moves to the next question without waiting. | US-7 |
| FR-E2 | The server sends the audio to Groq `whisper-large-v3` with language `en`, temperature 0 and word times. A prompt hint to keep fillers is used if the test in section 11 shows it works. | US-10 |
| FR-E3 | The server never writes audio to disk. If a temporary file is unavoidable, it is deleted in a `finally` block. Audio is never logged. | US-18 |
| FR-E4 | Reject files over 25 MB in the browser. Reject non-audio content types on the server. | US-22 |
| FR-E5 | On timeout or server error, the browser retries up to 2 times. On a rate limit (429), wait for the time Groq gives, then retry once. After that the answer is **not scored**. | US-22 |
| FR-E6 | Store transcript, duration in seconds and word count. Do not store the word list or the audio. | US-18 |
| FR-E7 | Fewer than 10 words: the answer is **too short to score**. No speech detected: the answer is **no speech**. Both are left out of averages. | US-10 |

### 4.F How you spoke (measured)

| ID | Requirement | Stories |
|---|---|---|
| FR-F1 | Compute the seven sub-aspects from transcripts and word times with fixed rules (section 5). No model is used. | US-10 |
| FR-F2 | Each sub-aspect has a value, a rating (Good, Needs work, Weak), points (100, 60, 25) and a tip. | US-10 |
| FR-F3 | Rubric score is the average of points, rounded to a whole number. | US-12 |
| FR-F4 | A sub-aspect that cannot be measured is left out. Fewer than 50 spoken words in total: show "Not enough speech to measure". | US-10 |
| FR-F5 | All limits and word lists live in one code module. Raw values are stored so ratings can be recomputed if limits change. | US-12 |
| FR-F6 | If word times are unavailable, use sentence times. Pace stays valid. Long pauses and thinking time are shown as approximate. | US-22 |

### 4.G Answer feedback (model)

| ID | Requirement | Stories |
|---|---|---|
| FR-G1 | After the last answer (or End), send one request to Mistral with all scorable transcripts. | US-11 |
| FR-G2 | Input: CV text (contact details removed), role details, must-haves, the questions, the transcripts, the rating scale (section 6). Nothing from earlier interviews. | US-11 |
| FR-G3 | Reply must be valid JSON (section 8.2). Reject and retry once if not. Then status Failed. | US-16 |
| FR-G4 | Settings: temperature 0.2, reply cap 6,000 tokens (Config). | US-12 |
| FR-G5 | **Quote check:** each sub-aspect needs a quote of at most 15 words. The app checks the quote appears in the transcript (ignoring case and punctuation). If missing or not found, that rating is capped at 60. | US-12 |
| FR-G6 | **Number check:** in "Your answer, improved", any number not in the CV, the transcript or a [bracket] is replaced with "[add figure]". | US-11 |
| FR-G7 | The app computes answer scores (mean of five) and the rubric score (mean of answer scores). Skipped, too-short, no-speech and not-scored answers are left out. | US-12 |
| FR-G8 | "Top 3 tips" = tip of the lowest-rated spoken sub-aspect (if not Good) plus the model's top tips, up to 3. | US-11 |
| FR-G9 | Store the prompt version and model name with the feedback. | US-12 |
| FR-G10 | Interview calls use the same pacing gate as fit scoring (the account limit is shared). If a scan or scoring run is busy, show "Waiting for AI quota" instead of failing. | US-22 |

### 4.H Feedback page

| ID | Requirement | Stories |
|---|---|---|
| FR-H1 | Header: type, role or title, date, "n of N answered", CV file name used. | US-10 |
| FR-H2 | Two rubric cards: Answer feedback and How you spoke. Each shows rubric score, band, change since last interview, and top finding. | US-10, US-11, US-13 |
| FR-H3 | Labels: How you spoke = **Measured**. Answer feedback = **AI estimate**. A "?" tip explains each (section 5 and 6). | US-12 |
| FR-H4 | Change since last interview: difference in rubric score against the latest earlier **Ready** interview of the same kind (same role for Company role, same title for General role). Under 5 points: "about the same". My questions and first interviews: nothing shown. | US-13 |
| FR-H5 | One block per question: text, type, length, score, band, verdict, five bars with reasons, What worked, Work on this next, Your answer improved (collapsed). Skipped and unscorable answers show their reason. | US-11 |
| FR-H6 | One card per spoken sub-aspect: value, status dot, benchmark, tip. | US-10 |
| FR-H7 | "See examples" under each rubric: fixed cards (section 7.3). | US-12 |
| FR-H8 | Thumbs up and down on each answer block. Saved, changeable. | US-20 |
| FR-H9 | Buttons: **Practice again** (same kind, new questions) and **Back to history**. | US-21 |
| FR-H10 | Colour is never the only signal. Every dot and band also has text. | US-10 |

### 4.I Past History

| ID | Requirement | Stories |
|---|---|---|
| FR-I1 | Columns: Date, Interview type, Details, Answered, Scores (Spoke · Answer), Status, Feedback, Delete. Newest first. 25 per page. | US-14 |
| FR-I2 | **Details**: Company role shows link "open role" and the company. General role shows the role chosen. My questions shows "n custom questions", with the questions on hover. | US-14, US-15 |
| FR-I3 | "open role" opens the read-only role panel (the same panel as Modification History). If the role was deleted, show "role removed". | US-15 |
| FR-I4 | Filter by interview type. | US-14 |
| FR-I5 | Feedback button by status (section 4.J). | US-16 |
| FR-I6 | Delete icon with a confirmation. Permanent. Removes questions, transcripts, feedback and thumbs. | US-17 |
| FR-I7 | Empty state: "No interviews yet" and **Start an interview**. | US-14 |

### 4.J Status and lifecycle

| Status | Meaning | Feedback button |
|---|---|---|
| **In progress** | The interview is running. | Link "Go to interview" |
| **Preparing** | Answers are being transcribed and scored. | Disabled: "Preparing…" |
| **Ready** | Feedback is complete for every scorable answer. Includes interviews ended early. | **View feedback** |
| **Incomplete** | The browser closed or the page was left before the end. Transcripts exist. No feedback yet. | **Get feedback** |
| **Failed** | Transcription or the model failed after one retry. | **Try again** |

| ID | Requirement | Stories |
|---|---|---|
| FR-J1 | Allowed moves: In progress → Preparing → Ready or Failed. In progress → Incomplete. Incomplete → Preparing. Failed → Preparing. | US-16 |
| FR-J2 | An In progress interview with no activity for 30 minutes becomes Incomplete when history is next opened. | US-9 |
| FR-J3 | Get feedback and Try again start from the saved transcripts. They never re-transcribe. An answer whose transcription failed stays "not scored". | US-16 |
| FR-J4 | The Preparing page checks status every 3 seconds. It shows steps: transcribing, scoring, writing. | US-16 |
| FR-J5 | An interview with no scorable answer is not saved. | US-8 |

### 4.K Config

| ID | Requirement | Stories |
|---|---|---|
| FR-K1 | New Config section **Interview Prep**. | US-19 |
| FR-K2 | Fields below. Keys come from terminal variables or the Keychain. They are never stored in the file. | US-19 |

| Setting | Default |
|---|---|
| Allowed roles (one per line) | See below |
| Transcription: base URL, model, key variable | Groq, `whisper-large-v3`, `GROQ_API_KEY` |
| Question and feedback model: base URL, model, key variable | Same as fit scoring (Mistral `ministral-14b`) |
| Reply cap for feedback | 6,000 tokens |
| Time guide: amber at / "over" at / hard stop at | 120 s / 180 s / 300 s |
| Read questions aloud by default | On |

Default allowed roles: Backend Engineer, Frontend Engineer, Full-stack Engineer, Mobile Engineer, Data Engineer, Data Scientist, ML / AI Engineer, DevOps / SRE Engineer, QA Engineer, Security Engineer, Engineering Manager, Technical Program Manager, Program Manager, Project Manager, Product Manager, Business Analyst, Designer.

| ID | Requirement | Stories |
|---|---|---|
| FR-K3 | Allowed roles: up to 50 entries, 60 characters each. Blank lines ignored. Duplicates (any case) removed. | US-19 |
| FR-K4 | Time guide: amber < over < hard stop, each between 30 and 600 seconds. Invalid values are refused with a message. | US-19 |
| FR-K5 | Changes apply to the next interview. A running interview keeps its values. | US-19 |

### 4.L Data, privacy and deletion

| ID | Requirement | Stories |
|---|---|---|
| FR-L1 | No audio, video or image is stored. | US-18 |
| FR-L2 | Interview content (questions, transcripts, feedback) is never sent to Langfuse. | US-18 |
| FR-L3 | Logs and the AI call log keep counts and timings only. No transcript, CV or answer text. | US-18 |
| FR-L4 | Deleting an interview removes all its rows (section 8.3). | US-17 |
| FR-L5 | The start screen privacy note is always visible (FR-B7). | US-18 |

---

## 5. Rubric 1: How you spoke (measured)

**Input:** transcript of each answer, with the time of each word. **No model is used.**

How each interview-level value is built
- **Pace:** all words divided by all speaking time. Pauses over 2 s are not speaking time.
- **Thinking time:** average over answers of the seconds from the end of the question to the first word. If reading aloud is off, the estimated reading time (question words ÷ 3.3 per second) is subtracted, not below 0.
- **Filler words, hedging phrases:** total count per 100 words.
- **Long pauses:** total silences over 3 s inside answers.
- **Answer length:** average seconds per answer.
- **Ownership:** share of "I" among "I" plus "we". "I" counts I, I'm, I've, I'll, I'd. "We" counts we, we're, we've, we'll, we'd.

| Sub-aspect | Good (100) | Needs work (60) | Weak (25) | Tip when not Good |
|---|---|---|---|---|
| Pace (words per minute) | 120 to 160 | 100 to 119, or 161 to 180 | Under 100 or over 180 | "Aim for about 140 words a minute." |
| Thinking time | Up to 3 s | 3 to 6 s | Over 6 s | "Take a breath. Then open with your headline." |
| Filler words per 100 | Under 3 | 3 to 5 | Over 5 | "Pause silently instead of saying um or like." |
| Hedging phrases per 100 | Under 2 | 2 to 4 | Over 4 | "Say 'I did', not 'I think I did'." |
| Long pauses | 0 | 1 to 2 | 3 or more | "Say 'let me think' and carry on." |
| Answer length | 60 to 120 s | 30 to 59 s, or 121 to 180 s | Under 30 s, or over 180 s | "Keep to 60 to 90 seconds." |
| Ownership | 50% or more | 30 to 49% | Under 30% | "Say 'I' for what you did yourself." |

Sources of the limits
- Pace: tools quote [120 to 140](https://www.rezi.ai/rezi-docs/ai-interview-practice-pace). Your reference screen shows 130 to 160. We use 120 to 160.
- Filler, hedging and pause limits come from your reference screens.
- Answer length: [60 to 90 seconds, 2 minutes at most](https://residencyadvisor.com/resources/behavioral-interview-questions/how-long-should-a-strong-behavioral-interview-answer-actually-be).

Word lists (starting set, in code)
- Fillers: um, uh, er, you know, basically, actually, kind of, sort of, I mean, like.
- "like" counts only when it is not part of "would like", "I like", "looks like", "feels like", "like to".
- Hedges: I think, maybe, I guess, probably, I feel like, might be, sort of, kind of.
- Matching ignores case and punctuation.

Rubric score and band
- Rubric score = average of the sub-aspect points, rounded.
- Bands: Strong 80 to 100, Good 65 to 79, Needs work 50 to 64, Weak under 50.

Worked example

| Sub-aspect | Value | Rating | Points |
|---|---|---|---|
| Pace | 175 wpm | Needs work | 60 |
| Thinking time | 2.5 s | Good | 100 |
| Filler words | 4.2 per 100 | Needs work | 60 |
| Hedging | 0.4 per 100 | Good | 100 |
| Long pauses | 1 | Needs work | 60 |
| Answer length | 85 s | Good | 100 |
| Ownership | 40% | Needs work | 60 |

Average = 540 / 7 = 77. Band: **Good**.

Label shown: **Measured**. It means the app counted it from the transcript. It can be wrong only if the transcript is wrong.

---

## 6. Rubric 2: Answer feedback (AI estimate)

**Input:** transcripts, CV, role details, must-haves. **Model:** Mistral.

Per answer the model returns: five ratings (0 to 100) with a reason and a quote, a one-line verdict, What worked (2 to 3), Work on next (1 to 3, each with title, why, fix), and Your answer improved (130 to 200 words).

| Sub-aspect | Checks | 80 to 100 | 60 to 79 | Under 60 |
|---|---|---|---|---|
| Relevance | Answers what was asked | Direct, nothing off-topic | Partly answers, or drifts | Misses the question |
| Structure | Behavioural: situation, action, result. Situational: steps. Introduction: present, past, future. | Clear order, headline early | Hard to follow | No order |
| Specificity | Names, numbers, details | Several concrete facts | A few | Generic |
| Impact & ownership | Own decisions and a real result | Own action and measured result | Result or ownership unclear | Neither stated |
| Role fit | Links to role needs and must-haves | Clear link | Weak link | No link |

How scores are computed (by the app)
- Answer score = average of the five ratings.
- Rubric score = average of answer scores, rounded.
- Skipped, too-short, no-speech and not-scored answers are left out.

Worked example

| Answer | Relevance | Structure | Specificity | Impact | Role fit | Answer score |
|---|---|---|---|---|---|---|
| Q1 | 85 | 70 | 60 | 55 | 80 | 70 |
| Q2 | 90 | 80 | 75 | 80 | 85 | 82 |
| Q3 | skipped | | | | | left out |
| Q4 | 70 | 60 | 55 | 60 | 75 | 64 |
| Q5 | 60 | 50 | 45 | 50 | 70 | 55 |

Rubric score = (70 + 82 + 64 + 55) / 4 = 68. Band: **Good**.

**Why it is a judgement, and the controls**
- No answer has one true score. The label says **AI estimate**.
- Controls: a fixed scale with anchors; the quote check (FR-G5); low randomness; a strict output format; the model sees only this interview; thumbs on each answer to review quality later.
- Scores compare you with the written scale, not with other candidates.

**Evidence rule, in plain words:** every rating must point to words you actually said. If it cannot, the app lowers it to 60 at most.

Improved answer rules: uses only facts from the CV or the answer; gaps become [placeholders]; the number check (FR-G6) enforces this.

---

## 7. Shared rules

### 7.1 Interview rules
- 5 questions (My questions: 1 to 5).
- The user may finish or leave at any time.
- Feedback needs at least one answer of 10 words or more.
- Time guide: amber at 2:00, "Over 3 minutes" at 3:00, hard stop at 5:00.

### 7.2 Change since last interview
- Same kind: Company role = same role. General role = same title. My questions = never compared.
- Show "▲ n" or "▼ n" in points out of 100. Under 5 points: "about the same".
- Example: last time 65, now 68 → **▲ 3**.

### 7.3 Examples (fixed text, written once)
Each card has Weak, Strong and Why.
- Pace: rushed at 190 wpm vs 140 with pauses at key points.
- Thinking time: "Umm, so, let me, yeah" for 8 s vs a 2 s pause and a headline.
- Filler words: "So basically I like managed the, you know, team" vs "I managed a team of 12."
- Hedging: "I think I maybe led it" vs "I led it."
- Ownership: "We delivered the launch" for every step vs "I wrote the brief. We shipped in May."
- Structure: result at the end of a long story vs "I cut checkout failures by 30%. Here is how."
- Specificity: "I improved processes" vs "I moved 14 lender integrations onto one API."

---

## 8. Technical design

### 8.1 Question writing contract
Request: CV text, role fields, must-haves, slot list, recent questions.
Reply:
```json
{"questions":[{"slot":1,"kind":"introduction","text":"...","source":"cv"}]}
```
Validation: exactly the requested number of items; slots 1 to 5 in order; text non-empty and at most 30 words.

### 8.2 Feedback contract
Reply:
```json
{"answers":[{"q":2,
  "ratings":{"relevance":{"score":90,"reason":"...","quote":"..."},
             "structure":{...},"specificity":{...},"impact":{...},"role_fit":{...}},
  "verdict":"...","worked":["..."],
  "next":[{"title":"...","why":"...","fix":"..."}],
  "improved":"..."}],
 "top_tips":["...","..."]}
```
Validation: scores are integers 0 to 100; every transcript has one item; lists within size limits; quote at most 15 words.

### 8.3 Data model (new tables)

| Table | Key fields |
|---|---|
| `interview` | id, type, role_id (null), general_title (null), status, started_at, ended_at, cv_name, cv_uploaded_at, read_aloud, question_model, feedback_model, prompt_version, error |
| `interview_question` | id, interview_id, idx, slot, kind, text, source |
| `interview_answer` | id, interview_id, idx, status (answered, skipped, too_short, no_speech, not_scored), transcript, duration_s, word_count, raw_metrics (JSON) |
| `interview_feedback` | interview_id, spoken (JSON: raw values, ratings), answer_feedback (JSON), spoken_score, answer_score, tips (JSON), created_at |
| `interview_thumb` | interview_id, idx, value |

- No word list, no audio, no images.
- Deleting an interview deletes its rows in all five tables.
- Added with the app's existing schema mechanism. Existing tables are unchanged.

### 8.4 Routes (outline)

| Route | Purpose |
|---|---|
| `GET /interview` | Online Interview start screen (`?role=<id>` preselects a role) |
| `POST /interview` | Create interview and write questions |
| `GET /interview/<id>/run` | Microphone check and interview screen |
| `POST /interview/<id>/answer/<n>` | Upload one answer's audio, transcribe, return status |
| `POST /interview/<id>/finish` | End or complete, start feedback |
| `GET /interview/<id>` | Feedback page, or the Preparing, Incomplete or Failed page |
| `GET /interview/<id>/status` | JSON state for polling (writing questions, preparing) |
| `POST /interview/<id>/skip/<n>`, `/not-scored/<n>` | Mark an answer skipped or not scored |
| `POST /interview/<id>/again` | Practice again (same kind, new questions) |
| `GET /interview/roles?q=` | Role search for the start screen |
| `POST /interview/<id>/retry` | Get feedback or Try again |
| `POST /interview/<id>/delete` | Delete |
| `POST /interview/<id>/thumb` | Save a thumb |
| `GET /interview/history` | Past History (`?type=`) |
| `POST /interview/<id>/abandon` | Beacon from page unload, sets Incomplete |

### 8.5 Provider facts

**Groq `whisper-large-v3`**
- Word error rate 10.3% (turbo is 12%).
- Free plan, third-party summary: 20 requests a minute, 2,000 a day, 7,200 audio seconds an hour, 28,800 a day.
- File up to 25 MB. Minimum billed length 10 s. Formats include webm, mp4, m4a, wav, mp3.
- Word times: Groq's page contradicts itself (`verbose_json` in the table, `json` in an example). Test both.
- Retention: [not retained by default](https://console.groq.com/docs/your-data). Logs may be kept up to 30 days for reliability or abuse checks. Zero Data Retention can be turned on in Groq Data Controls.

**Mistral `ministral-14b`**
- Your account: 40,000 tokens a minute and 0.03 requests a second (about 1.8 a minute). Tokens a day and requests a day: not applicable.
- An interview uses 2 calls: questions, feedback. With 1.8 calls a minute, a back-to-back second interview may wait about 30 seconds.
- **Data use:** the free Experiment plan says [API requests may be used to train Mistral's models](https://help.mistral.ai/en/articles/450104-how-can-i-try-the-api-for-free-with-the-experiment-plan). The opt-out is in the Admin Console: Privacy, toggle "Anonymous improvement data" ([how](https://help.mistral.ai/en/articles/347614-can-i-opt-out-of-my-input-or-output-data-being-used-for-training)). This also covers your CV in fit scoring.

**Browser**
- Recording uses `MediaRecorder`. Reading aloud uses `speechSynthesis`. Both run on the device.
- Microphone needs a secure context. `localhost` qualifies.

---

## 9. Non-functional requirements

| ID | Area | Requirement |
|---|---|---|
| NFR-1 | Performance | Start screen loads in under 1 s (local). |
| NFR-2 | Performance | Questions are written within 15 s in a normal run. |
| NFR-3 | Performance | An answer of 90 s is transcribed within 10 s. |
| NFR-4 | Performance | Feedback is Ready within 90 s of the last answer in 9 of 10 normal runs. Measure in the test (section 11). |
| NFR-5 | Reliability | Each answer is saved the moment its transcript arrives. A crash keeps earlier answers. |
| NFR-6 | Reliability | Retries are safe. Retrying never creates a second interview or a duplicate call for a finished step. |
| NFR-7 | Reliability | A failed provider call never loses the interview. The status shows Failed with Try again. |
| NFR-8 | Privacy | No audio on disk or in logs. No transcript, CV or answer text in logs, the call log or Langfuse. |
| NFR-9 | Privacy | Contact details are removed from CV text before any model call. |
| NFR-10 | Privacy | Provider data terms are shown in the spec and checked before first use (section 8.5). |
| NFR-11 | Security | API keys are read from the environment or Keychain. Never stored, never sent to the browser. |
| NFR-12 | Security | The upload route accepts audio only, up to 25 MB, from the local app only. |
| NFR-13 | Security | Model output is treated as untrusted. It is rendered as plain text, never as HTML. |
| NFR-14 | Usability | Start an interview in at most 3 clicks from the Roles list. |
| NFR-15 | Usability | Sentences are short. Words match the terms in section 1. |
| NFR-16 | Usability | The interview screen has no more than three buttons. |
| NFR-17 | Accessibility | All controls work by keyboard. Focus is visible. |
| NFR-18 | Accessibility | Colour is never the only signal (FR-H10). Text contrast meets WCAG AA. |
| NFR-19 | Accessibility | Questions are always shown as text. Reading aloud is optional. |
| NFR-20 | Compatibility | Chrome and Safari on Mac are supported. Firefox is best effort. |
| NFR-21 | Dark mode | All new screens work in the app's light and dark themes. |
| NFR-22 | Storage | About 30 KB per interview (section 10). |
| NFR-23 | Cost | Runs on free tiers only. The app shows when a limit is reached. |
| NFR-24 | Maintainability | Rating limits, word lists, prompts and example cards sit in separate modules. |
| NFR-25 | Testability | Providers are behind small interfaces so tests use fakes. |
| NFR-26 | Observability | Per interview: counts of calls, retries, timings and errors, with no content. |
| NFR-27 | Language | English only. |

---

## 10. Storage estimate

| Item | Size |
|---|---|
| Questions | about 1 KB |
| Transcripts (5 × about 150 words) | about 5 to 8 KB |
| Feedback and metrics | about 15 to 25 KB |
| **Per interview** | **about 30 KB** |
| 100 interviews | about 3 MB |
| 1,000 interviews | about 30 MB |

Audio exists only in browser memory, about 1.5 MB for a 3-minute answer.

---

## 11. Test plan

**Before build (one-time checks)**

| # | Check | Pass when |
|---|---|---|
| 1 | Groq word times: send one file with `json` and with `verbose_json` | One returns start and end time for every word |
| 2 | Fillers: record "um, uh, like" speech. Try with and without the prompt hint | Fillers appear in the transcript, or fillers are labelled approximate |
| 3 | Mistral: 5 real answers through the feedback prompt | Valid JSON. Scores agree with your own view. Quotes are found |
| 4 | Mistral: same answer 3 times | Rating spread is 10 points or less |
| 5 | Feedback timing | Meets NFR-4 |
| 6 | Mistral privacy toggle | "Anonymous improvement data" is off (your action) |
| 7 | Groq Zero Data Retention | Decide to turn on or not (your action) |

**Automated tests (during build)**
- Metric functions: fixed transcripts and word times give exact values (pace, pauses, fillers, hedges, ownership, thinking time).
- Rating and band functions: every boundary value (119, 120, 160, 161, and so on).
- Rubric score maths against the worked examples in sections 5 and 6.
- Quote check, including case, punctuation and missing quotes.
- Number check on improved answers.
- Change since last interview: same kind, different kind, under 5 points, first interview.
- Status moves and the 30-minute rule.
- Config validation (roles list, time limits).
- Routes with fake Groq and fake Mistral: happy path, End early, Incomplete, Failed, retry, delete, role removed.
- No audio and no content in logs.

**Manual checks**
- Real microphone on Chrome and Safari. Permission refused. Device unplugged.
- A full interview start to finish, then compare scores with your own judgement.

---

## 12. Out of scope (v1)

- Video: camera, on-camera rubric, expression, posture, gestures, eye contact, framing, any image analysis.
- Live follow-up questions.
- A single blended overall score.
- Recording playback or any stored audio.
- Re-recording a single answer.
- Editing generated questions or editing transcripts.
- Languages other than English. Accent or pronunciation scoring.
- Emotion or sentiment detection.
- Percentile or comparison with other candidates.
- Trend charts across interviews.
- Editing rubrics or ratings limits in the UI.
- Pause during an interview.
- Print, export or share a report.
- Reminders or scheduling practice.
- Several users, accounts or cloud sync.
- A question bank or imports.
- Interviewer voice or persona choices.
- Langfuse tracing of interviews.
- Mobile layout work beyond what the app already has.

---

## 13. Edge cases

**Start and setup**

| # | Case | Behaviour |
|---|---|---|
| 1 | No CV | Start disabled. Link to Upload CV. |
| 2 | CV is empty text or unreadable | Same as no CV, with "Your CV has no readable text." |
| 3 | CV changes after an interview | Stored feedback is unchanged. Header shows the CV name used. |
| 4 | Role has no description | Questions use title and company. Note shown. |
| 5 | Role has no fit verdict | Gap question uses a common gap for the title. |
| 6 | Role deleted after interview | Details shows "role removed". Feedback stays. |
| 7 | Allowed roles list empty | General role card disabled with "Add roles in Config". |
| 8 | My questions: blank, duplicate, over 300 characters | Blank ignored. Duplicates removed. Over-length shows an error. |
| 9 | Second interview started while one is running | Blocked with a link to the running one. |
| 10 | Role chosen on the start screen is hidden or removed meanwhile | "Role not found. Pick another." |

**Microphone and recording**

| # | Case | Behaviour |
|---|---|---|
| 11 | Microphone refused or missing | Cannot start. Shows how to allow it. |
| 12 | Microphone unplugged mid-interview | The current answer ends and is **not scored**. A message appears. The next question continues if a device returns. |
| 13 | Very quiet or noisy audio | Note on feedback: "Transcript may be incomplete." |
| 14 | No speech in an answer | Status no speech. Left out of averages. |
| 15 | Answer under 10 words | Too short to score. |
| 16 | Answer reaches 5:00 | Auto-ends. Next question starts. |
| 17 | Next clicked within 3 s | Ignored until enabled. |
| 18 | Speech synthesis unavailable | Text only, with a short note. |
| 19 | Question read-aloud is long | Skip reading is available. |
| 20 | Browser tab goes to the background | Recording continues. Timer uses the clock, not frames. |
| 21 | Browser closed mid-answer | That answer is lost. Earlier answers kept. Status Incomplete. |
| 22 | Computer sleeps mid-interview | On return, the current answer ends. User can continue or End. |

**Transcription and models**

| # | Case | Behaviour |
|---|---|---|
| 23 | Transcription times out | Browser retries up to 2 times, then **not scored**. |
| 24 | Groq rate limit | Wait for the time given, retry once, then not scored. |
| 25 | Groq hourly audio limit reached | Message with when to try again. Starting is allowed but warned. |
| 26 | Word times missing | Use sentence times. Show pauses as approximate. |
| 27 | Fillers missing from transcript | Fillers shown as approximate (if check 2 fails). |
| 28 | Mistral rate limit (1.8 a minute) | Wait. Show "Waiting for AI quota". |
| 29 | Scan or scoring run is busy | Interview calls queue behind the shared limit. |
| 30 | Invalid model JSON | Retry once. Then Failed. |
| 31 | Quote not found in transcript | That rating capped at 60. |
| 32 | Model puts an invented number in the improved answer | Replaced by "[add figure]". |
| 33 | Model returns fewer answers than sent | Missing answers marked "not scored". Status Ready if at least one is scored, else Failed. |
| 34 | Mistral reply cut by the token cap | Treated as invalid JSON. Retry once, then Failed. |
| 35 | No internet | Clear message. Interview stays In progress or Incomplete. Nothing is lost except audio not yet transcribed. |

**Feedback and history**

| # | Case | Behaviour |
|---|---|---|
| 36 | End interview with nothing to score | Not saved, after confirmation. |
| 37 | Only one answer scored | Feedback shown. Rubric scores use that one answer. |
| 38 | Fewer than 50 spoken words in total | Spoken rubric: "Not enough speech to measure". Answer rubric still shown. |
| 39 | No Good or all Good spoken ratings | Top 3 tips come from the model. |
| 40 | First interview of a kind | No change shown. |
| 41 | Change under 5 points | "about the same". |
| 42 | Interview opened while Preparing | Preparing page with steps. Updates every 3 s. |
| 43 | Get feedback on Incomplete with no scorable answer | Interview is removed with a message. |
| 44 | Try again on Failed after audio is gone | Uses saved transcripts only. |
| 45 | Delete during Preparing | Blocked until Ready or Failed. |
| 46 | Delete confirmation | Permanent. No undo. |
| 47 | History has no rows | Empty state with Start an interview. |
| 48 | Many interviews | 25 per page. Filter by type. |

---

## 14. Traceability

Every story is served by at least one functional requirement.

| Story | Requirements |
|---|---|
| US-1 | FR-A1, A3, B2, B5, B8, C1, C2, C8, C9 |
| US-2 | FR-A1, B3, B5, B8, C1, C2 |
| US-3 | FR-A1, B4, B5, B8, C10 |
| US-4 | FR-B1, C1, C2, C4, C5 |
| US-5 | FR-D1, D2 |
| US-6 | FR-B6, D3, D4, D5 |
| US-7 | FR-D3, D5, D6, D7, E1 |
| US-8 | FR-D3, D8, D9, J5 |
| US-9 | FR-A4, D10, J2 |
| US-10 | FR-E2, E7, F1, F2, F4, H1, H2, H6, H10 |
| US-11 | FR-G1, G2, G6, G8, H2, H5 |
| US-12 | FR-F3, F5, G4, G5, G7, G9, H3, H7 |
| US-13 | FR-H2, H4 |
| US-14 | FR-A2, I1, I2, I4, I7 |
| US-15 | FR-I2, I3 |
| US-16 | FR-G3, I5, J1, J3, J4 |
| US-17 | FR-I6, L4 |
| US-18 | FR-B7, C3, D11, E3, E6, L1, L2, L3, L5 |
| US-19 | FR-B3, K1 to K5 |
| US-20 | FR-H8 |
| US-21 | FR-C7, H9 |
| US-22 | FR-C6, E4, E5, F6, G10 |

---

## 15. Decisions

| # | Decision | Status |
|---|---|---|
| 1 | Live follow-ups in v1 | No. Agreed. |
| 2 | Blended overall score | No. Agreed. |
| 3 | Camera and video | None in v1. Agreed. |
| 4 | Audio to Groq for transcription | Yes. Agreed. |
| 5 | General role list | Allowed list in Config. Agreed. |
| 6 | Keep transcripts and feedback until deleted | Yes. About 30 KB per interview. |
| 7 | Read questions aloud by default | Yes. Agreed. |
| 8 | Speech model | Groq `whisper-large-v3`. Agreed. |
| 9 | Question and feedback model | **Mistral `ministral-14b`. Agreed.** |
| 10 | Hard stop at 5:00 per answer | Assumed. Say if you want it changed. |
| 11 | Evidence rule (quote check, cap at 60) | Assumed. Explained in section 6. |
| 12 | Rename Partial to Incomplete | Assumed. Avoids clash with fit "Partial". |
| 13 | Details for My questions: "n custom questions" | Assumed. |
| 14 | Status In progress added | New. Shown only while a run is open. |

**Your actions before build**
- Turn off "Anonymous improvement data" in the Mistral Admin Console (checks 6 and 7).
- Decide on Groq Zero Data Retention.


## 16. Build notes (deviations from the text above)

- "kind of" and "sort of" count once, as hedges only, never as fillers.
- Call pacing (`ai_calls_per_minute`) is kept inside the app process. A 429 is waited out once.
- Interview AI calls are not written to the `ai_calls` table. Counts per interview (calls, time, tokens, retries, errors) are in `interview.stats`, with no content.
- If question writing fails, the interview is deleted and the reason is shown once on the next page load. The reason is kept in memory only.
- The Preparing page polls `/interview/<id>/status` (not `?status=1`).
- Word times: the speech client asks for `verbose_json` with word and segment times and works without them. The pre-build test in section 11 is still to be run by the user.

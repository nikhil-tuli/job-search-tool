"""Ask an AI service to propose resume changes for one role, and check what comes back.

The service is any OpenAI-compatible chat endpoint (Cerebras by default). One call is made per
request. Nothing the AI says is trusted: every proposed text is checked against the rules in
tailor_docx (no longer than the original, no new numbers, same items, same bullets) and a
proposal that breaks a rule is dropped.

Only the role's text and the CV's experience text are sent. The name, phone, email and links
in the CV header are never read here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import requests

from .tailor_docx import CvStructure, check_rewrite

MAX_DESCRIPTION_CHARS = 7_000
MAX_GAPS = 6
MAX_GAP_CHARS = 200
REQUEST_TIMEOUT_SECONDS = 120

SYSTEM_PROMPT = """You help tailor a CV to one job description. Reply with a single JSON object \
and nothing else: no markdown, no commentary.

Rules:
- Only reword, reorder or emphasise what the CV already says. Never add a skill, tool, \
employer, date, number or achievement that the CV does not already contain.
- Every number in a new text must already be in the text it replaces.
- A reworded text must not be longer than the text it replaces, counted in characters.
- Keep the facts the same. Use the role's own vocabulary only where it is true for the CV.
- Change only what clearly helps. Leave everything else out of your reply.

Reply format:
{
  "summary": "new summary text, or null",
  "competencies_order": ["the same items", "in a new order"] or null,
  "jobs": [
    {"id": "j1", "order": ["j1b2", "j1b1", "..."] or null,
     "rewrites": {"j1b3": "new text for that bullet"}}
  ],
  "gaps": ["short things the role asks for that the CV does not show"]
}
Only bullets marked editable may appear in "rewrites". "order" lists every bullet id of that \
job. "gaps" has at most 6 short strings."""


class TailorError(Exception):
    """A failure with a plain-words reason that is shown in History."""


@dataclass(frozen=True)
class Proposal:
    changes: tuple[dict[str, Any], ...]
    gaps: tuple[str, ...]
    dropped: int

    def to_json(self) -> str:
        return json.dumps(
            {"changes": list(self.changes), "gaps": list(self.gaps), "dropped": self.dropped}
        )

    @classmethod
    def from_json(cls, text: str) -> Proposal:
        data = json.loads(text)
        return cls(tuple(data["changes"]), tuple(data["gaps"]), int(data["dropped"]))


# ---- the service ------------------------------------------------------------------------------


class TailorClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        *,
        session: requests.Session | None = None,
        timeout: int = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._model = model
        self._key = api_key
        self._session = session or requests.Session()
        self._timeout = timeout

    def complete(self, system: str, user: str) -> str:
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
            "max_completion_tokens": 8_000,
        }
        try:
            response = self._session.post(
                self._url,
                json=body,
                headers={"Authorization": f"Bearer {self._key}"},
                timeout=self._timeout,
            )
        except requests.Timeout:
            raise TailorError("The AI service took too long to answer. Try again.") from None
        except requests.RequestException:
            raise TailorError("The AI service could not be reached. Check your internet.") from None
        status = response.status_code
        if status in (401, 403):
            raise TailorError(
                f"The AI service refused the key (HTTP {status}). Check the key and its "
                "variable name in Config, Tailoring."
            )
        if status == 404:
            raise TailorError(
                "The AI service address or model was not found. Check Config, Tailoring."
            )
        if status == 429:
            raise TailorError("The AI service is busy (rate limit). Try again in a minute.")
        if status >= 500:
            raise TailorError(f"The AI service had a problem (HTTP {status}). Try again later.")
        if status != 200:
            raise TailorError(
                f"The AI service refused the request (HTTP {status}).{_detail(response)}"
            )
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise TailorError("The AI service reply was not in the expected form.") from None
        if not isinstance(content, str) or not content.strip():
            raise TailorError("The AI service returned an empty reply.")
        return content


def _detail(response: requests.Response) -> str:
    try:
        message = response.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return ""
    return f" {message[:160]}" if isinstance(message, str) else ""


def make_client(
    base_url: str, model: str, key_variable: str, environ: dict[str, str]
) -> TailorClient:
    key = environ.get(key_variable, "").strip()
    if not key:
        raise TailorError(
            f"The AI key is missing. Set {key_variable} in the terminal, then start "
            "jobscan serve from that terminal."
        )
    return TailorClient(base_url, model, key)


# ---- the question -----------------------------------------------------------------------------


def build_user_message(structure: CvStructure, title: str, company: str, description: str) -> str:
    lines = [f"ROLE: {title} at {company}", "", "ROLE DESCRIPTION:"]
    lines.append(description.strip()[:MAX_DESCRIPTION_CHARS])
    lines += ["", "CV", ""]
    if structure.summary:
        lines += [f"SUMMARY (you may reword this part): {structure.summary}", ""]
    if structure.competencies:
        lines += ["COMPETENCIES (reorder only): " + " | ".join(structure.competencies), ""]
    for job in structure.jobs:
        lines.append(f"JOB {job.id}: {job.header}")
        for bullet in job.bullets:
            tag = "editable" if bullet.editable else "fixed"
            label = f"{bullet.label}: " if bullet.label else ""
            lines.append(f"  {bullet.id} [{tag}] {label}{bullet.core}")
        lines.append("")
    return "\n".join(lines)


# ---- the answer -------------------------------------------------------------------------------


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object")
    data = json.loads(cleaned[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("not an object")
    return data


def _name(bullet_label: str, core: str) -> str:
    return bullet_label or (core[:50] + "..." if len(core) > 50 else core)


def parse_proposal(structure: CvStructure, reply: str) -> Proposal:
    """Check every proposed change. Raises ValueError if the reply is not usable JSON."""
    data = _extract_json(reply)
    changes: list[dict[str, Any]] = []
    dropped = 0

    def add(**fields: Any) -> None:
        changes.append({"id": f"c{len(changes) + 1}", **fields})

    new_summary = data.get("summary")
    if isinstance(new_summary, str) and structure.summary:
        new_summary = new_summary.strip()
        if check_rewrite(structure.summary, new_summary) is None:
            add(
                kind="summary",
                target="summary",
                label="Professional summary",
                before=structure.summary,
                after=new_summary,
            )
        else:
            dropped += 1

    order = data.get("competencies_order")
    if isinstance(order, list) and structure.competencies:
        items = [str(i).strip() for i in order]
        if sorted(items) == sorted(structure.competencies):
            if items != list(structure.competencies):
                add(
                    kind="competencies",
                    target="competencies",
                    label="Core competencies",
                    before=list(structure.competencies),
                    after=items,
                )
        else:
            dropped += 1

    jobs = {job.id: job for job in structure.jobs}
    for entry in data.get("jobs") or []:
        if not isinstance(entry, dict) or entry.get("id") not in jobs:
            dropped += 1
            continue
        job = jobs[entry["id"]]
        bullets = {b.id: b for b in job.bullets}
        rewrites = entry.get("rewrites")
        for bullet_id, text in rewrites.items() if isinstance(rewrites, dict) else []:
            bullet = bullets.get(bullet_id)
            new = text.strip() if isinstance(text, str) else ""
            if bullet is None or not bullet.editable or check_rewrite(bullet.core, new):
                dropped += 1
                continue
            add(
                kind="bullet",
                target=bullet_id,
                job=job.header,
                label=bullet.label or "Bullet",
                before=bullet.core,
                after=new,
            )
        new_order = entry.get("order")
        if isinstance(new_order, list):
            ids = [str(i) for i in new_order]
            current = [b.id for b in job.bullets]
            if sorted(ids) != sorted(current):
                dropped += 1
            elif ids != current:
                add(
                    kind="order",
                    target=job.id,
                    job=job.header,
                    label="Order of bullets",
                    before=[_name(bullets[i].label, bullets[i].core) for i in current],
                    after=[_name(bullets[i].label, bullets[i].core) for i in ids],
                    before_ids=current,
                    after_ids=ids,
                )

    gaps: list[str] = []
    for gap in data.get("gaps") or []:
        if isinstance(gap, str) and gap.strip() and len(gaps) < MAX_GAPS:
            gaps.append(gap.strip()[:MAX_GAP_CHARS])
    return Proposal(tuple(changes), tuple(gaps), dropped)


def propose(
    client: TailorClient, structure: CvStructure, title: str, company: str, description: str
) -> Proposal:
    """One AI call, with one more only if the reply could not be read."""
    user = build_user_message(structure, title, company, description)
    last_error = ""
    for attempt in range(2):
        message = (
            user
            if attempt == 0
            else (user + "\n\nYour last reply was not valid JSON. Reply with the JSON object only.")
        )
        reply = client.complete(SYSTEM_PROMPT, message)
        try:
            return parse_proposal(structure, reply)
        except (ValueError, TypeError) as exc:
            last_error = str(exc)
    raise TailorError(f"The AI reply could not be read ({last_error}). Try again.")

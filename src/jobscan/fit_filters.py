"""Rule filter: which roles are worth scoring against the CV.

The rules only explain. A role that fails one stays in the list with a reason tag, and nothing
is deleted or hidden. Every value comes from the Config page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import Config, split_list
from .models import Role
from .timeutil import from_iso

MAX_YEARS = 50  # Larger numbers are not experience requirements ("20 years ago" aside).

# 5 years, 5+ years, 3 to 5 years, 1-5 yrs, 10-15+ Years. Group 1 is the lower bound.
_YEARS = re.compile(
    r"(?<![\d.])(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|\u2013|\u2014|to)\s*\d{1,2}\s*\+?)?"
    r"\s*(?:years?|yrs?)\b",
    re.IGNORECASE,
)
# "Associate Director" is a senior title, so "associate" only counts as junior without these.
_SENIOR_AFTER = r"(?!\s+(?:director|vice|vp|principal|partner|head)\b)"
_JUNIOR_EXCEPTIONS = {"associate": _SENIOR_AFTER}

REASON_STATUS = "status"
REASON_RECENCY = "recency"
REASON_COMPANY = "company"
REASON_SENIORITY = "seniority"
REASON_TITLE = "title keyword"
REASON_YEARS = "years"
REASON_INDUSTRY = "industry"


@dataclass(frozen=True)
class FitRules:
    recency_days: int
    min_years: int
    junior_words: tuple[str, ...]
    junior_levels: tuple[str, ...]
    excluded_companies: tuple[str, ...]
    skip_statuses: tuple[str, ...]
    deny_keywords: tuple[str, ...]
    excluded_industries: tuple[str, ...]

    @classmethod
    def from_config(cls, config: Config) -> FitRules:
        return cls(
            recency_days=config.fit_recency_days,
            min_years=config.fit_min_years,
            junior_words=split_list(config.fit_junior_words),
            junior_levels=split_list(config.fit_junior_levels),
            excluded_companies=split_list(config.fit_excluded_companies),
            skip_statuses=split_list(config.fit_skip_statuses),
            deny_keywords=split_list(config.fit_deny_keywords),
            excluded_industries=split_list(config.fit_excluded_industries),
        )


@dataclass(frozen=True)
class Reason:
    kind: str  # one of the REASON_ constants, shown in the tag
    detail: str  # the full explanation, shown on hover and in the panel


@dataclass(frozen=True)
class FitResult:
    reasons: tuple[Reason, ...]

    @property
    def eligible(self) -> bool:
        return not self.reasons

    @property
    def label(self) -> str:
        return "Eligible" if self.eligible else f"Excluded: {self.reasons[0].kind}"

    @property
    def more(self) -> int:
        """How many further reasons sit behind the one shown in the tag."""
        return max(len(self.reasons) - 1, 0)

    @property
    def summary(self) -> str:
        return "; ".join(reason.detail for reason in self.reasons)


def required_years(description: str | None) -> int | None:
    """The most demanding minimum of experience the description asks for.

    "5+ years", "3 to 5 years" and "1-5 years" give 5, 3 and 1. The highest minimum wins, so a
    role is only seen as light on experience when every figure in it is low. None means the
    description names no years.
    """
    if not description:
        return None
    found = [int(m.group(1)) for m in _YEARS.finditer(description)]
    found = [n for n in found if n <= MAX_YEARS]
    return max(found) if found else None


def evaluate(role: Role, rules: FitRules, now: datetime) -> FitResult:
    """Every rule the role fails, in a fixed order (the first is the one shown in the tag)."""
    reasons: list[Reason] = []
    if role.status.casefold() in {s.casefold() for s in rules.skip_statuses}:
        reasons.append(Reason(REASON_STATUS, f"status is {role.status}"))
    age = now - from_iso(role.fetched_on)
    if age > timedelta(days=rules.recency_days):
        reasons.append(Reason(REASON_RECENCY, f"fetched more than {_days(rules.recency_days)} ago"))
    company = _first_hit(rules.excluded_companies, role.company)
    if company:
        reasons.append(Reason(REASON_COMPANY, f"company matches {company}"))
    junior = _junior_hit(rules, role)
    if junior:
        reasons.append(Reason(REASON_SENIORITY, junior))
    deny = _first_hit(rules.deny_keywords, role.title)
    if deny:
        reasons.append(Reason(REASON_TITLE, f'title contains "{deny}"'))
    industry = _industry_hit(rules.excluded_industries, role.industries)
    if industry:
        reasons.append(Reason(REASON_INDUSTRY, f"LinkedIn industry includes {industry}"))
    if rules.min_years > 0:
        years = required_years(role.description)
        if years is not None and years < rules.min_years:
            reasons.append(
                Reason(
                    REASON_YEARS,
                    f"asks for {years} {'year' if years == 1 else 'years'}, "
                    f"below {rules.min_years}",
                )
            )
    return FitResult(tuple(reasons))


def _days(count: int) -> str:
    return f"{count} day" if count == 1 else f"{count} days"


def _pattern(phrase: str, after: str = "") -> re.Pattern[str]:
    """The phrase as whole words, any spacing between them, any letter case."""
    words = r"\s+".join(re.escape(word) for word in phrase.split())
    return re.compile(rf"(?<!\w){words}(?!\w){after}", re.IGNORECASE)


def _first_hit(phrases: tuple[str, ...], text: str) -> str | None:
    for phrase in phrases:
        if _pattern(phrase).search(text):
            return phrase
    return None


def industry_list(industries: str | None) -> tuple[str, ...]:
    """The industries LinkedIn lists for a role, from its comma separated Industries line."""
    return split_list(industries or "")


def _industry_hit(excluded: tuple[str, ...], industries: str | None) -> str | None:
    """The first excluded industry the role lists. Whole names only, any letter case."""
    listed = {name.casefold() for name in industry_list(industries)}
    for name in excluded:
        if name.casefold() in listed:
            return name
    return None


def _junior_hit(rules: FitRules, role: Role) -> str | None:
    for word in rules.junior_words:
        after = _JUNIOR_EXCEPTIONS.get(word.casefold(), "")
        if _pattern(word, after).search(role.title):
            return f'title contains "{word}"'
    if role.seniority and role.seniority.casefold() in {
        level.casefold() for level in rules.junior_levels
    }:
        return f"LinkedIn level is {role.seniority}"
    return None

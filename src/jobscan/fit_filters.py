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

MIN_PLAUSIBLE_SALARY = 50_000  # a yearly figure below this is a misread, not a salary

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

REASON_RECENCY = "recency"
REASON_COMPANY = "company"
REASON_SENIORITY = "seniority"
REASON_TITLE = "title keyword"
REASON_YEARS = "years"
REASON_INDUSTRY = "industry"
REASON_SALARY = "salary"

_CUR = r"(?:\u20b9|\brs\.?|\binr)"
_NUM = r"(\d[\d,]*(?:\.\d+)?)"
_UNIT = r"(lakhs?|lacs?|lpa|crores?|cr|k)\b"
_SEP = r"(?:-|\u2013|\u2014|to)"
_MID = r"(?:\s*(?:/\s*|per\s+|a\s+|p\.?\s*)(?:yr|year|annum|a|month|mo|m)\b\.?)?"
# Rs 6,00,000 - 9,00,000 a year; INR 8-12 lakh; \u20b950,000 per month.
# Groups: 1 first figure, 2 its unit, 3 second figure, 4 its unit.
_WITH_CURRENCY = re.compile(
    rf"(?<![\w.]){_CUR}\s*{_NUM}(?:\s*{_UNIT})?{_MID}"
    rf"(?:\s*{_SEP}\s*(?:{_CUR}\s*)?{_NUM}(?:\s*{_UNIT})?)?",
    re.IGNORECASE,
)
# 8-12 LPA, 10 lakhs, 6 to 9 lacs, 10 lakh - 14 lakh: the unit alone says it is rupees a year.
_WITH_LAKH = re.compile(
    rf"(?<![\w.]){_NUM}(?:\s*(lakhs?|lacs?|lpa|crores?)\b)?{_MID}"
    rf"(?:\s*{_SEP}\s*{_NUM}\s*(lakhs?|lacs?|lpa|crores?)\b)?",
    re.IGNORECASE,
)
_PERIOD = re.compile(
    r"\s*(?:/\s*|per\s+|a\s+|p\.?\s*)(yr|year|annum|a\b|month|mo|m\b|hour|hr)"
    r"|\s*(annually|yearly|monthly)",
    re.IGNORECASE,
)
_MULTIPLIER = {"lakh": 100_000, "lac": 100_000, "lpa": 100_000, "crore": 10_000_000}


@dataclass(frozen=True)
class FitRules:
    recency_days: int
    min_years: int
    min_salary: int
    junior_words: tuple[str, ...]
    junior_levels: tuple[str, ...]
    excluded_companies: tuple[str, ...]
    deny_keywords: tuple[str, ...]
    excluded_industries: tuple[str, ...]

    @classmethod
    def from_config(cls, config: Config) -> FitRules:
        return cls(
            recency_days=config.fit_recency_days,
            min_years=config.fit_min_years,
            min_salary=config.fit_min_salary,
            junior_words=split_list(config.fit_junior_words),
            junior_levels=split_list(config.fit_junior_levels),
            excluded_companies=split_list(config.fit_excluded_companies),
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


def _period_factor(after: str) -> int | None:
    """12 for a monthly figure, 1 for a yearly one, None when no period is given or it is hourly."""
    found = _PERIOD.match(after)
    if not found:
        return None
    word = (found.group(1) or found.group(2)).casefold()
    if word in ("hour", "hr"):
        return 0
    return 12 if word.startswith(("month", "mo", "m")) else 1


def _rupees(figure: str, unit: str | None, after: str) -> int | None:
    """One figure as rupees a year, or None when it is unclear what it means."""
    try:
        number = float(figure.replace(",", ""))
    except ValueError:
        return None
    period = _period_factor(after)
    if period == 0:
        return None
    name = (unit or "").casefold().rstrip("s")
    if name in _MULTIPLIER:
        return int(number * _MULTIPLIER[name] * (period or 1))
    if name == "cr":
        return int(number * _MULTIPLIER["crore"] * (period or 1))
    if period is None:
        return None  # a bare amount with no unit or period could be monthly or yearly
    scale = 1_000 if name == "k" else 1
    return int(number * scale * period)


def listed_salary(description: str | None) -> int | None:
    """The lower end of the salary a description lists, in rupees a year.

    Reads Rs, INR and rupee-sign amounts with a yearly or monthly period, and lakh, LPA and
    crore amounts. When several figures appear the highest lower end wins, so a stray small
    number cannot exclude a role. Anything unclear is ignored. None means no salary was found.
    """
    if not description:
        return None
    found: list[int] = []
    spans: list[tuple[int, int]] = []
    for match in _WITH_CURRENCY.finditer(description):
        spans.append(match.span())
        unit = match.group(2) or match.group(4)
        first_end = match.end(2) if match.group(2) else match.end(1)
        figure = _rupees(match.group(1), unit, description[first_end:])
        if figure is None:
            figure = _rupees(match.group(1), unit, description[match.end() :])
        if figure is not None:
            found.append(figure)
    for match in _WITH_LAKH.finditer(description):
        if any(start <= match.start() < end for start, end in spans):
            continue
        unit = match.group(2) or match.group(4)
        if unit is None:
            continue  # a plain number is not a salary without a unit
        first_end = match.end(2) if match.group(2) else match.end(1)
        figure = _rupees(match.group(1), unit, description[first_end:])
        if figure is None:
            figure = _rupees(match.group(1), unit, description[match.end() :])
        if figure is not None:
            found.append(figure)
    found = [n for n in found if n >= MIN_PLAUSIBLE_SALARY]
    return max(found) if found else None


def rupee_text(amount: int) -> str:
    """A yearly amount as Indian people write it: 6.5 lakh, 1.2 crore."""
    if amount >= 10_000_000:
        return f"\u20b9{amount / 10_000_000:g} crore"
    if amount >= 100_000:
        return f"\u20b9{amount / 100_000:g} lakh"
    return f"\u20b9{amount:,}"


def evaluate(
    role: Role, rules: FitRules, now: datetime, *, ignore_recency: bool = False
) -> FitResult:
    """Every rule the role fails, in a fixed order (the first is the one shown in the tag).

    The role's status is never a reason: it has its own column. `ignore_recency` is for the
    one-off snapshot of roles scored before Fit was saved, whose age then is not known.
    """
    reasons: list[Reason] = []
    age = now - from_iso(role.fetched_on)
    if not ignore_recency and age > timedelta(days=rules.recency_days):
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
    if rules.min_salary > 0:
        salary = listed_salary(role.description)
        if salary is not None and salary < rules.min_salary:
            reasons.append(
                Reason(
                    REASON_SALARY,
                    f"lists {rupee_text(salary)} a year, below {rupee_text(rules.min_salary)}",
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

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from jobscan.config import Config, ConfigError, split_list
from jobscan.fit_filters import FitRules, evaluate, listed_salary, required_years, rupee_text
from jobscan.models import Role
from jobscan.timeutil import IST, to_iso

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=IST)


def make_role(**overrides: object) -> Role:
    fields: dict[str, object] = {
        "portal": "LinkedIn",
        "job_id": "1",
        "title": "Technical Program Manager",
        "company": "Acme",
        "location": "Pune, India",
        "link": "https://www.linkedin.com/jobs/view/1",
        "posted_on": None,
        "posted_precision": "hour",
        "fetched_on": to_iso(NOW - timedelta(hours=2)),
        "actively_hiring": False,
        "matched_phrase": "Technical Program Manager",
        "possible_duplicate_of": None,
        "status": "Not Applied",
        "applied_on": None,
        "contact_person": "",
        "comments": "",
        "raw_datetime": None,
        "raw_relative": None,
        "first_run_id": 1,
    }
    fields.update(overrides)
    return Role(**fields)  # type: ignore[arg-type]


RULES = FitRules.from_config(Config())


def kinds(role: Role, rules: FitRules = RULES) -> list[str]:
    return [reason.kind for reason in evaluate(role, rules, NOW).reasons]


# ---- years ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Experience: 10-15+ Years", 10),
        ("Experience: 1-5 years in a relevant role", 1),
        ("5 to 10 years managing projects", 5),
        ("Candidates must have a minimum of 4 years and a maximum of 7 years", 7),
        ("Project Management (6+ years) - Business Analysis (3+ years)", 6),
        ("8\u201312 years of experience", 8),  # en dash
        ("15 - 20 years of experience in water treatment", 15),
        ("3 + years of experience", 3),
        ("2 yrs in delivery", 2),
        ("1 year of experience", 1),
        ("about 1.5 years of experience", None),
        ("founded 2026 years", None),
        ("a 120 years old firm", None),
        ("No numbers here", None),
        ("", None),
        (None, None),
    ],
)
def test_required_years(text: str | None, expected: int | None) -> None:
    assert required_years(text) == expected


def test_highest_minimum_wins_so_noise_cannot_hide_a_senior_role() -> None:
    text = "Junior (1-3 Years) roles are elsewhere. This role needs 8+ years of delivery."
    assert required_years(text) == 8


# ---- rules ---------------------------------------------------------------------------------


def test_a_plain_recent_role_is_eligible() -> None:
    result = evaluate(make_role(), RULES, NOW)
    assert result.eligible
    assert result.label == "Eligible"
    assert result.summary == ""


@pytest.mark.parametrize("status", ["Applied", "Closed", "Skipped", "Not relevant", "applied"])
def test_actioned_statuses_are_excluded(status: str) -> None:
    assert kinds(make_role(status=status)) == ["status"]


@pytest.mark.parametrize("status", ["Not Applied", "Interview Ongoing"])
def test_other_statuses_are_not_excluded(status: str) -> None:
    assert kinds(make_role(status=status)) == []


def test_recency_boundary() -> None:
    exactly = make_role(fetched_on=to_iso(NOW - timedelta(days=1)))
    just_over = make_role(fetched_on=to_iso(NOW - timedelta(days=1, seconds=1)))
    assert kinds(exactly) == []
    assert kinds(just_over) == ["recency"]


def test_recency_follows_the_setting() -> None:
    week_old = make_role(fetched_on=to_iso(NOW - timedelta(days=5)))
    assert kinds(week_old) == ["recency"]
    assert kinds(week_old, replace(RULES, recency_days=7)) == []


@pytest.mark.parametrize(
    ("company", "hit"),
    [
        ("Amazon", True),
        ("amazon", True),
        ("Amazon Development Centre India", True),
        ("Amazon Web Services (AWS)", True),
        ("Intuit", True),
        ("Amazonian Foods", False),
        ("Acme", False),
    ],
)
def test_excluded_companies(company: str, hit: bool) -> None:
    assert (kinds(make_role(company=company)) == ["company"]) is hit


@pytest.mark.parametrize(
    ("title", "hit"),
    [
        ("Intern - Program Management", True),
        ("Junior Project Manager", True),
        ("Jr. Delivery Manager", True),
        ("Trainee Program Manager", True),
        ("Associate Technical Program Manager", True),
        ("associate project manager", True),
        ("Associate Director, Program Management", False),
        ("Associate Vice President - Delivery", False),
        ("Associate Principal TPM", False),
        ("Internal Tools Program Manager", False),
        ("Senior Technical Program Manager", False),
    ],
)
def test_junior_titles(title: str, hit: bool) -> None:
    assert (kinds(make_role(title=title)) == ["seniority"]) is hit


@pytest.mark.parametrize(("level", "hit"), [("Entry level", True), ("internship", True),
                                            ("Mid-Senior level", False), ("Associate", False),
                                            (None, False)])  # fmt: skip
def test_junior_linkedin_levels(level: str | None, hit: bool) -> None:
    assert (kinds(make_role(seniority=level)) == ["seniority"]) is hit


@pytest.mark.parametrize(
    ("title", "hit"),
    [
        ("Sales Program Manager", True),
        ("Program Manager, Warehouse Operations", True),
        ("SAP Functional Lead", True),
        ("SAP   functional consultant", True),
        ("Salesforce Program Manager", False),  # whole words only
        ("SAP Delivery Manager", False),
        ("Technical Program Manager", False),
    ],
)
def test_deny_keywords_match_the_title_only(title: str, hit: bool) -> None:
    assert (kinds(make_role(title=title)) == ["title keyword"]) is hit


def test_deny_keywords_ignore_the_description() -> None:
    role = make_role(description="You will work with the sales team and the warehouse.")
    assert kinds(role) == []


@pytest.mark.parametrize(
    ("description", "hit"),
    [
        ("Experience: 1-5 years", True),
        ("Minimum 2 years of experience in program management", True),
        ("2 years of experience. Preferred: 2 years managing teams.", True),
        ("Experience: 3 years", False),
        ("8+ years of experience", False),
        ("Candidates need 2 years in X and 10 years overall", False),
        ("No years are named in this description", False),
        (None, False),
    ],
)
def test_years_rule(description: str | None, hit: bool) -> None:
    assert (kinds(make_role(description=description)) == ["years"]) is hit


def test_years_rule_can_be_switched_off() -> None:
    role = make_role(description="Experience: 1-2 years")
    assert kinds(role, replace(RULES, min_years=0)) == []


def test_years_reason_is_readable() -> None:
    result = evaluate(make_role(description="Experience: 1 year"), RULES, NOW)
    assert result.summary == "asks for 1 year, below 3"


def test_several_failures_are_all_reported_in_a_fixed_order() -> None:
    role = make_role(
        status="Applied",
        company="Amazon",
        title="Junior Sales Manager",
        fetched_on=to_iso(NOW - timedelta(days=9)),
        description="Experience: 1 year",
    )
    result = evaluate(role, RULES, NOW)
    assert [r.kind for r in result.reasons] == [
        "status",
        "recency",
        "company",
        "seniority",
        "title keyword",
        "years",
    ]
    assert result.label == "Excluded: status"
    assert result.more == 5


def test_empty_lists_switch_a_rule_off() -> None:
    rules = FitRules.from_config(
        Config.from_raw(
            {
                "fit_junior_words": "",
                "fit_junior_levels": "",
                "fit_excluded_companies": "",
                "fit_skip_statuses": "",
                "fit_deny_keywords": "",
                "fit_min_years": "0",
                "fit_recency_days": "365",
            }
        )
    )
    role = make_role(
        status="Applied",
        company="Amazon",
        title="Junior Sales Manager",
        seniority="Entry level",
        description="Experience: 1 year",
    )
    assert evaluate(role, rules, NOW).eligible


# ---- config --------------------------------------------------------------------------------


def test_defaults_match_the_requirements() -> None:
    config = Config()
    assert config.fit_recency_days == 1
    assert config.fit_min_years == 3
    assert split_list(config.fit_excluded_companies) == ("Amazon", "Intuit")
    assert split_list(config.fit_skip_statuses) == ("Applied", "Closed", "Skipped", "Not relevant")
    assert split_list(config.fit_deny_keywords) == ("sales", "warehouse", "SAP functional")


def test_lists_are_stored_tidy() -> None:
    config = Config.from_raw(
        {"fit_deny_keywords": " Sales ,,warehouse\nSAP   functional, sales ", "geo_id": "1"}
    )
    assert config.fit_deny_keywords == "Sales, warehouse, SAP functional"


def test_status_names_are_fixed_to_their_proper_case() -> None:
    config = Config.from_raw({"fit_skip_statuses": "applied, NOT relevant"})
    assert config.fit_skip_statuses == "Applied, Not relevant"


def test_unknown_status_is_rejected() -> None:
    with pytest.raises(ConfigError) as caught:
        Config.from_raw({"fit_skip_statuses": "Applied, Banana"})
    assert caught.value.errors == {"fit_skip_statuses": "not a status: Banana"}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("fit_recency_days", "0"),
        ("fit_recency_days", "400"),
        ("fit_min_years", "-1"),
        ("fit_min_years", "31"),
        ("fit_min_years", "two"),
        ("fit_deny_keywords", "x" * 81),
        ("fit_deny_keywords", ",".join(str(n) for n in range(101))),
    ],
)
def test_bad_fit_settings_are_rejected(key: str, value: str) -> None:
    with pytest.raises(ConfigError) as caught:
        Config.from_raw({key: value})
    assert key in caught.value.errors


def test_config_round_trips_through_raw() -> None:
    config = Config.from_raw({"fit_excluded_companies": "Foo, Bar", "fit_min_years": "5"})
    assert Config.from_raw(config.to_raw()) == config


# ---- excluded industries -------------------------------------------------------------------

INDUSTRY_RULES = FitRules.from_config(
    Config(fit_excluded_industries="Staffing and Recruiting, Construction")
)


def test_no_industry_is_excluded_by_default() -> None:
    assert RULES.excluded_industries == ()
    assert kinds(make_role(industries="Staffing and Recruiting")) == []


def test_an_excluded_industry_tags_the_role() -> None:
    role = make_role(industries="Software Development, Staffing and Recruiting")
    result = evaluate(role, INDUSTRY_RULES, NOW)
    assert [r.kind for r in result.reasons] == ["industry"]
    assert result.reasons[0].detail == "LinkedIn industry includes Staffing and Recruiting"
    assert result.label == "Excluded: industry"


def test_industry_matching_ignores_case_and_spacing() -> None:
    role = make_role(industries="  construction ,Software Development")
    assert kinds(role, INDUSTRY_RULES) == ["industry"]


def test_industry_names_match_whole_names_only() -> None:
    assert kinds(make_role(industries="Construction Software"), INDUSTRY_RULES) == []
    assert kinds(make_role(industries="Staffing"), INDUSTRY_RULES) == []


def test_a_role_without_industries_is_not_excluded() -> None:
    assert kinds(make_role(industries=None), INDUSTRY_RULES) == []
    assert kinds(make_role(industries=""), INDUSTRY_RULES) == []


def test_industry_reason_sits_after_the_title_rules_and_before_years() -> None:
    config = Config(fit_excluded_industries="Construction", fit_deny_keywords="sales")
    role = make_role(
        title="Sales Manager", industries="Construction", description="1 year of experience"
    )
    assert kinds(role, FitRules.from_config(config)) == ["title keyword", "industry", "years"]


def test_industry_list_is_tidy() -> None:
    from jobscan.fit_filters import industry_list

    assert industry_list("A, b ,,A") == ("A", "b")
    assert industry_list(None) == ()


def test_excluded_industries_are_a_tidy_list_setting() -> None:
    config = Config.from_raw({"fit_excluded_industries": " Construction ,,construction, Mining "})
    assert config.fit_excluded_industries == "Construction, Mining"
    assert Config.from_raw(config.to_raw()) == config


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Salary: \u20b96,00,000.00/yr - \u20b99,00,000.00/yr", 600_000),
        ("CTC 8-12 LPA", 800_000),
        ("INR 50,000 per month", 600_000),
        ("Rs. 10 lakh - 14 lakh per annum", 1_000_000),
        ("up to \u20b925 LPA", 2_500_000),
        ("\u20b910-15 lakhs", 1_000_000),
        ("\u20b940k a month", 480_000),
        ("1.5 crore a year", 15_000_000),
        ("\u20b98,00,000 - \u20b912,00,000 per year", 800_000),
        ("\u20b950,000 - \u20b970,000 per month", 600_000),
        ("CTC 6 LPA, or \u20b950,00,000 per year", 5_000_000),
        ("bonus \u20b95,000 on joining", None),
        ("$120,000 a year", None),
        ("\u20b91,20,000 per hour", None),
        ("\u20b980,000", None),  # monthly or yearly: unclear, so ignored
        ("Experience 5-8 years", None),
        ("10 positions open", None),
        ("", None),
        (None, None),
    ],
)
def test_listed_salary(text: str | None, expected: int | None) -> None:
    assert listed_salary(text) == expected


@pytest.mark.parametrize(
    ("description", "hit"),
    [
        ("CTC 6-9 LPA", True),
        ("\u20b950,000 per month", True),  # 6 lakh a year
        ("CTC 8-14 LPA", True),  # the lower end decides
        ("CTC 10-14 LPA", False),
        ("\u20b91,00,000 per month", False),
        ("No pay is mentioned", False),
        (None, False),
    ],
)
def test_salary_rule_uses_the_lower_end(description: str | None, hit: bool) -> None:
    assert (kinds(make_role(description=description)) == ["salary"]) is hit


def test_salary_rule_can_be_switched_off() -> None:
    role = make_role(description="CTC 3-4 LPA")
    assert kinds(role, replace(RULES, min_salary=0)) == []


def test_salary_reason_is_readable() -> None:
    result = evaluate(make_role(description="CTC 6-9 LPA"), RULES, NOW)
    assert result.summary == "lists \u20b96 lakh a year, below \u20b910 lakh"


def test_salary_minimum_defaults_to_ten_lakh() -> None:
    assert Config().fit_min_salary == 1_000_000
    assert rupee_text(1_000_000) == "\u20b910 lakh"
    assert rupee_text(15_000_000) == "\u20b91.5 crore"

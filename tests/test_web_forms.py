from __future__ import annotations

from datetime import datetime

import pytest

from jobscan.models import DEFAULT_PHRASES, Role
from jobscan.timeutil import IST
from jobscan.web.forms import (
    csv_cell,
    parse_config_form,
    parse_role_form,
    resolve_applied_on,
    roles_to_csv,
    safe_next,
)

TODAY = datetime(2026, 10, 15, 9, 30, tzinfo=IST)


# ---- applied-on rules --------------------------------------------------------------------


def test_resolve_applied_on_rules() -> None:
    assert resolve_applied_on("Not Applied", "2026-10-01T00:00:00+05:30", TODAY) is None
    assert resolve_applied_on("Applied", None, TODAY) == "2026-10-15T09:30:00+05:30"
    assert resolve_applied_on("Applied", "2026-10-01T00:00:00+05:30", TODAY) == (
        "2026-10-01T00:00:00+05:30"
    )
    assert resolve_applied_on("Interview Ongoing", None, TODAY) is None


# ---- role form ---------------------------------------------------------------------------


def role_form(**overrides: str) -> dict[str, str]:
    base = {
        "status": "Applied",
        "applied_on": "2026-10-10",
        "contact_person": " Priya ",
        "comments": "line one\r\nline two ",
    }
    base.update(overrides)
    return base


def test_role_form_parses_and_normalises() -> None:
    edit, errors = parse_role_form(role_form(), TODAY)
    assert errors == {}
    assert edit is not None
    assert edit.applied_on == "2026-10-10T00:00:00+05:30"
    assert edit.contact_person == "Priya"
    assert edit.comments == "line one\nline two"


def test_role_form_blank_date() -> None:
    edit, errors = parse_role_form(role_form(applied_on="", status="Not Applied"), TODAY)
    assert errors == {}
    assert edit is not None
    assert edit.applied_on is None


def test_role_form_applied_without_date_gets_today() -> None:
    edit, _ = parse_role_form(role_form(applied_on=""), TODAY)
    assert edit is not None
    assert edit.applied_on == "2026-10-15T09:30:00+05:30"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "Bogus"),
        ("status", ""),
        ("applied_on", "15/10/2026"),
        ("applied_on", "2026-13-40"),
        ("applied_on", "2026-10-16"),
        ("contact_person", "x" * 201),
        ("comments", "x" * 5001),
    ],
)
def test_role_form_errors(field: str, value: str) -> None:
    edit, errors = parse_role_form(role_form(**{field: value}), TODAY)
    assert edit is None
    assert field in errors


def test_role_form_today_is_allowed_for_applied_on() -> None:
    edit, errors = parse_role_form(role_form(applied_on="2026-10-15"), TODAY)
    assert errors == {}
    assert edit is not None


def test_role_form_collects_every_error() -> None:
    _, errors = parse_role_form(role_form(status="x", applied_on="x"), TODAY)
    assert set(errors) == {"status", "applied_on"}


# ---- config form -------------------------------------------------------------------------


def config_form(**overrides: str) -> dict[str, str]:
    from jobscan.config import Config

    form = Config().to_raw()
    form.update(
        {
            "phrase_count": "2",
            "phrase_0": "Technical Program Manager",
            "keyword_0": "1",
            "match_0": "1",
            "phrase_1": "TPM",
            "match_1": "1",
        }
    )
    form.update(overrides)
    return form


def test_config_form_valid() -> None:
    parsed = parse_config_form(config_form())
    assert parsed.errors == {}
    assert parsed.phrases == [("Technical Program Manager", True, True), ("TPM", False, True)]


def test_config_form_blank_and_removed_rows_are_dropped() -> None:
    parsed = parse_config_form(
        config_form(phrase_count="4", phrase_2="", phrase_3="Delivery Manager", remove_3="1")
    )
    assert [p[0] for p in parsed.phrases] == ["Technical Program Manager", "TPM"]


def test_config_form_trims_phrase_whitespace() -> None:
    parsed = parse_config_form(config_form(phrase_0="  Delivery   Manager "))
    assert parsed.phrases[0][0] == "Delivery Manager"


def test_config_form_duplicate_phrase() -> None:
    parsed = parse_config_form(config_form(phrase_1="technical program manager"))
    assert "Duplicate" in parsed.errors["phrases"]


def test_config_form_needs_a_keyword_and_a_match_phrase() -> None:
    no_keyword = parse_config_form(config_form(keyword_0=""))
    assert "keyword" in no_keyword.errors["phrases"]
    no_match = parse_config_form(config_form(match_0="", match_1=""))
    assert "matching" in no_match.errors["phrases"]


def test_config_form_long_phrase() -> None:
    assert "phrases" in parse_config_form(config_form(phrase_0="x" * 101)).errors


def test_config_form_reports_setting_errors_by_field() -> None:
    parsed = parse_config_form(config_form(max_pages_per_keyword="0", match_mode="fuzzy"))
    assert {"max_pages_per_keyword", "match_mode"} <= set(parsed.errors)


def test_config_form_bad_phrase_count_is_treated_as_zero() -> None:
    parsed = parse_config_form(config_form(phrase_count="abc"))
    assert parsed.phrases == []
    assert "phrases" in parsed.errors


def test_default_phrases_constant_is_used_by_seed() -> None:
    assert len(DEFAULT_PHRASES) == 13


# ---- safe_next ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "https://evil.com",
        "//evil.com",
        "/\\evil.com",
        "evil.com",
        "javascript:alert(1)",
        "/ok\nSet-Cookie: x=1",
    ],
)
def test_unsafe_next_falls_back(value: str | None) -> None:
    assert safe_next(value, "/roles") == "/roles"


@pytest.mark.parametrize("value", ["/roles", "/roles?q=tpm&page=2", "/runs?run=3"])
def test_safe_next_keeps_local_paths(value: str) -> None:
    assert safe_next(value, "/x") == value


# ---- csv ---------------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["=1+1", "+1", "-1", "@SUM(A1)", "\tcmd", "\rcmd"])
def test_csv_cell_neutralises_formulas(value: str) -> None:
    assert csv_cell(value) == "'" + value


@pytest.mark.parametrize("value", ["plain", "", None, 12, "a=b"])
def test_csv_cell_leaves_safe_values(value: object) -> None:
    assert csv_cell(value) == ("" if value is None else str(value))


def make_role(**kw: object) -> Role:
    fields: dict[str, object] = {
        "portal": "LinkedIn", "job_id": "1", "title": "TPM", "company": "Acme",
        "location": "Pune", "link": "https://www.linkedin.com/jobs/view/1",
        "posted_on": "2026-10-14T11:00:00+05:30", "posted_precision": "hour",
        "fetched_on": "2026-10-15T09:00:00+05:30", "actively_hiring": True,
        "matched_phrase": "TPM", "possible_duplicate_of": None, "status": "Applied",
        "applied_on": "2026-10-15T00:00:00+05:30", "contact_person": "", "comments": "",
        "raw_datetime": None, "raw_relative": None, "first_run_id": 1,
    }  # fmt: skip
    fields.update(kw)
    return Role(**fields)  # type: ignore[arg-type]


def test_roles_to_csv_layout() -> None:
    lines = roles_to_csv([make_role()]).strip().split("\r\n")
    assert lines[0].startswith("Job ID,Title,Company")
    assert lines[1].split(",")[:2] == ["1", "TPM"]
    assert "2026-10-14 11:00" in lines[1]
    assert "2026-10-15," in lines[1] + ","


def test_roles_to_csv_date_only_precision_and_injection() -> None:
    role = make_role(posted_precision="week", title="=HYPERLINK(1)", comments='@x, "q"')
    text = roles_to_csv([role])
    assert "2026-10-14," in text
    assert "2026-10-14 11:00" not in text
    assert "'=HYPERLINK(1)" in text
    assert '"\'@x, ""q"""' in text

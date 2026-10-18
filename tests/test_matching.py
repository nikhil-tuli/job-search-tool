from __future__ import annotations

import pytest

from jobscan.matching import (
    TitleMatcher,
    dup_key,
    normalize_city,
    normalize_company,
    normalize_text,
)
from jobscan.models import DEFAULT_PHRASES


@pytest.fixture
def matcher() -> TitleMatcher:
    return TitleMatcher(DEFAULT_PHRASES)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Sr. Technical Project Manager", "senior technical project manager"),
        ("Sr Principal Program Manager", "senior principal program manager"),
        ("  Technical   Program\tManager  ", "technical program manager"),
        ("Lead, Data & Analytics", "lead data analytics"),
        ("TPM-II", "tpm ii"),
        ("Sr-Program-Manager", "senior program manager"),
        ("SENIOR PROGRAM MANAGER", "senior program manager"),
        (chr(0xFF34) + "echnical Program Manager", "technical program manager"),  # fullwidth T
        ("", ""),
    ],
)
def test_normalize_text(raw: str, expected: str) -> None:
    assert normalize_text(raw) == expected


def test_sr_inside_a_word_is_not_expanded() -> None:
    assert normalize_text("Disrupt Program Manager") == "disrupt program manager"
    assert normalize_text("Sra Manager") == "sra manager"


@pytest.mark.parametrize(
    ("title", "phrase"),
    [
        ("Technical Program Manager, Google TV", "Technical Program Manager"),
        ("Senior Technical Program Manager, Data", "Senior Technical Program Manager"),
        ("Sr. Technical Program Manager", "Senior Technical Program Manager"),
        ("Staff Technical Program Manager", "Staff Technical Program Manager"),
        ("Staff Program Manager II", "Staff Program Manager"),
        ("Senior Program Manager (Platform)", "Senior Program Manager"),
        ("Sr Program Manager - Payments", "Senior Program Manager"),
        ("Delivery Manager", "Delivery Manager"),
        ("Agile Delivery Manager - Healthcare", "Delivery Manager"),
        ("TPM", "TPM"),
        ("Senior TPM, Infra", "TPM"),
        ("tpm - platform", "TPM"),
        ("Sr. Technical Project Manager", "Technical Project Manager"),
        ("Senior Technical Project Manager", "Technical Project Manager"),
        ("technical project manager", "Technical Project Manager"),
        ("Senior Project Manager - Platform", "Senior Project Manager"),
        ("Sr. Project Manager", "Senior Project Manager"),
        ("IT Project Manager", "IT Project Manager"),
        ("Software Program Manager II", "Software Program Manager"),
        ("Technical Project/Program Manager", "Technical Project/Program Manager"),
        ("Technical Program Management, Cloud", "Technical Program Management"),
    ],
)
def test_titles_that_are_kept(matcher: TitleMatcher, title: str, phrase: str) -> None:
    assert matcher.match(title) == phrase


@pytest.mark.parametrize(
    "title",
    [
        "Program Manager",
        "Principal Program Manager, Ship With Amazon",
        "Sr Principal Program Manager",
        "Project Manager",
        "Engineering Manager",
        "Senior Engineering Manager",
        "Sr Staff AI Architect " + chr(0x2013) + " Strategic Programs",  # en dash
        "Digital Product Manager",
        "Third Party Management Lead",
        "TPMS Analyst",
        "Delivery Lead",
        "",
        "   ",
    ],
)
def test_titles_that_are_dropped(matcher: TitleMatcher, title: str) -> None:
    assert matcher.match(title) is None


def test_phrase_must_match_whole_words(matcher: TitleMatcher) -> None:
    assert matcher.match("Subdelivery Manager") is None
    assert matcher.match("Delivery Managers") is None


def test_longest_phrase_is_reported() -> None:
    m = TitleMatcher(["Program Manager", "Technical Program Manager"])
    assert m.match("Senior Technical Program Manager") == "Technical Program Manager"


def test_exact_mode_requires_whole_title() -> None:
    m = TitleMatcher(["Technical Program Manager"], mode="exact")
    assert m.match("Technical Program Manager") == "Technical Program Manager"
    assert m.match("  technical   program manager ") == "Technical Program Manager"
    assert m.match("Senior Technical Program Manager") is None
    assert m.match("Technical Program Manager, Data") is None


def test_unknown_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="mode"):
        TitleMatcher(["TPM"], mode="fuzzy")


def test_blank_phrases_are_ignored() -> None:
    m = TitleMatcher(["", "  ", "TPM"])
    assert m.match("TPM") == "TPM"
    assert m.match("Anything else") is None


def test_no_phrases_matches_nothing() -> None:
    assert TitleMatcher([]).match("Technical Program Manager") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Acme Pvt. Ltd.", "acme"),
        ("Acme Private Limited", "acme"),
        ("Acme Inc", "acme"),
        ("Orange Business", "orange business"),
        ("Limited", "limited"),
        ("Tata Consultancy Services Limited", "tata consultancy services"),
        ("", ""),
    ],
)
def test_normalize_company(raw: str, expected: str) -> None:
    assert normalize_company(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Bengaluru, Karnataka, India", "bengaluru"),
        ("Bangalore, Karnataka, India", "bengaluru"),
        ("Gurgaon, Haryana, India", "gurugram"),
        ("Bombay", "mumbai"),
        ("Pune District, Maharashtra, India", "pune district"),
        ("India", "india"),
        ("", ""),
    ],
)
def test_normalize_city(raw: str, expected: str) -> None:
    assert normalize_city(raw) == expected


def test_dup_key_matches_reposts_with_cosmetic_differences() -> None:
    first = dup_key("Acme Pvt Ltd", "Sr. Technical Program Manager", "Bangalore, Karnataka, India")
    second = dup_key("ACME", "Senior Technical Program Manager!", "Bengaluru, India")
    assert first is not None
    assert first == second


def test_dup_key_differs_for_different_city_or_title() -> None:
    base = dup_key("Acme", "Technical Program Manager", "Pune, India")
    assert base != dup_key("Acme", "Technical Program Manager", "Mumbai, India")
    assert base != dup_key("Acme", "Technical Program Manager II", "Pune, India")
    assert base != dup_key("Other", "Technical Program Manager", "Pune, India")


@pytest.mark.parametrize(
    ("company", "title", "location"),
    [("", "TPM", "Pune"), ("Acme", "", "Pune"), ("Acme", "TPM", "")],
)
def test_dup_key_is_none_when_a_part_is_missing(company: str, title: str, location: str) -> None:
    assert dup_key(company, title, location) is None

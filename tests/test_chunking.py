from __future__ import annotations

import pytest

from jobscan.chunking import (
    KIND_COMPANY,
    KIND_OTHER,
    KIND_REQUIREMENT,
    KIND_RESPONSIBILITY,
    MAX_WORDS,
    chunk_cv,
    chunk_description,
)

DESCRIPTION = """\
Technical Project Manager - Media Tech

About the Role

We are looking for an execution-focused Technical Project Manager to deliver media products.

Key Responsibilities

- End-to-End Project Management: Own the project lifecycle from planning through delivery.
- Stakeholder Management: Maintain alignment on priorities, scope and timelines with leadership.
- Risk Management: Identify delivery risks early and drive their resolution proactively.

Required Qualifications

- 4-7 years of relevant experience in technical project management and software delivery.
- Strong understanding of SDLC, APIs and cloud infrastructure for large platforms.

About RunnTV

RunnTV is India's first independent FAST channel network.

How to Apply

Apply now
"""


def kinds(description: str, target: int = 40) -> list[str]:
    return [c.kind for c in chunk_description(description, target)]


def test_sections_are_recognised_from_their_headings() -> None:
    chunks = chunk_description(DESCRIPTION, target_words=1)
    by_kind: dict[str, list[str]] = {}
    for chunk in chunks:
        by_kind.setdefault(chunk.kind, []).append(chunk.text)
    assert any("execution-focused" in t for t in by_kind[KIND_RESPONSIBILITY])  # About the Role
    assert (
        sum("Own the project" in t or "Stakeholder" in t for t in by_kind[KIND_RESPONSIBILITY]) == 2
    )
    assert any("4-7 years" in t for t in by_kind[KIND_REQUIREMENT])
    assert any("RunnTV is India" in t for t in by_kind[KIND_COMPANY])
    assert KIND_OTHER in by_kind  # the title line before any heading


def test_boilerplate_after_how_to_apply_is_company_or_dropped() -> None:
    chunks = chunk_description(DESCRIPTION, target_words=1)
    assert not any("Apply now" in c.text for c in chunks)


@pytest.mark.parametrize(
    ("heading", "kind"),
    [
        ("Key Responsibilities", KIND_RESPONSIBILITY),
        ("KEY RESPONSIBILITIES:", KIND_RESPONSIBILITY),
        ("WHAT YOU WILL DO", KIND_RESPONSIBILITY),
        ("What You\u2019ll Own", KIND_RESPONSIBILITY),
        ("Required Qualifications & Experience", KIND_REQUIREMENT),
        ("Preferred qualifications:", KIND_REQUIREMENT),
        ("What We're Looking For", KIND_REQUIREMENT),
        ("WHO YOU ARE", KIND_REQUIREMENT),
        ("Nice To Have", KIND_REQUIREMENT),
        ("Mandatory Skills:", KIND_REQUIREMENT),
        ("ABOUT SVECTOR", KIND_COMPANY),
        ("About the Company", KIND_COMPANY),
        ("Benefits", KIND_COMPANY),
        ("What We Offer", KIND_COMPANY),
        ("Why SVECTOR", KIND_COMPANY),
        ("Who We Are", KIND_COMPANY),
    ],
)
def test_heading_vocabulary(heading: str, kind: str) -> None:
    chunks = chunk_description(f"{heading}\n\nA sentence that has more than six words in it.")
    assert [c.kind for c in chunks] == [kind]


@pytest.mark.parametrize(
    "line", ["About you and the role", "Technical Excellence", "Role: Cloud PM"]
)
def test_other_short_lines_are_not_headings(line: str) -> None:
    chunks = chunk_description(
        f"Key Responsibilities\n\n{line}\nDeliver the programme on time every quarter."
    )
    assert {c.kind for c in chunks} == {KIND_RESPONSIBILITY}


def test_about_the_role_is_not_company_text() -> None:
    (chunk,) = chunk_description(
        "About the Role\n\nYou will lead delivery for our payments platform."
    )
    assert chunk.kind == KIND_RESPONSIBILITY


def test_no_headings_means_one_kind_for_everything() -> None:
    text = "Lead delivery of platform programs across teams.\n\nWork with engineering and product."
    assert set(kinds(text, target=1)) == {KIND_OTHER}


def test_neighbouring_bullets_are_packed_up_to_the_target() -> None:
    bullets = "\n".join(f"- bullet {i} has exactly seven words in it" for i in range(6))
    text = f"Responsibilities\n\n{bullets}"
    assert len(chunk_description(text, target_words=1)) == 6
    packed = chunk_description(text, target_words=20)
    assert [len(c.text.split()) for c in packed] == [16, 16, 16]


def test_packing_never_crosses_a_section() -> None:
    text = (
        "Responsibilities\n\n- Own the roadmap and delivery plan for the payments team.\n\n"
        "Qualifications\n\n- Ten years of experience running large programmes at scale."
    )
    chunks = chunk_description(text, target_words=100)
    assert [c.kind for c in chunks] == [KIND_RESPONSIBILITY, KIND_REQUIREMENT]


def test_short_fragments_join_a_neighbour() -> None:
    text = (
        "Requirements\n\n- Agile\n"
        "- Strong stakeholder management and communication across functions"
    )
    (chunk,) = chunk_description(text, target_words=1)
    assert chunk.text.startswith("Agile Strong stakeholder")


def test_a_long_paragraph_is_cut_at_sentence_ends() -> None:
    sentence = "This sentence has exactly eight words in it."
    text = "Responsibilities\n\n" + " ".join([sentence] * 30)
    chunks = chunk_description(text)
    assert len(chunks) > 1
    assert all(len(c.text.split()) <= MAX_WORDS for c in chunks)
    assert all(c.text.endswith(".") for c in chunks)


def test_one_huge_sentence_is_cut_by_words() -> None:
    text = "Responsibilities\n\n" + " ".join(["word"] * 250)
    sizes = [len(c.text.split()) for c in chunk_description(text)]
    assert sizes == [100, 100, 50]


def test_metadata_and_page_furniture_lines_are_dropped() -> None:
    text = (
        "Responsibilities\n\nLocation: Pune, India\nCTC: 12 LPA\nApply now\n"
        "- Deliver the programme with engineering across three time zones."
    )
    (chunk,) = chunk_description(text)
    assert chunk.text.startswith("Deliver the programme")


def test_numbered_headings_and_list_markers() -> None:
    text = (
        "1. Project Planning\n2. Execution\n"
        "- \u2022 Plan the quarter with every stakeholder involved."
    )
    assert chunk_description(text)  # does not raise; bullets with stacked markers are cleaned


def test_empty_description_has_no_chunks() -> None:
    assert chunk_description("") == []
    assert chunk_description("\n\n  \n") == []


# ---- CV ----------------------------------------------------------------------------------------

CV = """\
Nikhil Tuli
+91-9845642255 | someone@example.com | https://www.linkedin.com/in/someone/
\\
Technical Program Manager with 10+ years of experience driving large-scale programs across
fintech and enterprise platforms with measurable business outcomes

                                        Core Competencies

Program Delivery \u2022 Release Management \u2022 Vendor Management \u2022 JIRA \u2022 SQL

PROFESSIONAL EXPERIENCE

Tesco, Bengaluru                                                          Feb\u201926-Aug\u201926
Technical Program Manager
\u2b25    Payment Gateway Migration: Supported migration to Worldpay across multiple countries,
     coordinating with engineering, product and program teams
\u2b25    Cloud Cost Optimization: Drove a cost program delivering ~20% savings
AngelOne, Bengaluru                                                       Nov\u201924-Nov\u201925
Staff Technical Program Manager (Lending)
\u2b25    Integrated 3 new lenders across 20+ stakeholders

EDUCATION

MBA, Information Systems, DMS IIT Delhi, 2012 - 2014
"""


def test_cv_bullets_carry_their_employer_and_title() -> None:
    chunks = chunk_cv(CV)
    tesco = [c for c in chunks if c.startswith("Tesco, Bengaluru | Technical Program Manager.")]
    assert len(tesco) == 2
    assert "coordinating with engineering" in tesco[0]  # the wrapped line stayed with its bullet
    assert any(
        c.startswith("AngelOne, Bengaluru | Staff Technical Program Manager (Lending).")
        for c in chunks
    )


def test_cv_dates_are_not_part_of_the_context() -> None:
    assert not any("Feb" in c for c in chunk_cv(CV))


def test_cv_contact_details_and_name_are_never_chunked() -> None:
    text = " ".join(chunk_cv(CV))
    for private in ("9845642255", "example.com", "linkedin.com", "Nikhil"):
        assert private not in text


def test_cv_summary_and_skills_are_paragraph_chunks() -> None:
    chunks = chunk_cv(CV)
    assert chunks[0].startswith("Technical Program Manager with 10+ years")
    assert chunks[0].endswith("business outcomes")  # the short last line stays in the paragraph
    assert chunks[1].startswith("Program Delivery")


def test_cv_education_without_bullets_is_kept() -> None:
    assert any("DMS IIT Delhi" in c for c in chunk_cv(CV))


def test_cv_section_titles_are_not_chunks() -> None:
    text = " ".join(chunk_cv(CV))
    assert "Core Competencies" not in text
    assert "PROFESSIONAL EXPERIENCE" not in text


def test_cv_without_indentation_still_chunks_by_bullet() -> None:
    text = (
        "Acme, Pune\nProgram Manager\n"
        "- Led the migration of billing systems for 3 regions\n"
        "- Cut costs by 20% across vendors"
    )
    chunks = chunk_cv(text)
    assert len(chunks) == 2
    assert all(c.startswith("Acme, Pune | Program Manager.") for c in chunks)


def test_cv_long_bullet_is_split() -> None:
    bullet = "- " + " ".join(["Delivered the programme on schedule."] * 40)
    chunks = chunk_cv(f"Acme\nManager\n{bullet}")
    assert all(len(c.split()) <= MAX_WORDS + 6 for c in chunks)
    assert len(chunks) > 1


def test_empty_cv() -> None:
    assert chunk_cv("") == []

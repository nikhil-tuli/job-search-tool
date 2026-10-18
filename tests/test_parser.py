from __future__ import annotations

from jobscan.parser import has_job_urns, parse_page
from tests.helpers import FakeCard, card_html, page_html, real_card_html, stub_html


def test_real_card_all_fields() -> None:
    page = parse_page(real_card_html())
    assert page.malformed == 0
    assert len(page.cards) == 1
    card = page.cards[0]
    assert card.job_id == "4478445031"
    assert card.title == "Sr. Technical Project Manager"
    assert card.company == "Orange Business"
    assert card.location == "Gurgaon, Haryana, India"
    assert card.link == "https://www.linkedin.com/jobs/view/4478445031"
    assert card.actively_hiring is True
    assert card.raw_datetime == "2026-10-14"
    assert card.raw_relative == "22 hours ago"


def test_link_is_clean_without_tracking_parameters() -> None:
    link = parse_page(real_card_html()).cards[0].link
    assert "?" not in link
    assert "refId" not in link


def test_multiple_cards_keep_order() -> None:
    html = page_html(
        [FakeCard("101", "Technical Program Manager"), FakeCard("102", "Delivery Manager")]
    )
    assert [c.job_id for c in parse_page(html).cards] == ["101", "102"]


def test_html_entities_are_decoded() -> None:
    html = card_html(FakeCard("201", "Lead, Data & Analytics", company="AT&T"))
    card = parse_page(html).cards[0]
    assert card.title == "Lead, Data & Analytics"
    assert card.company == "AT&T"


def test_markup_in_title_is_treated_as_text() -> None:
    html = card_html(FakeCard("202", "<script>alert(1)</script> Program Manager"))
    card = parse_page(html).cards[0]
    assert card.title == "<script>alert(1)</script> Program Manager"


def test_not_actively_hiring_by_default() -> None:
    assert parse_page(card_html(FakeCard("301", "TPM"))).cards[0].actively_hiring is False


def test_actively_hiring_flag() -> None:
    assert parse_page(card_html(FakeCard("302", "TPM", hiring=True))).cards[0].actively_hiring


def test_missing_time_tag_gives_none_fields() -> None:
    html = card_html(FakeCard("401", "TPM", relative="", datetime_attr=None))
    card = parse_page(html).cards[0]
    assert card.raw_datetime is None
    assert card.raw_relative is None


def test_time_without_datetime_attribute() -> None:
    html = card_html(FakeCard("402", "TPM", datetime_attr=None))
    card = parse_page(html).cards[0]
    assert card.raw_datetime is None
    assert card.raw_relative == "5 hours ago"


def test_missing_company_and_location_become_empty_strings() -> None:
    html = (
        card_html(FakeCard("501", "TPM"))
        .replace("Acme Corp", "")
        .replace("Bengaluru, Karnataka, India", "")
    )
    card = parse_page(html).cards[0]
    assert card.company == ""
    assert card.location == ""


def test_container_without_job_id_is_counted_as_malformed() -> None:
    html = card_html(FakeCard("601", "TPM")).replace("urn:li:jobPosting:601", "urn:li:other:x")
    page = parse_page(html)
    assert page.cards == []
    assert page.malformed == 1


def test_container_without_title_is_counted_as_malformed() -> None:
    html = card_html(FakeCard("602", "TPM")).replace("TPM", "")
    page = parse_page(html)
    assert page.cards == []
    assert page.malformed == 1


def test_empty_and_stub_pages_have_no_cards() -> None:
    assert parse_page("").cards == []
    assert parse_page(stub_html()).cards == []


def test_has_job_urns() -> None:
    assert has_job_urns(card_html(FakeCard("701", "TPM")))
    assert not has_job_urns(stub_html())
    assert not has_job_urns("")

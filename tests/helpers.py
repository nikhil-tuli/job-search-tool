"""Builders for realistic LinkedIn guest-search HTML used across tests."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from html import escape
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"

# The throttle stub seen on 15 Oct 2026 was 24,633 bytes: a meta refresh to linkedin.com plus
# obfuscated JavaScript. Only its first lines were captured, so the rest is padding.
STUB_SIZE = 24_633


@dataclass(frozen=True)
class FakeCard:
    job_id: str
    title: str
    company: str = "Acme Corp"
    location: str = "Bengaluru, Karnataka, India"
    relative: str = "5 hours ago"
    datetime_attr: str | None = "2026-10-15"
    hiring: bool = False


def card_html(card: FakeCard) -> str:
    """One <li> card in the structure captured from the real guest endpoint."""
    time_tag = ""
    if card.datetime_attr is not None or card.relative:
        attr = f' datetime="{card.datetime_attr}"' if card.datetime_attr is not None else ""
        time_tag = (
            f'<time class="job-search-card__listdate--new"{attr}>\n  {card.relative}\n</time>'
        )
    hiring = (
        '<div class="job-posting-benefits text-sm"><span class="job-posting-benefits__text">\n'
        "Actively Hiring\n</span></div>"
        if card.hiring
        else ""
    )
    return f"""<li>
<div class="base-card relative w-full base-search-card base-search-card--link job-search-card"
 data-entity-urn="urn:li:jobPosting:{card.job_id}">
<a class="base-card__full-link" href="https://in.linkedin.com/jobs/view/x-{card.job_id}?refId=abc">
<span class="sr-only">{escape(card.title)}</span></a>
<div class="base-search-card__info">
<h3 class="base-search-card__title">
  {escape(card.title)}
</h3>
<h4 class="base-search-card__subtitle">
<a class="hidden-nested-link" href="https://fr.linkedin.com/company/x">
  {escape(card.company)}
</a>
</h4>
<div class="base-search-card__metadata">
<span class="job-search-card__location">
  {escape(card.location)}
</span>
{hiring}
{time_tag}
</div></div></div></li>"""


def page_html(cards: Iterable[FakeCard]) -> str:
    """A full guest-search response body made of cards."""
    return "\n".join(card_html(c) for c in cards)


def stub_html() -> str:
    head = (
        '<html><head>\n    <meta http-equiv="refresh" content="1;url=https://www.linkedin.com" />\n'
        '    <script type="text/javascript">\nvar _0x26bb58=_0x3a5d;function _0x2511(){'
    )
    padding_needed = STUB_SIZE - len(head.encode()) - len("</script></head></html>")
    return head + ("x" * padding_needed) + "</script></head></html>"


def real_card_html() -> str:
    return (FIXTURES / "card_real_orange.html").read_text(encoding="utf-8")

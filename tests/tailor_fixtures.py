"""A small Word CV built in code, shaped like a real one (bold lead-ins, bullets, dates)."""

from __future__ import annotations

import io

from docx import Document

SUMMARY = (
    "Technical program manager with ten years of delivery experience across payments and "
    "platform teams, known for clear plans and calm execution."
)
COMPETENCIES = ["Program management", "Risk management", "Stakeholder management", "Agile"]
BULLETS = [
    ("Payments launch", ": Led a launch across 6 teams and cut release time by 30% in a year."),
    ("Platform", ": Ran quarterly planning for 4 squads and kept scope changes visible."),
    ("Reliability", ": Set up incident reviews that reduced repeat outages across services."),
]


def make_cv() -> bytes:
    doc = Document()
    doc.add_paragraph("Asha Rao")
    doc.add_paragraph("asha@example.com | +91 90000 00000")
    doc.add_paragraph(SUMMARY)
    doc.add_paragraph("CORE COMPETENCIES")
    doc.add_paragraph(" • ".join(COMPETENCIES))
    doc.add_paragraph("PROFESSIONAL EXPERIENCE")
    doc.add_paragraph("Acme Pay, Bengaluru\tJan 2020 - Present")
    for label, text in BULLETS:
        paragraph = doc.add_paragraph(style="List Bullet")
        paragraph.add_run(label).bold = True
        paragraph.add_run(text)
    doc.add_paragraph("EDUCATION")
    doc.add_paragraph("B.Tech, 2012")
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()

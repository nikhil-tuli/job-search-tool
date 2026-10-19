from __future__ import annotations

import io
import zipfile

import pytest

from jobscan import cv
from jobscan.cv import CvError, extract_text, sha_of

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def docx_bytes(paragraphs: list[tuple[str, bool]], *, name: str = "word/document.xml") -> bytes:
    body = "".join(
        "<w:p>"
        + ("<w:pPr><w:numPr><w:ilvl w:val='0'/></w:numPr></w:pPr>" if bullet else "")
        + f"<w:r><w:t>{text}</w:t></w:r></w:p>"
        for text, bullet in paragraphs
    )
    xml = f"<?xml version='1.0'?><w:document xmlns:w='{W}'><w:body>{body}</w:body></w:document>"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, xml)
    return buffer.getvalue()


def pdf_bytes(lines: list[str]) -> bytes:
    """A one-page PDF with real text, built by hand so no PDF library is needed to test."""
    stream = "BT /F1 12 Tf 14 TL 50 700 Td " + " T* ".join(f"({line}) Tj" for line in lines) + " ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out


def test_pdf_text_is_extracted() -> None:
    text = extract_text("cv.pdf", pdf_bytes(["Program Manager at Acme", "Led payments migration"]))
    assert "Program Manager at Acme" in text
    assert "Led payments migration" in text


def test_docx_text_keeps_paragraphs_and_marks_list_items() -> None:
    data = docx_bytes([("Acme, Pune", False), ("Led the migration", True)])
    assert extract_text("cv.docx", data) == "Acme, Pune\n• Led the migration"


def test_plain_text_and_markdown() -> None:
    assert extract_text("cv.txt", b"\xef\xbb\xbfHello   world") == "Hello world"
    assert extract_text("cv.MD", b"- item one is here") == "- item one is here"


def test_indentation_is_kept_and_capped_and_spaces_collapsed() -> None:
    text = extract_text("cv.txt", b"- first   bullet\n     wrapped line\n" + b" " * 90 + b"centred")
    lines = text.splitlines()
    assert lines[0] == "- first bullet"
    assert lines[1] == "     wrapped line"
    assert lines[2] == " " * 40 + "centred"


def test_blank_runs_collapse() -> None:
    assert extract_text("cv.txt", b"a b\n\n\n\n\nc d") == "a b\n\nc d"


@pytest.mark.parametrize(
    ("name", "data", "message"),
    [
        ("cv.pdf", b"", "empty"),
        ("cv.pdf", b"not a pdf at all", "could not be read"),
        ("cv.docx", b"not a zip", "could not be read"),
        ("cv.docx", docx_bytes([("x", False)], name="other.xml"), "could not be read"),
        ("cv.txt", b"\xff\xfe\x00bad", "UTF-8"),
        ("cv.exe", b"hello", "Upload a"),
        ("cv.txt", b"1234 5678 !!!", "No text"),
        ("cv.pdf", pdf_bytes([""]), "No text"),
    ],
)
def test_unusable_files_say_why(name: str, data: bytes, message: str) -> None:
    with pytest.raises(CvError, match=message):
        extract_text(name, data)


def test_oversized_upload_is_refused() -> None:
    with pytest.raises(CvError, match="larger than"):
        extract_text("cv.txt", b"x" * (cv.MAX_UPLOAD_BYTES + 1))


def test_oversized_word_xml_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cv, "_MAX_XML_BYTES", 10)
    with pytest.raises(CvError, match="too large"):
        extract_text("cv.docx", docx_bytes([("some text", False)]))


def test_text_is_capped() -> None:
    text = extract_text("cv.txt", ("word " * 50_000).encode())
    assert len(text) == cv.MAX_TEXT_CHARS


def test_sha_names_the_exact_text() -> None:
    assert sha_of("a") == sha_of("a")
    assert sha_of("a") != sha_of("b")

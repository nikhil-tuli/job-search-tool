"""Turn an uploaded CV file into clean text. Everything runs locally."""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from xml.etree import ElementTree

from pypdf import PdfReader

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_TEXT_CHARS = 100_000
MAX_PDF_PAGES = 20
_MAX_XML_BYTES = 20 * 1024 * 1024
_MAX_INDENT = 40
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_SUFFIXES = (".pdf", ".docx", ".txt", ".md")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class CvError(ValueError):
    """The file cannot be used as a CV; the message says why, in plain words."""


def sha_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract_text(filename: str, data: bytes) -> str:
    """Text of a PDF, DOCX or plain text file, with line breaks and indentation kept."""
    if not data:
        raise CvError("The file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise CvError(f"The file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    name = filename.lower()
    if name.endswith(".pdf"):
        raw = _pdf_text(data)
    elif name.endswith(".docx"):
        raw = _docx_text(data)
    elif name.endswith((".txt", ".md")):
        try:
            raw = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise CvError("The text file is not UTF-8.") from None
    else:
        raise CvError(f"Upload a {', '.join(_SUFFIXES[:-1])} or {_SUFFIXES[-1]} file.")
    text = _tidy(raw)
    if not any(c.isalpha() for c in text):
        raise CvError(
            "No text could be read. A scanned PDF has no text layer; export the CV as a PDF "
            "from Word or Google Docs instead."
        )
    return text[:MAX_TEXT_CHARS]


def _tidy(raw: str) -> str:
    """One space between words; leading indentation kept, because it shows wrapped bullets."""
    lines = []
    for line in _CONTROL.sub(" ", raw).splitlines():
        words = " ".join(line.split())
        indent = min(len(line) - len(line.lstrip(" ")), _MAX_INDENT) if words else 0
        lines.append(" " * indent + words)
    text = "\n".join(lines).strip("\n")
    return re.sub(r"\n{3,}", "\n\n", text)


def _pdf_text(data: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise CvError("The PDF is password protected.")
        pages = reader.pages[:MAX_PDF_PAGES]
        # Layout mode keeps lines whole; the default mode can return one word per line.
        return "\n".join(page.extract_text(extraction_mode="layout") for page in pages)
    except CvError:
        raise
    except Exception:  # pypdf raises assorted errors on malformed files
        raise CvError("The PDF could not be read.") from None


def _docx_text(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > _MAX_XML_BYTES:
                raise CvError("The Word file is too large to read.")
            root = ElementTree.fromstring(archive.read(info))
    except CvError:
        raise
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError):
        raise CvError("The Word file could not be read.") from None
    lines = []
    for paragraph in root.iter(f"{_W}p"):
        parts = []
        for node in paragraph.iter():
            if node.tag == f"{_W}t" and node.text:
                parts.append(node.text)
            elif node.tag == f"{_W}tab":
                parts.append(" ")
            elif node.tag == f"{_W}br":
                parts.append("\n")
        text = "".join(parts).strip()
        if text and paragraph.find(f"{_W}pPr/{_W}numPr") is not None:
            text = "• " + text  # a list paragraph has no bullet character in its text
        lines.append(text)
    return "\n".join(lines)

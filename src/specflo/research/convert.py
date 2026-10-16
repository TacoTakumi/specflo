"""Convert a PDF or a Word document to the text a research source stores.

Both converters have the signature the ingest seam asks for: the file's path
(for its name in messages) and its bytes in, the text and the extra fields of
the format out. A PDF becomes one ``## Page N`` section per page, in page
order, with its page count as the ``Pages`` field. A Word document becomes
markdown with ATX headings, so each Word heading is a markdown heading and the
text under it is its section; it adds no field.

The libraries that do the reading ship as the ``docs`` extra (pypdfium2 for
PDF; mammoth, then markdownify, for DOCX), so a core install does not carry
them. This module imports them inside the converters, when a file of that kind
is converted, never at import time: an install without the extra loads and
runs every other command, and an ingest of a PDF or DOCX is refused with a
message that names ``specflo[docs]``. A file the library cannot read (a
corrupt or mislabelled one) is refused with the library's reason. Every
refusal is raised before the caller writes anything.
"""

from __future__ import annotations

import io
from pathlib import Path

from ..errors import SpecfloError

# The extra that brings the converters, as the install command names it.
DOCS_EXTRA = "specflo[docs]"


def _needs_extra(path: Path, exc: ImportError) -> SpecfloError:
    return SpecfloError(
        f"Cannot ingest {path.name}: PDF and DOCX need the docs extra"
        f" ({exc.name or 'a converter'} is not installed). Install {DOCS_EXTRA}."
    )


def _unreadable(path: Path, what: str, exc: Exception) -> SpecfloError:
    reason = str(exc).strip() or type(exc).__name__
    return SpecfloError(f"Cannot ingest {path.name}: it is not {what} that can be read: {reason}")


def convert_pdf(path: Path, data: bytes) -> tuple[str, list[tuple[str, str]]]:
    """The text of a PDF, one ``## Page N`` section per page, and its ``Pages`` count.

    A page without text keeps its heading, so page numbers stay citable; a
    scan with no text on any page is not refused here.
    """
    try:
        import pypdfium2
    except ImportError as exc:
        raise _needs_extra(path, exc) from exc
    sections: list[str] = []
    try:
        with pypdfium2.PdfDocument(data) as document:
            count = len(document)
            for index in range(count):
                page_text = document[index].get_textpage().get_text_range().strip()
                heading = f"## Page {index + 1}"
                sections.append(f"{heading}\n\n{page_text}" if page_text else heading)
    except Exception as exc:  # PdfiumError for a file it cannot load; a damaged page raises others
        raise _unreadable(path, "a PDF", exc) from exc
    return "\n\n".join(sections) + "\n", [("Pages", str(count))]


def convert_docx(path: Path, data: bytes) -> tuple[str, list[tuple[str, str]]]:
    """The text of a Word document as markdown with ATX headings; no extra field."""
    try:
        import mammoth
        import markdownify
    except ImportError as exc:
        raise _needs_extra(path, exc) from exc
    try:
        html = mammoth.convert_to_html(io.BytesIO(data)).value
    except Exception as exc:  # a broken zip, a zip without the document part, bad XML
        raise _unreadable(path, "a Word document", exc) from exc
    text = markdownify.markdownify(html, heading_style=markdownify.ATX)
    return text.strip("\n") + "\n", []

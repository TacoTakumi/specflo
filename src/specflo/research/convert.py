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
corrupt or mislabelled one) is refused with the library's reason. A PDF with
no text on any page is a scan, and a scan is refused: reading one needs OCR,
which specflo does not do. Every refusal is raised before the caller writes
anything.

:func:`run_with_timeout` runs any converter under a time limit. A library call
that never returns cannot be interrupted from a thread, so the converter runs
in a child process the parent stops when the time is up; the result, or the
refusal the converter raised, comes back over a pipe.
"""

from __future__ import annotations

import io
import multiprocessing
from collections.abc import Callable
from pathlib import Path

from ..errors import SpecfloError

# The extra that brings the converters, as the install command names it.
DOCS_EXTRA = "specflo[docs]"
# The config key the timeout refusal names, as the registry spells it.
TIMEOUT_KEY = "research_convert_timeout"

# A converter takes the file's path (for its name in messages) and its bytes;
# it returns the text to store and the extra fields of its format, in order.
Converter = Callable[[Path, bytes], tuple[str, list[tuple[str, str]]]]


def _needs_extra(path: Path, exc: ImportError) -> SpecfloError:
    return SpecfloError(
        f"Cannot ingest {path.name}: PDF and DOCX need the docs extra"
        f" ({exc.name or 'a converter'} is not installed). Install {DOCS_EXTRA}."
    )


def _unreadable(path: Path, what: str, exc: Exception) -> SpecfloError:
    reason = str(exc).strip() or type(exc).__name__
    return SpecfloError(f"Cannot ingest {path.name}: it is not {what} that can be read: {reason}")


def _scanned(path: Path) -> SpecfloError:
    return SpecfloError(
        f"Cannot ingest {path.name}: no page of it has any text, so it looks like a scanned"
        " document. Scanned documents are not supported yet: reading one needs OCR, which"
        " specflo does not do. Run OCR on it first and ingest the text that gives."
    )


def convert_pdf(path: Path, data: bytes) -> tuple[str, list[tuple[str, str]]]:
    """The text of a PDF, one ``## Page N`` section per page, and its ``Pages`` count.

    A page without text keeps its heading, so page numbers stay citable. A
    PDF with no text on any page is a scan and is refused: without OCR there
    is nothing to store or cite.
    """
    try:
        import pypdfium2
    except ImportError as exc:
        raise _needs_extra(path, exc) from exc
    sections: list[str] = []
    found_text = False
    try:
        with pypdfium2.PdfDocument(data) as document:
            count = len(document)
            for index in range(count):
                page_text = document[index].get_textpage().get_text_range().strip()
                heading = f"## Page {index + 1}"
                sections.append(f"{heading}\n\n{page_text}" if page_text else heading)
                found_text = found_text or bool(page_text)
    except Exception as exc:  # PdfiumError for a file it cannot load; a damaged page raises others
        raise _unreadable(path, "a PDF", exc) from exc
    if count and not found_text:
        raise _scanned(path)
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


# --- the timeout runner ----------------------------------------------------------------


def _context():
    """The multiprocessing context the runner starts the child with.

    fork is preferred: the child inherits the converter and the bytes it takes
    instead of unpickling them, so any callable the seam registers runs. Where
    fork is not offered the platform default stands in, and a converter then
    has to be importable by name.
    """
    if "fork" in multiprocessing.get_all_start_methods():
        return multiprocessing.get_context("fork")
    return multiprocessing.get_context()


def _in_child(connection, converter: Converter, path: Path, data: bytes) -> None:
    """The child's side: run the converter and send what came of it, never raise.

    A refusal keeps its message and is raised again by the parent; any other
    failure is sent as its kind and reason, so the parent can refuse the file
    naming them without a traceback.
    """
    try:
        outcome = ("done", converter(path, data))
    except SpecfloError as exc:
        outcome = ("refused", str(exc))
    except BaseException as exc:  # noqa: BLE001 - the parent reports whatever stopped the converter
        outcome = ("failed", f"{type(exc).__name__}: {exc}")
    try:
        connection.send(outcome)
    finally:
        connection.close()


def _stop(worker) -> None:
    """End the child for good: a terminate, and a kill if that is ignored."""
    worker.terminate()
    worker.join(5)
    if worker.is_alive():
        worker.kill()
        worker.join()


def _seconds(count: int) -> str:
    return f"{count} second" + ("" if count == 1 else "s")


def run_with_timeout(
    converter: Converter, path: Path, data: bytes, timeout: int
) -> tuple[str, list[tuple[str, str]]]:
    """``converter(path, data)`` in a child process that is stopped after ``timeout`` seconds.

    The child sends the converter's result, or the refusal it raised, back
    over a pipe. When the time is up the child is terminated and the file is
    refused naming the timeout and its config key; a child that fails or dies
    without a result is refused naming the file and what stopped it. The
    parent writes nothing either way, so every refusal comes before a mint.
    """
    context = _context()
    receiver, sender = context.Pipe(duplex=False)
    worker = context.Process(
        target=_in_child, args=(sender, converter, path, data), daemon=True
    )
    worker.start()
    sender.close()
    outcome = None
    try:
        finished = receiver.poll(timeout)
        if finished:
            try:
                outcome = receiver.recv()
            except EOFError:  # the child ended without sending
                outcome = None
    finally:
        receiver.close()
    if not finished:
        _stop(worker)
        raise SpecfloError(
            f"Cannot ingest {path.name}: its conversion did not finish within"
            f" {_seconds(timeout)} and was stopped. Raise the limit with"
            f" `specflo config set {TIMEOUT_KEY} <seconds>` or convert the file by hand."
        )
    worker.join()
    if outcome is None:
        raise SpecfloError(
            f"Cannot ingest {path.name}: its converter stopped without a result"
            f" (exit code {worker.exitcode})."
        )
    kind, payload = outcome
    if kind == "done":
        return payload
    if kind == "refused":
        raise SpecfloError(payload)
    raise SpecfloError(f"Cannot ingest {path.name}: its converter failed: {payload}")

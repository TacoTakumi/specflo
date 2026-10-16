"""Ingest a file as a document source.

:func:`ingest_file` reads the file, hashes it, refuses bytes the sources
ledger already holds, converts the file to text by its suffix, and then
stores it: the ledger mints S-NN, the directory of that ID is created, the
original is written byte for byte, the text beside it, and the entry records
where both are. Every refusal comes before the first write.

The converter is the seam for the formats: :data:`CONVERTERS` has one entry
per suffix with the Format value and a converter that returns the text and the
fields its format adds. A markdown or plain-text file's text is its content
decoded as UTF-8. A PDF or Word document goes through :mod:`.convert`, whose
libraries come from the ``docs`` extra and are imported only when such a file
is converted; a PDF adds its page count.
"""

from __future__ import annotations

import datetime
import hashlib
from collections.abc import Callable
from pathlib import Path

from ..config import SpecfloConfig
from ..errors import SpecfloError
from . import sources
from .convert import convert_docx, convert_pdf
from .ledger import Entry, source_dir

# A converter takes the file's path (for its name in messages) and its bytes;
# it returns the text to store and the extra fields of its format, in order.
Converter = Callable[[Path, bytes], tuple[str, list[tuple[str, str]]]]


def _decode_utf8(path: Path, data: bytes) -> tuple[str, list[tuple[str, str]]]:
    """The text of a markdown or plain-text file: its content, which must be UTF-8."""
    try:
        return data.decode("utf-8"), []
    except UnicodeDecodeError as exc:
        raise SpecfloError(
            f"Cannot ingest {path.name}: it is not valid UTF-8 (at byte {exc.start})."
            " A markdown or text file is stored as UTF-8; convert it first."
        ) from exc


# Lowercase suffix -> (Format value, converter); the Format values are those
# of sources.FORMATS. The PDF and Word converters need the docs extra and say
# so when it is missing.
CONVERTERS: dict[str, tuple[str, Converter]] = {
    ".md": ("md", _decode_utf8),
    ".txt": ("txt", _decode_utf8),
    ".pdf": ("pdf", convert_pdf),
    ".docx": ("docx", convert_docx),
}
SUPPORTED_SUFFIXES = tuple(CONVERTERS)


def _suffix(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix not in CONVERTERS:
        what = f"{suffix!r}" if suffix else "a file without a suffix"
        raise SpecfloError(
            f"Cannot ingest {path.name}: {what} is not a supported format."
            f" Supported: {', '.join(SUPPORTED_SUFFIXES)}."
        )
    return suffix


def format_of(path: Path) -> str:
    """The Format value of ``path`` by its suffix; refused for a suffix no converter takes."""
    return CONVERTERS[_suffix(Path(path))][0]


def convert(path: Path, data: bytes) -> tuple[str, list[tuple[str, str]]]:
    """The text of ``data`` and the fields its format adds, by the suffix of ``path``."""
    path = Path(path)
    _, converter = CONVERTERS[_suffix(path)]
    return converter(path, data)


def ingest_file(
    root: Path, cfg: SpecfloConfig, slug: str, path: Path, today: str | None = None
) -> Entry:
    """Store ``path`` as a document source of project ``slug`` and return its entry.

    Refused, with nothing written: a missing file or a directory, bytes a
    source already holds (the message names it), a suffix no converter takes,
    a name the source directory cannot hold, a text file that is not UTF-8, a
    PDF or DOCX without the docs extra installed and a file its converter
    cannot read.
    Then the ledger mints the ID, the source directory is made for it, the
    original and the text are written there and the entry records both paths.
    """
    path = Path(path)
    if not path.is_file():
        if path.exists():
            raise SpecfloError(f"Cannot ingest {path}: it is not a file.")
        raise SpecfloError(f"No file at {path}.")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise SpecfloError(f"Cannot read {path}: {exc}.") from exc
    digest = hashlib.sha256(data).hexdigest()
    held = sources.find_by_sha256(root, cfg, slug, digest)
    if held is not None:
        raise SpecfloError(
            f"Not ingested: {held.id} already holds these bytes, stored as {held.title!r}."
            f" A file is stored once; cite {held.id}."
        )
    fmt = format_of(path)
    name = sources.storage_name(path.name)
    text, extra = convert(path, data)
    today = today or datetime.date.today().isoformat()

    entry = sources.add_source(
        root, cfg, slug, name, sources.DOCUMENT_KIND,
        [
            (sources.DATE_FIELD, today),
            (sources.FORMAT_FIELD, fmt),
            (sources.SHA256_FIELD, digest),
            *extra,
        ],
        today=today,
    )
    # The ID is minted once and never again, so this directory is this
    # ingest's alone; anything already there was never a recorded source.
    directory = source_dir(root, cfg, slug, entry.id)
    original = sources.original_path(root, cfg, slug, entry.id, name)
    text_file = sources.text_path(root, cfg, slug, entry.id)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        original.write_bytes(data)
        text_file.write_bytes(text.encode("utf-8"))
    except OSError as exc:
        raise SpecfloError(
            f"{entry.id} is recorded but its files could not be stored under {directory}: {exc}."
        ) from exc
    return sources.record_stored(root, cfg, slug, entry.id, original, text_file, today=today)

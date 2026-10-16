"""Ingest a file as a document source.

:func:`ingest_file` reads the file, hashes it, refuses bytes the sources
ledger already holds, converts the file to text by its suffix, and then
stores it: the ledger mints S-NN, the directory of that ID is created, the
original is written byte for byte, the text beside it, and the entry records
where both are. Every refusal comes before the first write.

The duplicate check runs twice. Once before the conversion, so a file already
held is refused without converting it; and once more under the research lock,
right before the ID is minted, with the sources held at that moment. The
second check is the one that counts: two ingests of the same bytes at once
both pass the first, and only the lock keeps the second of them from minting
an entry of its own.

The converter is the seam for the formats: :data:`CONVERTERS` has one entry
per suffix with the Format value and a converter that returns the text and the
fields its format adds. A markdown or plain-text file's text is its content
decoded as UTF-8. A PDF or Word document goes through :mod:`.convert`, whose
libraries come from the ``docs`` extra and are imported only when such a file
is converted; a PDF adds its page count.

Two config keys bound an ingest. ``research_source_limit_mb`` caps the file:
one over it is refused from its size on disk, before it is read, whatever
its suffix (the cap is one rule for every format, since every format is read
whole, hashed and stored byte for byte). ``research_convert_timeout`` caps the
conversion: a converter runs through :func:`.convert.run_with_timeout`, in a
child process stopped when the time is up, unless it is marked ``inline``;
the UTF-8 decoder is, since a decode cannot hang and is not worth a process.
"""

from __future__ import annotations

import datetime
import hashlib
from pathlib import Path

from ..config import SpecfloConfig
from ..errors import SpecfloError
from . import sources
from .convert import Converter, convert_docx, convert_pdf, run_with_timeout
from .ledger import Entry, source_dir

# The config key the size refusal names, as the registry spells it.
SIZE_LIMIT_KEY = "research_source_limit_mb"
MB = 1024 * 1024


def _decode_utf8(path: Path, data: bytes) -> tuple[str, list[tuple[str, str]]]:
    """The text of a markdown or plain-text file: its content, which must be UTF-8."""
    try:
        return data.decode("utf-8"), []
    except UnicodeDecodeError as exc:
        raise SpecfloError(
            f"Cannot ingest {path.name}: it is not valid UTF-8 (at byte {exc.start})."
            " A markdown or text file is stored as UTF-8; convert it first."
        ) from exc


# A converter marked ``inline`` runs in this process, timeout or not; every
# other one (a library call that may never return) runs under the runner. The
# decoder is the only one marked: it cannot hang, so a child process for it
# would cost a fork per markdown or text file for nothing.
_decode_utf8.inline = True


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


def convert(
    path: Path, data: bytes, timeout: int | None = None
) -> tuple[str, list[tuple[str, str]]]:
    """The text of ``data`` and the fields its format adds, by the suffix of ``path``.

    With ``timeout`` (seconds) the converter runs in a child process that is
    stopped at that point; without one it runs in this process, unbounded. A
    converter marked ``inline`` runs in this process either way.
    """
    path = Path(path)
    _, converter = CONVERTERS[_suffix(path)]
    if timeout is None or getattr(converter, "inline", False):
        return converter(path, data)
    return run_with_timeout(converter, path, data, timeout)


def _check_size(path: Path, limit_mb: int) -> None:
    """Refuse ``path`` from its size on disk when it is over ``limit_mb`` MB."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise SpecfloError(f"Cannot read {path}: {exc}.") from exc
    if size > limit_mb * MB:
        raise SpecfloError(
            f"Cannot ingest {path.name}: it is {size / MB:.1f} MB, over the {limit_mb} MB limit."
            f" Raise the limit with `specflo config set {SIZE_LIMIT_KEY} <mb>` or split the file."
        )


def _refuse_held(digest: str, entries: list[Entry]) -> None:
    """Refuse the ingest when a source among ``entries`` already holds the bytes of ``digest``."""
    held = sources.holder_of_sha256(entries, digest)
    if held is not None:
        raise SpecfloError(
            f"Not ingested: {held.id} already holds these bytes, stored as {held.title!r}."
            f" A file is stored once; cite {held.id}."
        )


def ingest_file(
    root: Path, cfg: SpecfloConfig, slug: str, path: Path, today: str | None = None
) -> Entry:
    """Store ``path`` as a document source of project ``slug`` and return its entry.

    Refused, with nothing written: a missing file or a directory, a file over
    the size limit (from its size on disk, before it is read), bytes a source
    already holds (the message names it), a suffix no converter takes, a name
    the source directory cannot hold, a text file that is not UTF-8, a PDF or
    DOCX without the docs extra installed, a file its converter cannot read, a
    scanned PDF and a conversion that runs past the timeout.
    Then the ledger mints the ID, the source directory is made for it, the
    original and the text are written there and the entry records both paths.
    """
    path = Path(path)
    if not path.is_file():
        if path.exists():
            raise SpecfloError(f"Cannot ingest {path}: it is not a file.")
        raise SpecfloError(f"No file at {path}.")
    _check_size(path, cfg.research_source_limit_mb)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise SpecfloError(f"Cannot read {path}: {exc}.") from exc
    digest = hashlib.sha256(data).hexdigest()
    # The early check spares a held file its conversion; it is not the one
    # that counts, since another ingest of the same bytes may record its
    # source between this read and the mint below.
    _refuse_held(digest, sources.sources(root, cfg, slug))
    fmt = format_of(path)
    name = sources.storage_name(path.name)
    text, extra = convert(path, data, timeout=cfg.research_convert_timeout)
    today = today or datetime.date.today().isoformat()

    # The check that counts: under the lock that mints the ID, against the
    # sources held at that moment, so of several ingests of the same bytes
    # at once exactly one records a source and the others are refused naming it.
    entry = sources.add_source(
        root, cfg, slug, name, sources.DOCUMENT_KIND,
        [
            (sources.DATE_FIELD, today),
            (sources.FORMAT_FIELD, fmt),
            (sources.SHA256_FIELD, digest),
            *extra,
        ],
        today=today,
        precheck=lambda entries: _refuse_held(digest, entries),
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

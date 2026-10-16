"""The sources ledger as the research commands see it: kinds, fields and stored files.

A source is a document, an interview, a web page or a repository. Its entry
in the sources ledger is ``### S-NN - <title>`` (a document's title is its
file name) with these fields, always in this order:

- ``Kind``: document, interview, web or repo.
- ``Date``: when the source was taken in, ``YYYY-MM-DD``.
- ``Format``: md, txt, pdf or docx; a document only.
- ``Pages``: the page count; a PDF only, written by its converter.
- ``Sha256``: the hex digest of the original bytes; a document only, and
  what keeps the same file from being stored twice.
- ``Url``: where a web page or repository was fetched from; those two only.
- ``Original``: the project-relative path of the stored original.
- ``Text``: the project-relative path of the stored text.

The files of a source live in one directory per source:
``research/sources/S-NN/<original file name>`` holds the original byte for
byte and ``research/sources/S-NN/text.md`` the text the research reads.
Project-relative means relative to the project folder, with forward slashes;
a path that leaves the folder is refused on the way in and on the way out.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from ..config import SpecfloConfig
from ..errors import SpecfloError, require_one_line
from ..projects import load_project, project_dir, validate_slug
from .document import RESEARCH_DIRNAME
from .ledger import SOURCES, SOURCES_DIRNAME, Entry, source_dir

KIND_FIELD = "Kind"
DATE_FIELD = "Date"
FORMAT_FIELD = "Format"
PAGES_FIELD = "Pages"
SHA256_FIELD = "Sha256"
URL_FIELD = "Url"
ORIGINAL_FIELD = "Original"
TEXT_FIELD = "Text"
# The order the fields are written in, whatever order a caller gives them.
FIELD_ORDER = (
    KIND_FIELD, DATE_FIELD, FORMAT_FIELD, PAGES_FIELD, SHA256_FIELD, URL_FIELD,
    ORIGINAL_FIELD, TEXT_FIELD,
)

DOCUMENT_KIND = "document"
INTERVIEW_KIND = "interview"
WEB_KIND = "web"
REPO_KIND = "repo"
KINDS = (DOCUMENT_KIND, INTERVIEW_KIND, WEB_KIND, REPO_KIND)

FORMATS = ("md", "txt", "pdf", "docx")
# The kinds whose text the user gives; a document comes in through ingest.
TEXT_KINDS = (INTERVIEW_KIND, WEB_KIND, REPO_KIND)

# The stored text of every source, beside the original in the source directory.
TEXT_FILENAME = "text.md"

# Printed once, at the first source of a project that pins no egress class.
# A suggestion only: the project file is never written here.
EGRESS_HINT = (
    "Suggestion: pin an egress class with `specflo egress local|no-train|open`."
    " Sources may hold private material; the pin tells agents what may leave this machine."
)

# What each kind may carry beyond Kind and Date, and what it must carry. A
# document's Original and Text are recorded once its files are stored, since
# their paths carry the ID the ledger mints.
_ALLOWED = {
    DOCUMENT_KIND: (FORMAT_FIELD, PAGES_FIELD, SHA256_FIELD, ORIGINAL_FIELD, TEXT_FIELD),
    INTERVIEW_KIND: (ORIGINAL_FIELD, TEXT_FIELD),
    WEB_KIND: (URL_FIELD, ORIGINAL_FIELD, TEXT_FIELD),
    REPO_KIND: (URL_FIELD, ORIGINAL_FIELD, TEXT_FIELD),
}
_REQUIRED = {
    DOCUMENT_KIND: (FORMAT_FIELD, SHA256_FIELD),
    INTERVIEW_KIND: (),
    WEB_KIND: (URL_FIELD,),
    REPO_KIND: (URL_FIELD,),
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCES_PREFIX = f"{RESEARCH_DIRNAME}/{SOURCES_DIRNAME}/"


# --- the stored files ------------------------------------------------------------


def project_folder(root: Path, cfg: SpecfloConfig, slug: str) -> Path:
    """The project folder every stored path is relative to."""
    return project_dir(root, cfg, validate_slug(slug))


def _check_relative(what: str, value: str) -> str:
    """``value`` when it is a project-relative path under ``research/sources/``."""
    posix = PurePosixPath(value or "")
    if (
        not value
        or "\\" in value
        or posix.is_absolute()
        or ".." in posix.parts
        or not value.startswith(_SOURCES_PREFIX)
    ):
        raise SpecfloError(
            f"{what} must be a path relative to the project folder under {_SOURCES_PREFIX},"
            f" like {_SOURCES_PREFIX}S-01/{TEXT_FILENAME}: got {value!r}."
        )
    return value


def project_path(root: Path, cfg: SpecfloConfig, slug: str, relative: str, what: str = "A stored path") -> Path:
    """The file a project-relative field value names; refused when it leaves the project folder."""
    folder = project_folder(root, cfg, slug)
    path = folder / _check_relative(what, relative)
    if not path.resolve().is_relative_to(folder.resolve()):
        raise SpecfloError(f"{what} leaves the project folder: {relative!r}.")
    return path


def project_relative(root: Path, cfg: SpecfloConfig, slug: str, path: Path) -> str:
    """``path`` as the project-relative string the ledger records; refused outside the folder."""
    folder = project_folder(root, cfg, slug)
    try:
        relative = Path(path).resolve().relative_to(folder.resolve()).as_posix()
    except ValueError:
        raise SpecfloError(f"{path} is not inside the project folder {folder}.") from None
    return _check_relative("A stored path", relative)


def storage_name(name: str) -> str:
    """``name`` as an original is stored under; refused when it cannot be stored or recorded.

    The name is the entry's title and part of its Original path, both one
    ledger line, so it may not start or end with whitespace or hold a line
    break; and it may not be the stored text's own name.
    """
    if not name or Path(name).name != name or name in (".", "..") or "\\" in name:
        raise SpecfloError(f"{name!r} is not a file name a source directory can hold.")
    if name != name.strip() or name.splitlines() != [name]:
        raise SpecfloError(
            f"The file name {name!r} cannot be recorded on one ledger line: rename the file"
            " so it has no leading or trailing whitespace and no line break."
        )
    if name == TEXT_FILENAME:
        raise SpecfloError(
            f"A file named {TEXT_FILENAME} cannot be stored as it is: that is the name of"
            " the stored text beside it. Rename the file and ingest it again."
        )
    return name


def original_path(root: Path, cfg: SpecfloConfig, slug: str, source_id: str, name: str) -> Path:
    """``research/sources/S-NN/<name>``: where source ``source_id`` keeps its original."""
    return source_dir(root, cfg, slug, source_id) / storage_name(name)


def text_path(root: Path, cfg: SpecfloConfig, slug: str, source_id: str) -> Path:
    """``research/sources/S-NN/text.md``: where source ``source_id`` keeps its text."""
    return source_dir(root, cfg, slug, source_id) / TEXT_FILENAME


def source_text_path(root: Path, cfg: SpecfloConfig, slug: str, entry: Entry) -> Path:
    """The stored text of ``entry``; refused when it has none or its path leaves the folder."""
    relative = entry.get(TEXT_FIELD)
    if relative is None:
        raise SpecfloError(f"{entry.id} has no stored text: its entry has no {TEXT_FIELD} field.")
    return project_path(root, cfg, slug, relative, what=f"The {TEXT_FIELD} field of {entry.id}")


# --- the typed view over the ledger -------------------------------------------------


def _check_value(name: str, value: str) -> str:
    if not value:
        raise SpecfloError(f"A source's {name} field needs a value.")
    if name == DATE_FIELD:
        try:
            parsed = datetime.date.fromisoformat(value)
        except ValueError:
            parsed = None
        if parsed is None or parsed.isoformat() != value:
            raise SpecfloError(f"A source's {name} is a date as YYYY-MM-DD: got {value!r}.")
    elif name == FORMAT_FIELD and value not in FORMATS:
        raise SpecfloError(f"Unknown source format {value!r}: expected one of {', '.join(FORMATS)}.")
    elif name == PAGES_FIELD and not (value.isdigit() and int(value) > 0):
        raise SpecfloError(f"A source's {name} is a count above zero: got {value!r}.")
    elif name == SHA256_FIELD:
        value = value.lower()
        if not _SHA256.match(value):
            raise SpecfloError(f"A source's {name} is 64 hex digits: got {value!r}.")
    elif name in (ORIGINAL_FIELD, TEXT_FIELD):
        _check_relative(f"A source's {name} field", value)
    return value


def _checked_fields(kind: str, fields: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The entry's fields in contract order, Kind first; refused when the contract is broken."""
    if kind not in KINDS:
        raise SpecfloError(f"Unknown source kind {kind!r}: expected one of {', '.join(KINDS)}.")
    allowed = (DATE_FIELD, *_ALLOWED[kind])
    given: dict[str, str] = {}
    for name, value in fields:
        name = (name or "").strip()
        if name == KIND_FIELD:
            raise SpecfloError("A source's kind is given on its own, not as a Kind field.")
        if name not in FIELD_ORDER:
            raise SpecfloError(
                f"Unknown source field {name!r}: expected one of {', '.join(FIELD_ORDER)}."
            )
        if name not in allowed:
            raise SpecfloError(f"A {kind} source has no {name} field.")
        if name in given:
            raise SpecfloError(f"A source's {name} field is given twice.")
        given[name] = _check_value(name, (value or "").strip())
    for name in (DATE_FIELD, *_REQUIRED[kind]):
        if name not in given:
            raise SpecfloError(f"A {kind} source needs a {name} field.")
    return [(KIND_FIELD, kind), *((name, given[name]) for name in FIELD_ORDER if name in given)]


def add_source(
    root: Path,
    cfg: SpecfloConfig,
    slug: str,
    title: str,
    kind: str,
    fields: list[tuple[str, str]],
    today: str | None = None,
    precheck: Callable[[list[Entry]], None] | None = None,
) -> Entry:
    """Record a source of ``kind`` and return its entry with the ID the ledger minted.

    ``fields`` are the entry's fields after Kind, in any order; they are checked
    against the contract above and written in its order. Refused, before
    anything is written, for an unknown kind, a field the kind has not, a
    missing required field, a value in the wrong shape and a repeated field.
    ``precheck`` is handed to the ledger: it runs under the research lock with
    the sources held at that moment, right before the ID is minted, and a
    SpecfloError it raises refuses the add with nothing written.
    """
    return SOURCES.add(
        root, cfg, slug, title, _checked_fields(kind, list(fields or [])), today=today,
        precheck=precheck,
    )


def record_stored(
    root: Path,
    cfg: SpecfloConfig,
    slug: str,
    entry_id: str,
    original: Path | None,
    text: Path,
    today: str | None = None,
) -> Entry:
    """Record where source ``entry_id`` keeps its original and its text; return the entry.

    ``original`` is None for a source that has no original file, only its text.
    """
    fields = []
    if original is not None:
        fields.append((ORIGINAL_FIELD, project_relative(root, cfg, slug, original)))
    fields.append((TEXT_FIELD, project_relative(root, cfg, slug, text)))
    return SOURCES.set_fields(root, cfg, slug, entry_id, fields, today=today)


def add_text_source(
    root: Path,
    cfg: SpecfloConfig,
    slug: str,
    kind: str,
    title: str,
    text: str,
    url: str | None = None,
    today: str | None = None,
) -> Entry:
    """Store ``text`` as an interview, web or repo source of project ``slug``; return its entry.

    Refused, with nothing written: a kind other than those three (a document
    comes in through ingest, which keeps its bytes and sha256), an empty or
    multi-line title, empty text, a web or repo source without a URL and an
    interview with one. Then the ledger mints the ID, the source directory is
    made for it, the text is written there as given and the entry records it.
    """
    if kind == DOCUMENT_KIND:
        raise SpecfloError(
            "A document comes in through `specflo research ingest <file>`, which keeps its"
            f" bytes and sha256; `research source add` takes one of {', '.join(TEXT_KINDS)}."
        )
    if kind not in TEXT_KINDS:
        raise SpecfloError(
            f"Unknown source kind {kind!r}: use one of {', '.join(TEXT_KINDS)}"
            " (a document comes in through `specflo research ingest <file>`)."
        )
    if title is None or not title.strip():
        raise SpecfloError("A source needs a title: one line naming it.")
    require_one_line("A source's title", title)
    title = title.strip()
    if not text or not text.strip():
        raise SpecfloError("A source needs its text: the file or standard input is empty.")
    url = (url or "").strip() or None
    if kind == INTERVIEW_KIND and url is not None:
        raise SpecfloError(
            "An interview source has no --url: it is the user's own words, not a fetched page."
        )
    if kind != INTERVIEW_KIND and url is None:
        raise SpecfloError(f"A {kind} source needs --url: where it was fetched from.")
    if url is not None:
        require_one_line(f"A {kind} source's --url", url)
    today = today or datetime.date.today().isoformat()

    fields = [(DATE_FIELD, today)]
    if url is not None:
        fields.append((URL_FIELD, url))
    entry = add_source(root, cfg, slug, title, kind, fields, today=today)
    # The ID is minted once and never again, so this directory is this
    # source's alone; anything already there was never a recorded source.
    directory = source_dir(root, cfg, slug, entry.id)
    text_file = text_path(root, cfg, slug, entry.id)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        text_file.write_bytes(text.encode("utf-8"))
    except OSError as exc:
        raise SpecfloError(
            f"{entry.id} is recorded but its text could not be stored under {directory}: {exc}."
        ) from exc
    return record_stored(root, cfg, slug, entry.id, None, text_file, today=today)


def egress_hint(root: Path, cfg: SpecfloConfig, slug: str) -> str | None:
    """The one-line egress suggestion, or None.

    The suggestion is due once, at the first source of a project that pins no
    egress class: call this before the source is added, since the ledger is
    then no longer empty. It reads the project record and the ledger and
    writes nothing; the pin itself is set by ``specflo egress`` only.
    """
    if load_project(root, cfg, slug).egress:
        return None
    if SOURCES.entries(root, cfg, slug):
        return None
    return EGRESS_HINT


def sources(
    root: Path, cfg: SpecfloConfig, slug: str, include_superseded: bool = True
) -> list[Entry]:
    """Every source in ledger order; ``include_superseded=False`` keeps the active ones."""
    return SOURCES.entries(root, cfg, slug, include_superseded)


def holder_of_sha256(entries: list[Entry], digest: str) -> Entry | None:
    """The entry among ``entries`` whose original has the sha256 ``digest``, or None.

    Pure: it reads nothing, so a caller can run it on the entries the ledger
    hands it under the research lock.
    """
    digest = (digest or "").strip().lower()
    if not digest:
        return None
    for entry in entries:
        if (entry.get(SHA256_FIELD) or "").strip().lower() == digest:
            return entry
    return None


def find_by_sha256(root: Path, cfg: SpecfloConfig, slug: str, digest: str) -> Entry | None:
    """The source whose original has the sha256 ``digest``, replaced ones included, or None.

    A replaced source still holds its bytes, so the same file is never stored
    twice, whichever entry is active.
    """
    return holder_of_sha256(SOURCES.entries(root, cfg, slug), digest)

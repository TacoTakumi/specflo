"""The research document ``research.md`` and its append-only log.

A research project keeps one ``research.md`` next to ``project.md`` with four
sections the CLI owns:

- ``Destination``: where the research is headed (build, inform, learn or
  unknown) and the reason of its last change, as ``- Destination:`` and
  ``- Reason:`` lines.
- ``Usual user``: who usually sits at the keyboard, as free text in their
  own words.
- ``Decisions``: the same append-only section ``brainstorm.md`` has, with the
  exact header the decision code looks for, so that code can write and read
  decisions here.
- ``Log``: dated one-line entries, each with an optional kind, appended by
  the research commands and never rewritten.

Every write to the document and to the ledgers under ``research/`` takes the
one lock :data:`LOCK_NAME` for the project, so a ledger add that also logs
cannot race another.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from pathlib import Path

from .. import markdown
from ..config import SpecfloConfig
from ..errors import SpecfloError, require_one_line
from ..locking import lock_path_for, locked
from ..projects import load_project, project_dir, validate_slug

RESEARCH_FILENAME = "research.md"
# The directory under the project folder that holds the ledgers and sources.
RESEARCH_DIRNAME = "research"
# The one lock every research write takes, named for the package since the
# document and the four ledgers are written together.
LOCK_NAME = "research"

DESTINATION_HEADER = "## Destination"
USER_HEADER = "## Usual user"
DECISIONS_HEADER = "## Decisions"
LOG_HEADER = "## Log"
SECTION_HEADERS = (DESTINATION_HEADER, USER_HEADER, DECISIONS_HEADER, LOG_HEADER)

DESTINATIONS = ("build", "inform", "learn", "unknown")
UNKNOWN_DESTINATION = "unknown"

_DESTINATION_LINE = re.compile(r"^- Destination:(.*)$")
_REASON_LINE = re.compile(r"^- Reason:(.*)$")
# ``- 2026-10-16: text`` or ``- 2026-10-16 session: text``.
_LOG_LINE = re.compile(r"^- (\d{4}-\d{2}-\d{2})(?: ([a-z][a-z0-9-]*))?: (.*)$")
_KIND = re.compile(r"^[a-z][a-z0-9-]*$")

_TEMPLATE = """\
---
project: {slug}
created: {today}
updated: {today}
---

# Research: {name}

{destination_header}
<!-- managed by the research commands: build, inform, learn or unknown, with the reason of the last change -->
- Destination: {destination}

{user_header}
<!-- managed by the research commands: who usually sits at the keyboard, in their own words -->

{decisions_header}
<!-- append-only; managed by `specflo decision add` -->

{log_header}
<!-- append-only; dated entries the research commands write, never rewritten -->
"""


@dataclass(frozen=True)
class LogEntry:
    date: str
    kind: str | None
    text: str


@dataclass(frozen=True)
class ResearchDocument:
    destination: str
    reason: str | None
    usual_user: str
    log: tuple[LogEntry, ...]


def research_dir(root: Path, cfg: SpecfloConfig, slug: str) -> Path:
    """``<project>/research/``, the folder of the ledgers and stored sources."""
    return project_dir(root, cfg, validate_slug(slug)) / RESEARCH_DIRNAME


def research_path(root: Path, cfg: SpecfloConfig, slug: str) -> Path:
    """``<project>/research.md``."""
    return project_dir(root, cfg, validate_slug(slug)) / RESEARCH_FILENAME


def start_document(
    root: Path, cfg: SpecfloConfig, slug: str, today: str | None = None
) -> tuple[Path, bool]:
    """Create the research document, or locate an existing one.

    Returns ``(path, created)``; ``created`` is False when the file already
    existed, which is never clobbered. Refuses a project that does not exist.
    """
    project = load_project(root, cfg, slug)
    path = research_path(root, cfg, slug)
    if path.exists():
        return path, False
    today = today or datetime.date.today().isoformat()
    path.write_text(
        _TEMPLATE.format(
            slug=project.slug,
            name=project.name,
            today=today,
            destination=UNKNOWN_DESTINATION,
            destination_header=DESTINATION_HEADER,
            user_header=USER_HEADER,
            decisions_header=DECISIONS_HEADER,
            log_header=LOG_HEADER,
        )
    )
    return path, True


def read_document(root: Path, cfg: SpecfloConfig, slug: str) -> str:
    """The document's text; refused when there is none or it cannot be read."""
    path = research_path(root, cfg, slug)
    if not path.is_file():
        raise SpecfloError(f"No research document for project {slug!r}: {path.name} is missing.")
    try:
        return path.read_text()
    except (OSError, UnicodeDecodeError) as exc:
        raise SpecfloError(f"Cannot read {slug}/{path.name}: {exc}.") from exc


def _section(doc: str, header: str) -> str:
    body = markdown.section_body(doc, header)
    if body is None:
        raise SpecfloError(f"Malformed {RESEARCH_FILENAME}: no '{header}' section.")
    return markdown.strip_comments(body)


def parse(doc: str) -> ResearchDocument:
    """Every CLI-owned value of ``doc``; refused when a section is missing.

    A destination line that is missing reads as unknown with no reason, as a
    new document has it. The decisions are the brainstorm code's to read.
    """
    destination, reason = UNKNOWN_DESTINATION, None
    for raw in _section(doc, DESTINATION_HEADER).splitlines():
        line = raw.rstrip("\r\n")
        if m := _DESTINATION_LINE.match(line):
            destination = m.group(1).strip() or UNKNOWN_DESTINATION
        elif m := _REASON_LINE.match(line):
            reason = m.group(1).strip() or None
    usual_user = _section(doc, USER_HEADER).strip()
    _section(doc, DECISIONS_HEADER)
    log = tuple(
        LogEntry(date=m.group(1), kind=m.group(2), text=m.group(3).strip())
        for raw in _section(doc, LOG_HEADER).splitlines()
        if (m := _LOG_LINE.match(raw.rstrip("\r\n")))
    )
    return ResearchDocument(destination=destination, reason=reason, usual_user=usual_user, log=log)


def load(root: Path, cfg: SpecfloConfig, slug: str) -> ResearchDocument:
    """:func:`parse` of the project's research document."""
    return parse(read_document(root, cfg, slug))


def _log_line(entry: LogEntry) -> str:
    head = f"- {entry.date} {entry.kind}" if entry.kind else f"- {entry.date}"
    return f"{head}: {entry.text}"


def append_line_to_section(doc: str, header: str, line: str) -> str:
    """``doc`` with ``line`` as the last line of the ``header`` section.

    Unlike ``markdown.append_to_section`` it adds no blank line after the
    entry, so the dated lines of the log stay one list.
    """
    body = markdown.section_body(doc, header)
    if body is None:
        raise SpecfloError(f"Malformed {RESEARCH_FILENAME}: no '{header}' section.")
    kept = body.rstrip("\n")
    return markdown.replace_section_body(doc, header, f"{kept}\n{line}" if kept else line)


def append_log(
    root: Path,
    cfg: SpecfloConfig,
    slug: str,
    text: str,
    kind: str | None = None,
    today: str | None = None,
) -> LogEntry:
    """Append a dated entry to the Log and return it.

    ``kind`` is an optional lowercase word (``session``, ``destination``) a
    reader groups entries by. Refuses an empty or multi-line text and a kind
    in another shape before anything is written.
    """
    if text is None or not text.strip():
        raise SpecfloError("A log entry needs a non-empty text.")
    require_one_line("A log entry's text", text)
    if kind is not None and not _KIND.match(kind):
        raise SpecfloError(
            f"A log entry's kind is one lowercase word, like 'session': got {kind!r}."
        )
    today = today or datetime.date.today().isoformat()
    entry = LogEntry(date=today, kind=kind, text=text.strip())
    with locked(lock_path_for(root, slug, LOCK_NAME)):
        doc = read_document(root, cfg, slug)
        doc = append_line_to_section(doc, LOG_HEADER, _log_line(entry))
        research_path(root, cfg, slug).write_text(markdown.bump_updated(doc, today))
    return entry

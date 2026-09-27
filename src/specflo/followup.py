"""Follow-ups: the work a project leaves behind for a later one.

Each project keeps its follow-ups in its own ``followup.md``, created on the
first add. An entry is an ``FU-NN`` heading with a Do line, an optional From
line and a Status line, and a dated Closed line once it is closed. The numbers
run across the whole projects directory: the next one is one above the highest
``FU-NN`` found in any project's followup document, hand-written documents
included, so any project can cite an entry with no project prefix.

Minting reads every followup document, so every write takes one lock for the
whole projects directory rather than the per-project artifact lock.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from pathlib import Path

from . import markdown
from .config import SpecfloConfig
from .errors import SpecfloError
from .locking import lock_path_for, locked
from .projects import load_project

FOLLOWUP_FILENAME = "followup.md"
SECTION_HEADER = "## Follow-ups"

# The lock every followup write takes. A slug never starts with a dot, so this
# lock directory cannot be a project's own.
_LOCK_SCOPE = ".followups"

# Any FU-NN mention counts for numbering: the hand-written documents name their
# entries as "### FU-85." headings and "**FU-03 (O2).**" bullets.
_ANY_ID = re.compile(r"\bFU-(\d+)\b")

# The heading of an entry the followup verbs wrote.
_HEADING = re.compile(r"### (FU-\d+) - ")
_ANY_HEADING = re.compile(r"#{1,6} ")


@dataclass(frozen=True)
class FollowUp:
    id: str
    project: str
    title: str
    do: str
    source: str | None
    status: str


def followup_path(root: Path, cfg: SpecfloConfig, slug: str) -> Path:
    return load_project(root, cfg, slug).path / FOLLOWUP_FILENAME


def _documents(root: Path, cfg: SpecfloConfig) -> list[Path]:
    """Every followup document under the projects directory."""
    return sorted((root / cfg.projects_dir).glob(f"*/{FOLLOWUP_FILENAME}"))


def _next_id(root: Path, cfg: SpecfloConfig) -> str:
    numbers = [
        int(match.group(1))
        for path in _documents(root, cfg)
        for match in _ANY_ID.finditer(path.read_text())
    ]
    return f"FU-{(max(numbers) + 1 if numbers else 1):02d}"


def _status_line(doc: str, followup_id: str) -> int | None:
    """Line index of the Status line of entry ``followup_id`` in ``doc``, or None.

    An entry runs from its ``### FU-NN - <title>`` heading to the next heading.
    A hand-written entry has no Status line, so it is never found here.
    """
    lines = doc.splitlines(keepends=True)
    start = next(
        (
            i
            for i, line, in_fence in markdown.iter_lines_with_fence(doc)
            if not in_fence and (m := _HEADING.match(line)) and m.group(1) == followup_id
        ),
        None,
    )
    if start is None:
        return None
    for i in range(start + 1, len(lines)):
        if _ANY_HEADING.match(lines[i]):
            break
        if lines[i].startswith("- Status: "):
            return i
    return None


def _one_line(label: str, value: str | None, required: bool) -> str | None:
    """``value`` stripped; refused when required and empty, or over more than one line."""
    if value is None or not value.strip():
        if required:
            raise SpecfloError(f"A follow-up needs a non-empty {label}.")
        return None
    if "\n" in value or "\r" in value:
        raise SpecfloError(f"A follow-up's {label} must be one line.")
    return value.strip()


def _new_document(slug: str, today: str) -> str:
    return (
        f"---\nproject: {slug}\ncreated: {today}\nupdated: {today}\n---\n\n"
        f"# Follow-ups: {slug}\n\n"
        "Work this project leaves for a later one.\n\n"
        f"{SECTION_HEADER}\n"
        "<!-- managed by `specflo followup`. IDs FU-NN are numbered across every project. -->\n\n"
    )


def add_followup(
    root: Path,
    cfg: SpecfloConfig,
    slug: str,
    title: str,
    do: str,
    source: str | None = None,
    today: str | None = None,
) -> FollowUp:
    """Append a follow-up to ``slug``'s followup document and return it.

    Creates the document on the first add. Refuses an empty title or Do line,
    and any field over more than one line, before anything is written.
    """
    title = _one_line("title", title, required=True)
    do = _one_line("Do line", do, required=True)
    source = _one_line("From line", source, required=False)
    today = today or datetime.date.today().isoformat()
    path = followup_path(root, cfg, slug)
    with locked(lock_path_for(root, _LOCK_SCOPE, FOLLOWUP_FILENAME)):
        new_id = _next_id(root, cfg)
        doc = path.read_text() if path.is_file() else _new_document(slug, today)
        if SECTION_HEADER not in markdown.section_headers(doc):
            raise SpecfloError(f"Malformed {slug}/followup: no '{SECTION_HEADER}' section.")
        lines = [f"### {new_id} - {title}", f"- Do: {do}"]
        if source:
            lines.append(f"- From: {source}")
        lines.append("- Status: open")
        doc = markdown.append_to_section(doc, SECTION_HEADER, "\n".join(lines) + "\n")
        doc = markdown.bump_updated(doc, today)
        path.write_text(doc)
    return FollowUp(id=new_id, project=slug, title=title, do=do, source=source, status="open")


def close_followup(
    root: Path,
    cfg: SpecfloConfig,
    followup_id: str,
    note: str,
    today: str | None = None,
) -> str:
    """Close the open entry ``followup_id`` in whichever project holds it.

    Sets its Status to closed and adds a dated Closed line below it. Refuses an
    empty or multi-line note, an ID no entry carries, and an entry that is not
    open, before anything is written. Returns the slug of the project it closed
    the entry in.
    """
    note = _one_line("note", note, required=True)
    today = today or datetime.date.today().isoformat()
    with locked(lock_path_for(root, _LOCK_SCOPE, FOLLOWUP_FILENAME)):
        for path in _documents(root, cfg):
            doc = path.read_text()
            index = _status_line(doc, followup_id)
            if index is None:
                continue
            lines = doc.splitlines(keepends=True)
            slug = path.parent.name
            status = lines[index].removeprefix("- Status: ").strip()
            if status != "open":
                raise SpecfloError(f"{followup_id} in {slug}/followup is {status}, not open.")
            lines[index : index + 1] = ["- Status: closed\n", f"- Closed: {today}: {note}\n"]
            path.write_text(markdown.bump_updated("".join(lines), today))
            return slug
    raise SpecfloError(f"No follow-up {followup_id} in this checkout's projects.")

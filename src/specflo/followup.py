"""Follow-ups: the work a project leaves behind for a later one.

Each project keeps its follow-ups in its own ``followup.md``, created on the
first add. An entry is an ``FU-NN`` heading with a Do line, an optional From
line and a Status line, and a dated Closed line once it is closed, with a
Closed by line when the close named what did the work. The numbers
run across the whole projects directory: the next one is one above the highest
``FU-NN`` found in any project's followup document, hand-written documents
included, so any project can cite an entry with no project prefix.

Minting reads every followup document, so every write takes one lock for the
whole projects directory rather than the per-project artifact lock.
"""

from __future__ import annotations

import datetime
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import markdown, plan
from .config import SpecfloConfig
from .errors import SpecfloError
from .locking import lock_path_for, locked
from .projects import PROJECT_FILENAME, load_project, project_dir

FOLLOWUP_FILENAME = "followup.md"
SECTION_HEADER = "## Follow-ups"

# The lock every followup write takes. A slug never starts with a dot, so this
# lock directory cannot be a project's own.
_LOCK_SCOPE = ".followups"

# Any FU-NN mention counts for numbering: the hand-written documents name their
# entries as "### FU-85." headings and bold "**FU-03 ...**" bullets.
_ANY_ID = re.compile(r"\bFU-(\d+)\b")

# The heading and field lines of an entry the followup verbs wrote.
_HEADING = re.compile(r"### (FU-\d+) - (.*)")
_FIELD = re.compile(r"- (Do|From|Status|Closed|Closed by): (.*)")
_ANY_HEADING = re.compile(r"#{1,6} ")
_TASK_ID = re.compile(r"T-\d+")


@dataclass(frozen=True)
class FollowUp:
    id: str
    project: str
    title: str
    do: str
    source: str | None
    status: str
    closed: str | None = None
    closed_by: str | None = None


def followup_path(root: Path, cfg: SpecfloConfig, slug: str) -> Path:
    return load_project(root, cfg, slug).path / FOLLOWUP_FILENAME


def _documents(root: Path, cfg: SpecfloConfig) -> list[Path]:
    """Every project's followup document.

    Like the project listing, a dot-directory or a directory with no
    ``project.md`` is not a project, so its document is not read.
    """
    return sorted(
        path
        for path in (root / cfg.projects_dir).glob(f"*/{FOLLOWUP_FILENAME}")
        if not path.parent.name.startswith(".") and (path.parent / PROJECT_FILENAME).is_file()
    )


def _read(path: Path) -> str:
    """The document's text, or a refusal that names the document."""
    try:
        return path.read_text()
    except (OSError, UnicodeDecodeError) as exc:
        raise SpecfloError(f"Cannot read {path.parent.name}/followup: {exc}.") from exc


def unreadable_documents(root: Path, cfg: SpecfloConfig) -> list[str]:
    """Why each followup document that cannot be read was refused."""
    problems = []
    for path in _documents(root, cfg):
        try:
            _read(path)
        except SpecfloError as exc:
            problems.append(str(exc))
    return problems


def _next_id(root: Path, cfg: SpecfloConfig) -> str:
    numbers = [
        int(match.group(1))
        for path in _documents(root, cfg)
        for match in _ANY_ID.finditer(_read(path))
    ]
    return f"FU-{(max(numbers) + 1 if numbers else 1):02d}"


def _entries(doc: str, slug: str) -> list[tuple[FollowUp, int]]:
    """Each entry the followup verbs wrote in ``doc``, with its Status line's index.

    An entry runs from its ``### FU-NN - <title>`` heading to the next heading.
    A hand-written entry has no Status line, so it is left out.
    """
    lines = doc.splitlines(keepends=True)
    headings = [
        (i, m)
        for i, line, in_fence in markdown.iter_lines_with_fence(doc)
        if not in_fence and (m := _HEADING.match(line))
    ]
    found = []
    for start, heading in headings:
        values: dict[str, str] = {}
        status_at = None
        for i in range(start + 1, len(lines)):
            if _ANY_HEADING.match(lines[i]):
                break
            if (field := _FIELD.match(lines[i])) and field.group(1) not in values:
                values[field.group(1)] = field.group(2).strip()
                if field.group(1) == "Status":
                    status_at = i
        if status_at is None:
            continue
        entry = FollowUp(
            id=heading.group(1),
            project=slug,
            title=heading.group(2).strip(),
            do=values.get("Do", ""),
            source=values.get("From"),
            status=values["Status"],
            closed=values.get("Closed"),
            closed_by=values.get("Closed by"),
        )
        found.append((entry, status_at))
    return found


def _one_line(label: str, value: str | None, required: bool) -> str | None:
    """``value`` stripped; refused when required and empty, or over more than one line."""
    if value is None or not value.strip():
        if required:
            raise SpecfloError(f"A follow-up needs a non-empty {label}.")
        return None
    # Any line break the document is later split on, not only \n and \r.
    if value.splitlines() != [value]:
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
        doc = _read(path) if path.is_file() else _new_document(slug, today)
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


def _is_checkout_project(root: Path, cfg: SpecfloConfig, slug: str) -> bool:
    return (
        bool(slug)
        and not slug.startswith(".")
        and (project_dir(root, cfg, slug) / PROJECT_FILENAME).is_file()
    )


def _short_commit(root: Path, ref: str) -> str | None:
    """The short SHA of the commit ``ref`` names in the checkout's git repository, or None."""
    if ref.startswith("-"):
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", "--short", f"{ref}^{{commit}}"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and sha else None


def resolve_closed_by(root: Path, cfg: SpecfloConfig, ref: str) -> str:
    """The Closed by value for ``ref``, or a refusal that names the ref.

    ``<project>/<T-NN>`` names a task in a project's plan; ``<project>`` names a
    project of this checkout; anything else must name a commit in the
    checkout's git repository and is stored as its short SHA. A project name
    wins over a commit that the same word would name.
    """
    ref = _one_line("--by ref", ref, required=True)
    if "/" in ref:
        slug, _, task_id = ref.partition("/")
        if not _is_checkout_project(root, cfg, slug):
            raise SpecfloError(f"--by {ref}: no project {slug!r} in this checkout.")
        if not _TASK_ID.fullmatch(task_id):
            raise SpecfloError(f"--by {ref}: expected <project>/<T-NN>.")
        try:
            tasks = plan.list_tasks(root, cfg, slug, include_superseded=True)
        except SpecfloError:
            tasks = []
        except (OSError, UnicodeDecodeError) as exc:
            raise SpecfloError(f"--by {ref}: cannot read the plan of {slug!r}: {exc}.") from exc
        if task_id not in {task.id for task in tasks}:
            raise SpecfloError(f"--by {ref}: project {slug!r} has no task {task_id} in its plan.")
        return ref
    if _is_checkout_project(root, cfg, ref):
        return ref
    sha = _short_commit(root, ref)
    if sha is None:
        raise SpecfloError(
            f"--by {ref}: names no project in this checkout and no commit in its git repository."
        )
    return sha


def close_followup(
    root: Path,
    cfg: SpecfloConfig,
    followup_id: str,
    note: str,
    by: str | None = None,
    today: str | None = None,
) -> str:
    """Close the open entry ``followup_id`` in whichever project holds it.

    Sets its Status to closed and adds a dated Closed line below it, and a
    Closed by line below that when ``by`` names what did the work (see
    ``resolve_closed_by``). When two projects hold the ID, as a merge of two
    branches can leave them, the open one is closed. Refuses an empty or
    multi-line note, a ``by`` that names no work, an ID no entry carries, and
    an ID with no open entry, before anything is written. Returns the slug of
    the project it closed the entry in.
    """
    note = _one_line("note", note, required=True)
    closed_by = resolve_closed_by(root, cfg, by) if by is not None else None
    today = today or datetime.date.today().isoformat()
    with locked(lock_path_for(root, _LOCK_SCOPE, FOLLOWUP_FILENAME)):
        found = []
        for path in _documents(root, cfg):
            doc = _read(path)
            found += [
                (path, doc, entry, index)
                for entry, index in _entries(doc, path.parent.name)
                if entry.id == followup_id
            ]
        if not found:
            raise SpecfloError(f"No follow-up {followup_id} in this checkout's projects.")
        open_one = next((match for match in found if match[2].status == "open"), None)
        if open_one is None:
            entry = found[0][2]
            raise SpecfloError(
                f"{followup_id} in {entry.project}/followup is {entry.status}, not open."
            )
        path, doc, entry, index = open_one
        lines = doc.splitlines(keepends=True)
        closed = ["- Status: closed\n", f"- Closed: {today}: {note}\n"]
        if closed_by is not None:
            closed.append(f"- Closed by: {closed_by}\n")
        lines[index : index + 1] = closed
        path.write_text(markdown.bump_updated("".join(lines), today))
        return entry.project


def list_followups(root: Path, cfg: SpecfloConfig, include_closed: bool = False) -> list[FollowUp]:
    """The open entries of every project, in ID order; ``include_closed`` adds the rest.

    Hand-written entries have no Status line and are never listed. Like the
    project listing, one document that cannot be read does not take the rest
    down: it is skipped, and ``unreadable_documents`` names it.
    """
    entries = []
    for path in _documents(root, cfg):
        try:
            doc = _read(path)
        except SpecfloError:
            continue
        entries += [
            entry
            for entry, _ in _entries(doc, path.parent.name)
            if include_closed or entry.status == "open"
        ]
    return sorted(entries, key=lambda entry: int(entry.id.removeprefix("FU-")))


def require_open(root: Path, cfg: SpecfloConfig, followup_ids: list[str]) -> None:
    """Refuse, naming the first one, when any of ``followup_ids`` has no open entry.

    A followup document that cannot be read is refused as well, since
    ``close_followup`` reads every document and would refuse it later.
    """
    for path in _documents(root, cfg):
        _read(path)
    for followup_id in followup_ids:
        entry = show_followup(root, cfg, followup_id)
        if entry.status != "open":
            raise SpecfloError(
                f"{followup_id} in {entry.project}/followup is {entry.status}, not open."
            )


def show_followup(root: Path, cfg: SpecfloConfig, followup_id: str) -> FollowUp:
    """The entry ``followup_id``, open or closed, in whichever project holds it.

    When two projects hold the ID, the open one is returned, the one close
    would act on. A document that cannot be read is skipped, as in
    ``list_followups``. Refuses an ID no entry carries, a hand-written one
    included.
    """
    found = [
        entry
        for entry in list_followups(root, cfg, include_closed=True)
        if entry.id == followup_id
    ]
    if not found:
        raise SpecfloError(f"No follow-up {followup_id} in this checkout's projects.")
    return next((entry for entry in found if entry.status == "open"), found[0])

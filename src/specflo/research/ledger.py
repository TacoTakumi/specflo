"""The research ledgers: one append-only markdown file per kind of entry.

A research project keeps four ledgers under ``research/``: sources (S-NN),
questions (Q-NN), findings (RF-NN) and the glossary, keyed by term with no
minted ID. An entry is a ``### <id> - <title>`` heading (``### <term>`` in
the glossary) followed by ``- Field: value`` lines; a list field repeats its
line, one value per line, in order.

The CLI mints the IDs: two digits, zero-padded, one above the highest number
the ledger mentions anywhere, so a number is never handed out twice, not
after a supersede and not after a hand-written mention. The read that picks
the number and the write that records it sit under the project's research
lock, so two adds at once never share an ID. An add may also hand in a
precheck: a callable run under that same lock with the entries the ledger
holds at that moment, so a caller can refuse an entry by the ledger's current
state (a document's bytes already held, say) and not by an earlier read that
another add may have overtaken.

No entry is ever deleted. An entry is replaced by a later one: the old entry
gets a ``- Superseded by: <new id>`` line and the new one a ``- Supersedes:
<old id>`` line. Those two fields are the ledger's own; a caller's ``Status``
line (open or resolved for a question, supported or unsure for a finding)
stays as it is when the entry is replaced, so a replaced finding still says
what support it had.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .. import markdown
from ..config import SpecfloConfig
from ..errors import SpecfloError, require_one_line
from ..locking import lock_path_for, locked
from .document import LOCK_NAME, research_dir

SOURCES_DIRNAME = "sources"
STATUS_FIELD = "Status"
SUPERSEDES_FIELD = "Supersedes"
SUPERSEDED_BY_FIELD = "Superseded by"
# The fields the ledger writes itself; an add or a field set may not name them.
OWN_FIELDS = (SUPERSEDES_FIELD, SUPERSEDED_BY_FIELD)

_FIELD_LINE = re.compile(r"^- ([A-Za-z][A-Za-z0-9 ]*):(.*)$")
_FIELD_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9 ]*$")
_ANY_HEADING = re.compile(r"#{1,6} ")
_SOURCE_ID = re.compile(r"^S-\d+$")

_TEMPLATE = """\
---
project: {slug}
ledger: {name}
created: {today}
updated: {today}
---

# {title}: {slug}

{about}

## {title}
<!-- managed by the research commands. {rule} -->

"""


@dataclass(frozen=True)
class Entry:
    """One ledger entry: its heading and every field line in document order."""

    id: str
    title: str
    fields: tuple[tuple[str, str], ...] = ()

    def get(self, field: str) -> str | None:
        """The first value of ``field``, or None when the entry has none."""
        return next((value for name, value in self.fields if name == field), None)

    def all(self, field: str) -> list[str]:
        """Every value of ``field`` in document order; a list field's values."""
        return [value for name, value in self.fields if name == field]

    @property
    def status(self) -> str | None:
        return self.get(STATUS_FIELD)

    @property
    def supersedes(self) -> str | None:
        return self.get(SUPERSEDES_FIELD)

    @property
    def superseded_by(self) -> str | None:
        return self.get(SUPERSEDED_BY_FIELD)

    @property
    def active(self) -> bool:
        """True while no later entry has replaced this one."""
        return self.superseded_by is None


def _heading_re(prefix: str | None) -> re.Pattern[str]:
    if prefix is None:
        return re.compile(r"^### (\S.*?)\s*$")
    return re.compile(rf"^### ({re.escape(prefix)}-\d+) - (.*)$")


def _blocks(doc: str, prefix: str | None) -> list[tuple[int, int, re.Match[str]]]:
    """``(start, end, heading match)`` of each entry: its heading line to the next heading."""
    lines = doc.splitlines(keepends=True)
    heading = _heading_re(prefix)
    starts = [
        (i, m)
        for i, line, in_fence in markdown.iter_lines_with_fence(doc)
        if not in_fence and (m := heading.match(line.rstrip("\r\n")))
    ]
    blocks = []
    for start, match in starts:
        end = len(lines)
        for i, line, in_fence in markdown.iter_lines_with_fence(doc):
            if i > start and not in_fence and _ANY_HEADING.match(line):
                end = i
                break
        blocks.append((start, end, match))
    return blocks


def _field_lines(doc: str, start: int, end: int) -> list[tuple[int, str, str]]:
    """``(index, name, value)`` of each field line between ``start`` and ``end``."""
    lines = doc.splitlines(keepends=True)
    found = []
    in_fence = False
    for i in range(start + 1, end):
        line = lines[i].rstrip("\r\n")
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence and (m := _FIELD_LINE.match(line)):
            found.append((i, m.group(1).strip(), m.group(2).strip()))
    return found


def _entry_of(doc: str, block: tuple[int, int, re.Match[str]]) -> Entry:
    start, end, match = block
    title = match.group(2).strip() if match.re.groups > 1 else ""
    return Entry(
        id=match.group(1),
        title=title,
        fields=tuple((name, value) for _, name, value in _field_lines(doc, start, end)),
    )


def parse_entries(doc: str, prefix: str | None) -> list[Entry]:
    """Every entry of ``doc`` in document order, replaced ones included.

    With a ``prefix`` an entry is a ``### <prefix>-NN - <title>`` heading;
    with none, any ``### <term>`` heading, whose text is the entry's id.
    Headings and fields inside a code fence are quoted text, not entries.
    """
    return [_entry_of(doc, block) for block in _blocks(doc, prefix)]


def render_entry(entry_id: str, title: str, fields: list[tuple[str, str]]) -> str:
    """The text of one entry: its heading and one line per field, in order."""
    heading = f"### {entry_id} - {title}" if title else f"### {entry_id}"
    return "".join(f"{line}\n" for line in [heading, *(f"- {n}: {v}" for n, v in fields)])


def _block_of(doc: str, entry_id: str, prefix: str | None) -> tuple[int, int, re.Match[str]]:
    for block in _blocks(doc, prefix):
        if block[2].group(1) == entry_id:
            return block
    raise SpecfloError(f"No entry {entry_id} in this ledger.")


def set_fields(
    doc: str, entry_id: str, fields: list[tuple[str, str]], prefix: str | None
) -> str:
    """``doc`` with the named fields of entry ``entry_id`` set to ``fields``.

    Each field named is rewritten whole: its existing lines go, and its new
    values (one line each, so a list field is replaced by the list given)
    take the place of the first line that went, or follow the entry's last
    field line when the entry had none. Other fields stay where they are.
    """
    start, end, _ = _block_of(doc, entry_id, prefix)
    lines = doc.splitlines(keepends=True)
    existing = {i: name for i, name, _ in _field_lines(doc, start, end)}
    names = {name for name, _ in fields}
    replaced: set[str] = set()
    kept: list[str] = []
    # Where a field the entry had none of goes: right after the heading, or
    # after the last field line written out, replaced ones included.
    after_fields = 1
    for i in range(start, end):
        name = existing.get(i)
        if name in names:
            if name not in replaced:
                replaced.add(name)
                kept.extend(f"- {name}: {v}\n" for n, v in fields if n == name)
                after_fields = len(kept)
            continue
        # The document's last line may carry no newline; a line after it needs one.
        kept.append(lines[i] if lines[i].endswith("\n") else lines[i] + "\n")
        if name is not None:
            after_fields = len(kept)
    kept[after_fields:after_fields] = [f"- {n}: {v}\n" for n, v in fields if n not in replaced]
    return "".join(lines[:start]) + "".join(kept) + "".join(lines[end:])


def mark_superseded(doc: str, old_id: str, new_id: str, prefix: str | None) -> str:
    """``doc`` with entry ``old_id`` marked as replaced by ``new_id``."""
    return set_fields(doc, old_id, [(SUPERSEDED_BY_FIELD, new_id)], prefix)


def _check_fields(fields: list[tuple[str, str]], what: str) -> list[tuple[str, str]]:
    checked = []
    for name, value in fields:
        name = (name or "").strip()
        if not _FIELD_NAME.match(name):
            raise SpecfloError(
                f"{what} field name {name!r} is not one: use letters, digits and spaces,"
                " starting with a letter."
            )
        if name.casefold() in {own.casefold() for own in OWN_FIELDS}:
            raise SpecfloError(
                f"{what} may not set {name!r}: the ledger writes it when an entry is superseded."
            )
        require_one_line(f"{what} field {name!r}", value)
        checked.append((name, (value or "").strip()))
    return checked


@dataclass(frozen=True)
class Ledger:
    """One ledger: its file under ``research/``, its ID prefix and its verbs.

    ``prefix`` is None for the glossary, whose entries are keyed by term and
    minted by no one; its ``add`` is refused, the pure helpers serve it.
    """

    name: str
    prefix: str | None
    title: str
    about: str
    rule: str

    @property
    def filename(self) -> str:
        return f"{self.name}.md"

    @property
    def header(self) -> str:
        return f"## {self.title}"

    def path(self, root: Path, cfg: SpecfloConfig, slug: str) -> Path:
        """``<project>/research/<name>.md``."""
        return research_dir(root, cfg, slug) / self.filename

    def start(
        self, root: Path, cfg: SpecfloConfig, slug: str, today: str | None = None
    ) -> tuple[Path, bool]:
        """Create the empty ledger, or locate an existing one, never clobbering it."""
        path = self.path(root, cfg, slug)
        if path.exists():
            return path, False
        today = today or datetime.date.today().isoformat()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            _TEMPLATE.format(
                slug=slug, name=self.name, today=today, title=self.title, about=self.about,
                rule=self.rule,
            )
        )
        return path, True

    def read(self, root: Path, cfg: SpecfloConfig, slug: str) -> str:
        """The ledger's text; refused when there is none or it cannot be read."""
        path = self.path(root, cfg, slug)
        if not path.is_file():
            raise SpecfloError(
                f"No {self.name} ledger for project {slug!r}: {path.name} is missing."
            )
        try:
            return path.read_text()
        except (OSError, UnicodeDecodeError) as exc:
            raise SpecfloError(f"Cannot read {slug}/{path.name}: {exc}.") from exc

    def entries(
        self, root: Path, cfg: SpecfloConfig, slug: str, include_superseded: bool = True
    ) -> list[Entry]:
        """Every entry in document order; ``include_superseded=False`` keeps the active ones."""
        found = parse_entries(self.read(root, cfg, slug), self.prefix)
        if not include_superseded:
            found = [entry for entry in found if entry.active]
        return found

    def show(self, root: Path, cfg: SpecfloConfig, slug: str, entry_id: str) -> Entry:
        """The entry ``entry_id``, active or replaced; refused when there is none."""
        for entry in self.entries(root, cfg, slug):
            if entry.id == entry_id:
                return entry
        raise SpecfloError(f"No entry {entry_id} in the {self.name} ledger of project {slug!r}.")

    def _next_id(self, doc: str) -> str:
        """One above the highest number of this prefix the ledger mentions anywhere."""
        numbers = [int(m.group(1)) for m in re.finditer(rf"\b{re.escape(self.prefix)}-(\d+)\b", doc)]
        return f"{self.prefix}-{max(numbers, default=0) + 1:02d}"

    def _check_id(self, entry_id: str, what: str) -> str:
        if self.prefix is None or not re.fullmatch(rf"{re.escape(self.prefix)}-\d+", entry_id or ""):
            raise SpecfloError(
                f"{what} {entry_id!r} is not an ID of the {self.name} ledger ({self.prefix}-NN)."
            )
        return entry_id

    def _write(self, root: Path, cfg: SpecfloConfig, slug: str, doc: str, today: str) -> None:
        self.path(root, cfg, slug).write_text(markdown.bump_updated(doc, today))

    def add(
        self,
        root: Path,
        cfg: SpecfloConfig,
        slug: str,
        title: str,
        fields: list[tuple[str, str]] | None = None,
        supersedes: str | None = None,
        today: str | None = None,
        precheck: Callable[[list[Entry]], None] | None = None,
    ) -> Entry:
        """Append an entry with the next ID and return it.

        ``title`` is the entry's text, its heading. ``fields`` are written in
        the order given, a list field as one line per value. ``supersedes``
        names the entry this one replaces: it is marked with the new ID and
        the new entry links back to it, in the same write. Refuses an empty
        or multi-line title or value, a field name the ledger owns, an
        unknown or already replaced ``supersedes``, and a ledger with no
        file or no section, before anything is written.

        ``precheck``, when given, is called under the lock with every entry
        the ledger holds at that moment, replaced ones included, before the
        ID is minted. A :class:`SpecfloError` it raises propagates and nothing
        is written. It is the place for a refusal that depends on what the
        ledger holds, since a check made before the lock can be overtaken by
        another add between that check and this one's write.
        """
        if self.prefix is None:
            raise SpecfloError(
                f"The {self.name} ledger mints no IDs: its entries are keyed by term."
            )
        if title is None or not title.strip():
            raise SpecfloError(f"A {self.name} entry needs a non-empty title.")
        require_one_line(f"A {self.name} entry's title", title)
        title = title.strip()
        fields = _check_fields(list(fields or []), f"A {self.name} entry's")
        if supersedes is not None:
            self._check_id(supersedes, "--supersedes")
        today = today or datetime.date.today().isoformat()
        with locked(lock_path_for(root, slug, LOCK_NAME)):
            doc = self.read(root, cfg, slug)
            if self.header not in markdown.section_headers(doc):
                raise SpecfloError(f"Malformed {slug}/{self.filename}: no '{self.header}' section.")
            if precheck is not None:
                precheck(parse_entries(doc, self.prefix))
            if supersedes is not None:
                old = self._active_entry(doc, supersedes)
            new_id = self._next_id(doc)
            if supersedes is not None:
                doc = mark_superseded(doc, old.id, new_id, self.prefix)
                fields = [*fields, (SUPERSEDES_FIELD, old.id)]
            doc = markdown.append_to_section(doc, self.header, render_entry(new_id, title, fields))
            self._write(root, cfg, slug, doc, today)
        return Entry(id=new_id, title=title, fields=tuple(fields))

    def _active_entry(self, doc: str, entry_id: str) -> Entry:
        """Entry ``entry_id`` of ``doc``, refused when unknown or already replaced."""
        entry = next((e for e in parse_entries(doc, self.prefix) if e.id == entry_id), None)
        if entry is None:
            raise SpecfloError(f"No entry {entry_id} in the {self.name} ledger to supersede.")
        if not entry.active:
            raise SpecfloError(f"{entry_id} is already superseded by {entry.superseded_by}.")
        return entry

    def supersede(
        self,
        root: Path,
        cfg: SpecfloConfig,
        slug: str,
        old_id: str,
        new_id: str,
        today: str | None = None,
    ) -> Entry:
        """Mark entry ``old_id`` as replaced by entry ``new_id`` and return it as marked.

        Both must be entries of this ledger and differ; ``old_id`` must not be
        replaced already. ``new_id`` gets a link back to ``old_id`` unless it
        names one already. Nothing is written when anything is refused.
        """
        self._check_id(old_id, "The superseded ID")
        self._check_id(new_id, "The superseding ID")
        if old_id == new_id:
            raise SpecfloError(f"{old_id} cannot supersede itself.")
        today = today or datetime.date.today().isoformat()
        with locked(lock_path_for(root, slug, LOCK_NAME)):
            doc = self.read(root, cfg, slug)
            old = self._active_entry(doc, old_id)
            new = next((e for e in parse_entries(doc, self.prefix) if e.id == new_id), None)
            if new is None:
                raise SpecfloError(f"No entry {new_id} in the {self.name} ledger to supersede with.")
            doc = mark_superseded(doc, old.id, new.id, self.prefix)
            if new.supersedes is None:
                doc = set_fields(doc, new.id, [(SUPERSEDES_FIELD, old.id)], self.prefix)
            self._write(root, cfg, slug, doc, today)
            return next(e for e in parse_entries(doc, self.prefix) if e.id == old_id)

    def set_fields(
        self,
        root: Path,
        cfg: SpecfloConfig,
        slug: str,
        entry_id: str,
        fields: list[tuple[str, str]],
        today: str | None = None,
    ) -> Entry:
        """Set fields of entry ``entry_id`` in place (see :func:`set_fields`) and return it.

        Refuses an unknown entry, a field the ledger owns and a multi-line
        value before anything is written.
        """
        fields = _check_fields(list(fields), f"A {self.name} entry's")
        today = today or datetime.date.today().isoformat()
        with locked(lock_path_for(root, slug, LOCK_NAME)):
            doc = self.read(root, cfg, slug)
            doc = set_fields(doc, entry_id, fields, self.prefix)
            self._write(root, cfg, slug, doc, today)
            return next(e for e in parse_entries(doc, self.prefix) if e.id == entry_id)


SOURCES = Ledger(
    "sources", "S", "Sources",
    "What the research draws on: documents, interviews, web pages and repositories.",
    "An entry is never deleted; a later source supersedes it.",
)
QUESTIONS = Ledger(
    "questions", "Q", "Questions",
    "What is still to find out, and who answers it: the user or a developer.",
    "An entry is never deleted; a question is resolved or superseded by a later one.",
)
FINDINGS = Ledger(
    "findings", "RF", "Findings",
    "One claim per entry, a fact or an inference, with the sources it cites.",
    "An entry is never deleted; a later finding supersedes it.",
)
GLOSSARY = Ledger(
    "glossary", None, "Glossary",
    "The domain's terms in the user's own words, with the words to avoid.",
    "Entries are keyed by term; a new definition replaces the old one, which the research log keeps.",
)
LEDGERS = (SOURCES, QUESTIONS, FINDINGS, GLOSSARY)


def by_name(name: str) -> Ledger:
    """The ledger called ``name``; refused when there is none."""
    for each in LEDGERS:
        if each.name == name:
            return each
    raise SpecfloError(
        f"Unknown ledger {name!r}: expected one of " + ", ".join(l.name for l in LEDGERS) + "."
    )


def source_dir(root: Path, cfg: SpecfloConfig, slug: str, source_id: str) -> Path:
    """``<project>/research/sources/<S-NN>/``, where a source's original and text are kept.

    Refuses anything but an ``S-NN`` ID, so the path stays inside the folder.
    """
    if not isinstance(source_id, str) or not _SOURCE_ID.match(source_id):
        raise SpecfloError(f"{source_id!r} is not a source ID (S-NN).")
    return research_dir(root, cfg, slug) / SOURCES_DIRNAME / source_id

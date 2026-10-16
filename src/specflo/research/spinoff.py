"""Spin a seeded project off a research project.

A spin-off is a new project, created the way ``specflo new <name> --level
<level>`` creates one (same files, same active pointer, same checkpoint), plus
a ``handoff.md`` in its folder that carries what the research hands to it:
each named source, question or finding with its ID and every field as the
ledger stores it, the research project's name and the date. The research
project itself keeps its phase and status; its log gets one ``spinoff`` entry
naming the new project, its level and the IDs, through the document's own
``append_log``.

Everything is checked before anything is created: the level, each ID, and the
new project's name. A glossary term is refused, since the handoff takes the
three minted ledgers; a superseded entry is accepted, and the handoff records
what was asked for, so a later reader sees the entry the spin-off rests on.

:func:`parse_handoff` reads a handoff back and :func:`spinoffs_of` lists the
spin-offs a research log records, so a reader needs no text parsing of its own.

The research moves on after a spin-off, and a finding it rests on may be
superseded. :func:`spinoffs_on_replaced` names, for the recap, each spin-off
that rests on a replaced entry; :func:`handoff_notice` gives the spun-off
project's status one line saying so. Both read the ledgers as they are now
and write nothing.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .. import config as config_module
from .. import projects
from ..config import SpecfloConfig
from ..errors import SpecfloError
from ..service.resolve import local_service
from . import document
from .ledger import FINDINGS, GLOSSARY, QUESTIONS, SOURCES, Entry, Ledger

HANDOFF_FILENAME = "handoff.md"
# The kind of the log entry the research project gets for each spin-off.
SPINOFF_KIND = "spinoff"
# The levels a spin-off may start at: the ladder, not research or harden.
SPINOFF_LEVELS = projects.LEVELS

# The ledgers whose entries a handoff takes, by ID prefix.
_LEDGERS_BY_PREFIX: dict[str, Ledger] = {
    each.prefix: each for each in (SOURCES, QUESTIONS, FINDINGS)
}
_ENTRY_ID = re.compile(r"^([A-Z]+)-\d+$")
_ID_SEPARATOR = ", "
# ``Spun off <slug> at <level>: RF-01, Q-02``, the one-line text of a spinoff log entry.
_SPINOFF_LINE = re.compile(r"^Spun off (\S+) at ([a-z]+): (.*)$")
_HANDOFF_FIELDS = ("research", "research_name", "date", "entries")


@dataclass(frozen=True)
class Handoff:
    """A handoff document's frontmatter, read back."""

    research: str
    research_name: str
    date: str
    entries: tuple[str, ...]


@dataclass(frozen=True)
class Spinoff:
    """One spin-off a research log records."""

    slug: str
    level: str
    entries: tuple[str, ...]
    date: str


@dataclass(frozen=True)
class SpinOffResult:
    """What :func:`spin_off` made: the new project and its handoff."""

    slug: str
    level: str
    entries: tuple[str, ...]
    handoff_path: Path
    phase: str
    scaffolded: Path


def handoff_path(root: Path, cfg: SpecfloConfig, slug: str) -> Path:
    """``<project>/handoff.md`` of the spun-off project ``slug``."""
    return projects.project_dir(root, cfg, projects.validate_slug(slug)) / HANDOFF_FILENAME


def split_ids(values) -> list[str]:
    """The IDs of repeated ``--use`` values, each of which may hold several, comma-separated.

    Blank pieces are dropped; the order given is kept, duplicates included, so
    :func:`spin_off` can name a repeat.
    """
    ids: list[str] = []
    for value in values or ():
        ids.extend(piece.strip() for piece in (value or "").split(",") if piece.strip())
    return ids


def _ids_text(entries) -> str:
    return _ID_SEPARATOR.join(entries)


def format_spinoff(slug: str, level: str, entries) -> str:
    """The one-line text of a ``spinoff`` log entry; :func:`parse_spinoff` reads it back."""
    return f"Spun off {slug} at {level}: {_ids_text(entries)}"


def parse_spinoff(entry: document.LogEntry) -> Spinoff:
    """The spin-off a ``spinoff`` log entry records; refused for any other entry."""
    if entry.kind != SPINOFF_KIND:
        raise SpecfloError(
            f"Not a spinoff entry: its kind is {entry.kind!r}, not {SPINOFF_KIND!r}."
        )
    m = _SPINOFF_LINE.match(entry.text)
    if m is None:
        raise SpecfloError(
            f"Malformed spinoff entry of {entry.date}: expected"
            f" '{format_spinoff('<slug>', '<level>', ['<ID>'])}'."
        )
    slug, level, ids = m.groups()
    return Spinoff(
        slug=slug,
        level=level,
        entries=tuple(piece.strip() for piece in ids.split(",") if piece.strip()),
        date=entry.date,
    )


def spinoffs_of(root: Path, cfg: SpecfloConfig, research_slug: str) -> list[Spinoff]:
    """Every spin-off the research log of ``research_slug`` records, oldest first."""
    parsed = document.load(root, cfg, research_slug)
    return [parse_spinoff(entry) for entry in parsed.log if entry.kind == SPINOFF_KIND]


def replaced_entries(
    root: Path, cfg: SpecfloConfig, research_slug: str, entries
) -> list[tuple[str, str]]:
    """``(id, replacing id)`` of each ID in ``entries`` that a later entry has replaced.

    Read against the research project's ledgers as they are now, in the order
    given. An ID that names no handed-off ledger, whose ledger file is missing
    or that the ledger no longer holds is skipped: the question is which
    entries were replaced, and such an ID answers it with nothing.
    """
    found: list[tuple[str, str]] = []
    for entry_id in entries:
        match = _ENTRY_ID.match(entry_id)
        chosen = _LEDGERS_BY_PREFIX.get(match.group(1)) if match else None
        if chosen is None:
            continue
        try:
            entry = chosen.show(root, cfg, research_slug, entry_id)
        except SpecfloError:
            continue
        if entry.superseded_by is not None:
            found.append((entry.id, entry.superseded_by))
    return found


def spinoffs_on_replaced(root: Path, cfg: SpecfloConfig, research_slug: str) -> list[dict]:
    """The spin-offs of ``research_slug`` that rest on a replaced entry, oldest first.

    Each is ``{"slug", "level", "date", "replaced": [{"id", "by"}, ...]}``; a
    spin-off whose entries all still stand is left out. Nothing is written.
    """
    flagged = []
    for each in spinoffs_of(root, cfg, research_slug):
        replaced = replaced_entries(root, cfg, research_slug, each.entries)
        if replaced:
            flagged.append({
                "slug": each.slug,
                "level": each.level,
                "date": each.date,
                "replaced": [{"id": old, "by": new} for old, new in replaced],
            })
    return flagged


def handoff_notice(root: Path, cfg: SpecfloConfig, slug: str) -> str | None:
    """One line for the status of project ``slug`` when its handoff rests on a replaced entry.

    None when the project has no handoff, when the research project it came
    from is gone (its folder or its research document), or when every entry
    the handoff names still stands. A handoff that cannot be read is refused.
    Nothing is written.
    """
    path = handoff_path(root, cfg, slug)
    if not path.is_file():
        return None
    try:
        handoff = parse_handoff(path.read_text())
    except (OSError, UnicodeDecodeError) as exc:
        raise SpecfloError(f"Cannot read {slug}/{HANDOFF_FILENAME}: {exc}.") from exc
    if not document.research_path(root, cfg, handoff.research).is_file():
        return None
    replaced = replaced_entries(root, cfg, handoff.research, handoff.entries)
    if not replaced:
        return None
    changes = _ID_SEPARATOR.join(f"{old} was replaced by {new}" for old, new in replaced)
    return (
        f"Handoff from research project {handoff.research!r}: {changes};"
        " see specflo research recap there."
    )


def _yaml_line(key: str, value: str) -> str:
    """``key: value`` as YAML writes it, quoted when the value would read otherwise."""
    return yaml.safe_dump({key: value}, sort_keys=False, default_flow_style=False).strip()


def render_handoff(research_slug: str, research_name: str, today: str, entries: list[Entry]) -> str:
    """The handoff document: its frontmatter, title, one line on what it is, and
    one section per entry in the order given, each field as a ``- Field: value`` line."""
    ids = [entry.id for entry in entries]
    head = [
        "---",
        _yaml_line("research", research_slug),
        _yaml_line("research_name", research_name),
        f"date: {today}",
        f"entries: [{_ids_text(ids)}]",
        "---",
        "",
        f"# Handoff from {research_name}",
        "",
        f"What the research project {research_slug} handed to this project on {today}:"
        " each entry below with its ID and its full text as the ledger holds it.",
        "",
    ]
    sections = []
    for entry in entries:
        heading = f"## {entry.id} - {entry.title}" if entry.title else f"## {entry.id}"
        sections.append(heading)
        sections.extend(f"- {name}: {value}" for name, value in entry.fields)
        sections.append("")
    return "".join(f"{line}\n" for line in [*head, *sections])


def parse_handoff(doc: str) -> Handoff:
    """The frontmatter of a handoff document; refused when it is missing or incomplete."""
    parts = doc.split("---", 2)
    if len(parts) < 3 or parts[0].strip():
        raise SpecfloError(f"Malformed {HANDOFF_FILENAME}: missing YAML frontmatter.")
    try:
        fields = yaml.safe_load(parts[1])
    except yaml.YAMLError as exc:
        raise SpecfloError(
            f"Malformed {HANDOFF_FILENAME}: the frontmatter is not valid YAML."
        ) from exc
    if not isinstance(fields, dict):
        raise SpecfloError(f"Malformed {HANDOFF_FILENAME}: the frontmatter is not a mapping.")
    missing = [key for key in _HANDOFF_FIELDS if fields.get(key) is None]
    if missing:
        raise SpecfloError(f"Malformed {HANDOFF_FILENAME}: missing {', '.join(missing)}.")
    entries = fields["entries"]
    if not isinstance(entries, list) or not all(isinstance(each, str) for each in entries):
        raise SpecfloError(f"Malformed {HANDOFF_FILENAME}: entries is not a list of IDs.")
    return Handoff(
        research=str(fields["research"]),
        research_name=str(fields["research_name"]),
        date=str(fields["date"]),
        entries=tuple(entries),
    )


def _check_level(level: str) -> str:
    if level not in SPINOFF_LEVELS:
        raise SpecfloError(
            f"Level {level!r} is not a spin-off level: a spin-off starts at one of"
            f" {', '.join(SPINOFF_LEVELS)}."
        )
    return level


def _resolve_entries(
    root: Path, cfg: SpecfloConfig, research_slug: str, use
) -> list[Entry]:
    """The entries ``use`` names, in order; refused on an unknown ID, a term or a repeat."""
    ids = [str(each).strip() for each in (use or ())]
    if not ids:
        raise SpecfloError(
            "A spin-off needs at least one entry to hand off: --use RF-01,Q-02 names"
            " sources (S-NN), questions (Q-NN) and findings (RF-NN)."
        )
    seen: set[str] = set()
    found: list[Entry] = []
    for entry_id in ids:
        if entry_id in seen:
            raise SpecfloError(f"{entry_id} is given twice: name each entry once.")
        seen.add(entry_id)
        match = _ENTRY_ID.match(entry_id)
        chosen = _LEDGERS_BY_PREFIX.get(match.group(1)) if match else None
        if chosen is None:
            if any(term.id == entry_id for term in GLOSSARY.entries(root, cfg, research_slug)):
                raise SpecfloError(
                    f"{entry_id!r} is a glossary term: the handoff takes sources, questions"
                    " and findings (S-NN, Q-NN, RF-NN), not terms."
                )
            raise SpecfloError(
                f"{entry_id!r} is not an entry ID: name a source (S-NN), a question (Q-NN)"
                " or a finding (RF-NN)."
            )
        found.append(chosen.show(root, cfg, research_slug, entry_id))
    return found


def spin_off(
    root: Path,
    cfg: SpecfloConfig,
    research_slug: str,
    name: str,
    level: str,
    use,
    today: str | None = None,
) -> SpinOffResult:
    """Create project ``name`` at ``level`` seeded with the entries ``use`` names.

    The level, every ID and the name are checked first, so a refusal creates
    nothing. The project is then created as ``specflo new`` creates one: through
    the local service, made active, its first document scaffolded (a brief at
    quick, a brainstorm otherwise), its checkpoint written and the index
    refreshed when there is one. ``handoff.md`` goes in its folder and the
    research project's log gets one ``spinoff`` entry; its project file is not
    touched.
    """
    level = _check_level(level)
    research = projects.load_project(root, cfg, research_slug)
    document.read_document(root, cfg, research_slug)
    entries = _resolve_entries(root, cfg, research_slug, use)
    slug = projects.slugify(name)
    hosting = config_module.hosting_remote(root, slug)
    if hosting is not None:
        raise SpecfloError(
            f"Project {slug!r} is hosted on remote {hosting!r}; pick another name."
        )
    if projects.project_dir(root, cfg, slug).exists():
        raise SpecfloError(f"Project {slug!r} already exists.")
    today = today or datetime.date.today().isoformat()

    svc = local_service(root, cfg)
    project = svc.create_project(name, level=level)
    cfg.active_project = project.slug
    config_module.save_config(root, cfg)
    if project.level == projects.QUICK_LEVEL:
        scaffolded, _ = svc.start_brief(project.slug)
    else:
        scaffolded, _ = svc.start_brainstorm(project.slug)
    path = handoff_path(root, cfg, project.slug)
    path.write_text(render_handoff(research.slug, research.name, today, entries))
    document.append_log(
        root, cfg, research.slug,
        format_spinoff(project.slug, project.level, [entry.id for entry in entries]),
        kind=SPINOFF_KIND, today=today,
    )
    svc.write_checkpoint(project.slug)
    try:
        if svc.index_exists():
            svc.write_index()
    except Exception:  # noqa: BLE001 - the index is a derived listing; its refresh never fails the spin-off
        pass
    return SpinOffResult(
        slug=project.slug,
        level=project.level,
        entries=tuple(entry.id for entry in entries),
        handoff_path=path,
        phase=project.phase,
        scaffolded=scaffolded,
    )

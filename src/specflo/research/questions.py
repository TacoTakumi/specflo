"""The research questions: what is still to find out, and who answers it.

A question is a ``Q-NN`` entry of the questions ledger. Its title is the
question text; its fields, in this order: ``By`` (user or developer), the
``Option`` lines (one per option), ``Default``, ``Why``, ``Status`` (open or
resolved) and, once resolved, ``Resolution`` and ``Resolved by``.

The user answers a question about their world; a developer answers one about
the build. A developer question needs a default, so the work goes on when
nobody answers it in time; a user question may have none, since guessing the
user's world is what the research is there to stop.

A question is never deleted. A later question supersedes it through the
ledger's own ``Superseded by`` mark, which is not a Status: an open question
is one whose Status is open and that no later question has replaced.
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
from . import document
from .ledger import QUESTIONS, Entry, parse_entries, set_fields

BY_FIELD = "By"
OPTION_FIELD = "Option"
DEFAULT_FIELD = "Default"
WHY_FIELD = "Why"
STATUS_FIELD = "Status"
RESOLUTION_FIELD = "Resolution"
RESOLVED_BY_FIELD = "Resolved by"

ANSWERERS = ("user", "developer")
OPEN = "open"
RESOLVED = "resolved"
_QUESTION_ID = re.compile(rf"{re.escape(QUESTIONS.prefix)}-\d+")


@dataclass(frozen=True)
class Question:
    """One question read out of its ledger entry, field by field."""

    id: str
    text: str
    by: str | None
    options: tuple[str, ...]
    default: str | None
    why: str | None
    status: str | None
    resolution: str | None
    resolved_by: str | None
    supersedes: str | None
    superseded_by: str | None

    @property
    def active(self) -> bool:
        return self.superseded_by is None

    @property
    def open(self) -> bool:
        """True while the question waits for an answer and no later one replaced it."""
        return self.status == OPEN and self.active


def question_of(entry: Entry) -> Question:
    """The typed view of a questions ledger entry."""
    return Question(
        id=entry.id,
        text=entry.title,
        by=entry.get(BY_FIELD),
        options=tuple(entry.all(OPTION_FIELD)),
        default=entry.get(DEFAULT_FIELD),
        why=entry.get(WHY_FIELD),
        status=entry.status,
        resolution=entry.get(RESOLUTION_FIELD),
        resolved_by=entry.get(RESOLVED_BY_FIELD),
        supersedes=entry.supersedes,
        superseded_by=entry.superseded_by,
    )


def is_open(entry: Entry) -> bool:
    """True for an entry whose Status is open and that no later entry replaced."""
    return entry.status == OPEN and entry.active


def _one_line(what: str, value: str | None, required: bool) -> str | None:
    """``value`` stripped; refused when empty and required, or when it spans lines."""
    if value is None:
        if required:
            raise SpecfloError(f"{what} is needed.")
        return None
    if not value.strip():
        raise SpecfloError(f"{what} is empty: give one line of text.")
    require_one_line(what, value)
    return value.strip()


def add_question(
    root: Path,
    cfg: SpecfloConfig,
    slug: str,
    text: str,
    by: str,
    options: tuple[str, ...] | list[str] = (),
    default: str | None = None,
    why: str | None = None,
    supersedes: str | None = None,
    today: str | None = None,
) -> Entry:
    """Record an open question and return its entry.

    ``by`` is who answers it, ``user`` or ``developer``; a developer question
    is refused without a ``default``. ``text`` and each option, the default
    and the why are one non-empty line. ``supersedes`` names the question
    this one replaces; the ledger marks the old one and links the new one in
    the same write. Everything is checked before anything is written.
    """
    text = _one_line("The question text", text, required=True)
    if by not in ANSWERERS:
        raise SpecfloError(
            f"--by {by!r} is not one of {', '.join(ANSWERERS)}: say who answers the question."
        )
    options = [_one_line("An option", option, required=True) for option in options]
    default = _one_line("The default", default, required=False)
    why = _one_line("The why", why, required=False)
    if by == "developer" and default is None:
        raise SpecfloError(
            "A developer question needs a default (--default): the work goes on with it"
            " when nobody answers in time."
        )
    fields = [(BY_FIELD, by)]
    fields.extend((OPTION_FIELD, option) for option in options)
    if default is not None:
        fields.append((DEFAULT_FIELD, default))
    if why is not None:
        fields.append((WHY_FIELD, why))
    fields.append((STATUS_FIELD, OPEN))
    return QUESTIONS.add(root, cfg, slug, text, fields, supersedes=supersedes, today=today)


def resolve_question(
    root: Path,
    cfg: SpecfloConfig,
    slug: str,
    question_id: str,
    resolution: str,
    by: str | None = None,
    today: str | None = None,
) -> Entry:
    """Resolve an open question and return its entry as resolved.

    Sets Status to resolved and records the Resolution and who resolved it.
    ``by`` defaults to the first line of the recorded usual user, or to
    ``the usual user`` when none is recorded, as a session close names who
    was present. Refuses an unknown, superseded or already resolved question,
    and an empty or multi-line resolution or name, before anything is written.
    """
    if not isinstance(question_id, str) or not _QUESTION_ID.fullmatch(question_id):
        raise SpecfloError(f"{question_id!r} is not a question ID ({QUESTIONS.prefix}-NN).")
    resolution = _one_line("The resolution", resolution, required=True)
    by = _one_line("The resolver's name", by, required=False)
    today = today or datetime.date.today().isoformat()
    with locked(lock_path_for(root, slug, document.LOCK_NAME)):
        doc = QUESTIONS.read(root, cfg, slug)
        entry = next((e for e in parse_entries(doc, QUESTIONS.prefix) if e.id == question_id), None)
        if entry is None:
            raise SpecfloError(
                f"No question {question_id} in the questions ledger of project {slug!r}."
            )
        if not entry.active:
            raise SpecfloError(
                f"{question_id} is superseded by {entry.superseded_by}: resolve that one instead."
            )
        if entry.status == RESOLVED:
            raise SpecfloError(f"{question_id} is already resolved: {entry.get(RESOLUTION_FIELD)}")
        if by is None:
            by = document.default_present(document.load(root, cfg, slug).usual_user)
        doc = set_fields(
            doc, question_id,
            [(STATUS_FIELD, RESOLVED), (RESOLUTION_FIELD, resolution), (RESOLVED_BY_FIELD, by)],
            QUESTIONS.prefix,
        )
        QUESTIONS.path(root, cfg, slug).write_text(markdown.bump_updated(doc, today))
        return next(e for e in parse_entries(doc, QUESTIONS.prefix) if e.id == question_id)


def open_questions(root: Path, cfg: SpecfloConfig, slug: str) -> list[Entry]:
    """The questions still waiting for an answer, in ledger order."""
    return [entry for entry in QUESTIONS.entries(root, cfg, slug) if is_open(entry)]

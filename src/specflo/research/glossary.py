"""The glossary: the domain's terms in the user's own words, keyed by term.

Unlike the three minted ledgers the glossary hands out no IDs and supersedes
nothing. An entry is a ``### <term>`` heading, the term itself being the ID,
with a ``- Definition:`` line and one ``- Avoid:`` line per word to avoid, in
that order. Setting a term again rewrites those two fields in place and sends
the definition it replaces, with its words to avoid, to the research log as a
``term`` entry, so nothing is lost. Both files change under the one research
lock in one call. There is no delete.

A term is matched on its trimmed text, exactly: case is part of the term, so
``Cap rate`` and ``cap rate`` are two entries.
"""

from __future__ import annotations

import datetime
from pathlib import Path

from .. import markdown
from ..config import SpecfloConfig
from ..errors import SpecfloError, require_one_line
from ..locking import lock_path_for, locked
from . import document, ledger
from .ledger import GLOSSARY, Entry

# The kind of the log entry a re-set writes.
TERM_KIND = "term"
DEFINITION_FIELD = "Definition"
AVOID_FIELD = "Avoid"


def _check_term(term: str | None) -> str:
    if term is None or not term.strip():
        raise SpecfloError("A glossary term needs a non-empty text.")
    require_one_line("A glossary term", term)
    return term.strip()


def _check_definition(definition: str | None) -> str:
    if definition is None or not definition.strip():
        raise SpecfloError("A glossary term needs a non-empty definition.")
    require_one_line("A glossary term's definition", definition)
    return definition.strip()


def _check_avoid(avoid) -> list[str]:
    """The words to avoid, trimmed, in order, each once."""
    words: list[str] = []
    for word in avoid:
        if word is None or not word.strip():
            raise SpecfloError("A word to avoid needs a non-empty text.")
        require_one_line("A word to avoid", word)
        if word.strip() not in words:
            words.append(word.strip())
    return words


def format_replaced(term: str, definition: str, avoid: list[str]) -> str:
    """The one-line text of the ``term`` log entry a re-set writes."""
    text = f"Term {term!r} was: {definition}"
    if avoid:
        text += f" (avoid: {', '.join(avoid)})"
    return text


def _find(entries: list[Entry], term: str) -> Entry | None:
    return next((entry for entry in entries if entry.id == term), None)


def terms(root: Path, cfg: SpecfloConfig, slug: str) -> list[Entry]:
    """Every term in document order; refused when the glossary is missing."""
    return GLOSSARY.entries(root, cfg, slug)


def get_term(root: Path, cfg: SpecfloConfig, slug: str, term: str | None) -> Entry | None:
    """The entry of ``term`` (trimmed, exact match), or None when the glossary has none."""
    return _find(terms(root, cfg, slug), (term or "").strip())


def _without_field(doc: str, term: str, field: str) -> str:
    """``doc`` with every ``field`` line of entry ``term`` removed.

    ``set_fields`` rewrites only the fields it is given, so the Avoid lines of
    a term re-set with no words to avoid have to go on their own. The ledger's
    block and field scanners are the package's, shared here rather than copied.
    """
    start, end, _ = ledger._block_of(doc, term, None)
    drop = {i for i, name, _ in ledger._field_lines(doc, start, end) if name == field}
    return "".join(line for i, line in enumerate(doc.splitlines(keepends=True)) if i not in drop)


def set_term(
    root: Path,
    cfg: SpecfloConfig,
    slug: str,
    term: str,
    definition: str,
    avoid=(),
    today: str | None = None,
) -> Entry:
    """Add the term, or replace its definition and words to avoid in place; return the entry.

    A new term is appended to the glossary. A term already there keeps its
    place: its Definition and Avoid lines are rewritten, and a ``term`` entry
    with the previous definition and words to avoid goes to the research log
    in the same write. Refuses an empty or multi-line term, definition or word,
    a missing glossary or research document and a glossary without its section
    before anything is written.
    """
    term = _check_term(term)
    definition = _check_definition(definition)
    words = _check_avoid(avoid)
    fields = [(DEFINITION_FIELD, definition), *((AVOID_FIELD, word) for word in words)]
    today = today or datetime.date.today().isoformat()
    with locked(lock_path_for(root, slug, document.LOCK_NAME)):
        doc = GLOSSARY.read(root, cfg, slug)
        if GLOSSARY.header not in markdown.section_headers(doc):
            raise SpecfloError(f"Malformed {slug}/{GLOSSARY.filename}: no '{GLOSSARY.header}' section.")
        previous = _find(ledger.parse_entries(doc, None), term)
        if previous is None:
            doc = markdown.append_to_section(doc, GLOSSARY.header, ledger.render_entry(term, "", fields))
            GLOSSARY.path(root, cfg, slug).write_text(markdown.bump_updated(doc, today))
            return Entry(id=term, title="", fields=tuple(fields))
        # Read the log before either file changes, so a missing document refuses both writes.
        research = document.read_document(root, cfg, slug)
        if not words:
            doc = _without_field(doc, term, AVOID_FIELD)
        doc = ledger.set_fields(doc, term, fields, None)
        logged = document.LogEntry(
            date=today,
            kind=TERM_KIND,
            text=format_replaced(
                term, previous.get(DEFINITION_FIELD) or "(no definition)", previous.all(AVOID_FIELD)
            ),
        )
        research = document.append_line_to_section(research, document.LOG_HEADER, document._log_line(logged))
        GLOSSARY.path(root, cfg, slug).write_text(markdown.bump_updated(doc, today))
        document.research_path(root, cfg, slug).write_text(markdown.bump_updated(research, today))
    return Entry(id=term, title="", fields=tuple(fields))

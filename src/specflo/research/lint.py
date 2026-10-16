"""Research lint: each finding's figures looked up in the text of its sources.

A finding's figures are the parts of its text a source can be held to word
for word: its numbers (``4%``, ``2025``, ``3.5``, ``10km``), its dates
(``2026-10-16``, ``October 2026``, ``16 October 2026``, ``October 16, 2026``)
and its double-quoted strings (the text between ``"..."``). A figure is
grounded when it appears in the stored text of at least one cited source,
whitespace-normalised: a number by its digits, so ``4%`` is found in ``4
percent``; a date and a quoted string as written, case included. A number is
looked for with and without thousands separators, so ``1000`` is found in a
source that writes ``1,000`` and ``1,000`` in one that writes ``1000``, but
only as a whole number: ``1000`` is not found in ``10000`` or ``1000.5``. A
citation that names a page (``S-NN:pN``) is held to that page: the ``## Page
N`` section of the source's stored text, as the PDF converter writes it.

A finding with no citation has nothing to check and is skipped; so is a
superseded finding. Every function here reads and writes nothing, and no
other command depends on the lint passing: it is a check, not a gate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..config import SpecfloConfig
from ..errors import SpecfloError
from .findings import CITES_FIELD, parse_citation
from .ledger import FINDINGS, SOURCES, Entry
from .sources import source_text_path

_MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|November|December"
    "|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec"
)
# An empty pair is a quoted string too, dropped below: its marks pair with each other.
_QUOTED = re.compile(r'"([^"\n]*)"')
_DATE = re.compile(
    rf"\b(?:\d{{4}}-\d{{2}}-\d{{2}}"
    rf"|(?:\d{{1,2}} )?(?:{_MONTHS}) \d{{4}}"
    rf"|(?:{_MONTHS}) \d{{1,2}}, \d{{4}})\b",
    re.IGNORECASE,
)
# A number with its glued unit: not part of a word or identifier (v2, A4),
# with an optional percent sign or letters right after it (4%, 10km, 2nd).
_NUMBER = re.compile(r"(?<![A-Za-z0-9.])(\d+(?:,\d{3})*(?:\.\d+)?)(?:%|[A-Za-z]+)?")
_PAGE_HEADING = re.compile(r"^## Page (\d+)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Figure:
    """One checkable part of a finding: what is shown and what is looked for."""

    kind: str
    text: str
    needle: str


@dataclass(frozen=True)
class Problem:
    """A figure of a finding that none of its cited sources holds.

    ``sources`` and ``pages`` run in citation order; a page is None when the
    citation names the whole source.
    """

    finding_id: str
    figure: str
    sources: list[str]
    pages: list[int | None]

    @property
    def citations(self) -> list[str]:
        return [
            source if page is None else f"{source}:p{page}"
            for source, page in zip(self.sources, self.pages)
        ]


def _normalise(text: str) -> str:
    return " ".join(text.split())


def figures(text: str) -> list[Figure]:
    """The figures of ``text`` in document order, each once.

    Quoted strings are taken first, then dates, then numbers, each from the
    text the earlier kinds left: a number inside a quoted string or a date is
    part of that figure, not one of its own.
    """
    taken: list[tuple[int, int, Figure]] = []

    def free(start: int, end: int) -> bool:
        return all(end <= s or start >= e for s, e, _ in taken)

    for match in _QUOTED.finditer(text):
        quoted = _normalise(match.group(1))
        if quoted:
            taken.append((match.start(), match.end(), Figure("quote", quoted, quoted)))
    for match in _DATE.finditer(text):
        if free(match.start(), match.end()):
            date = _normalise(match.group(0))
            taken.append((match.start(), match.end(), Figure("date", date, date)))
    for match in _NUMBER.finditer(text):
        if free(match.start(), match.end()):
            taken.append((match.start(), match.end(), Figure("number", match.group(0), match.group(1))))
    found: list[Figure] = []
    for _, _, figure in sorted(taken, key=lambda item: item[0]):
        if figure not in found:
            found.append(figure)
    return found


def figures_of(text: str) -> list[str]:
    """The checkable strings of ``text``: its numbers, dates and quoted strings, in order."""
    return [figure.text for figure in figures(text)]


def _grouped(digits: str) -> str:
    """``digits`` with thousands separators: ``1000`` -> ``1,000``, ``1234.5`` -> ``1,234.5``."""
    whole, dot, fraction = digits.partition(".")
    groups = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    groups.insert(0, whole)
    return ",".join(groups) + dot + fraction


def _number_pattern(digits: str) -> re.Pattern[str]:
    """``digits`` as a whole number in a text, with or without thousands separators.

    The figure is looked for as written, without its separators and with them
    (``1000`` as ``1,000``, ``1,000`` as ``1000``, ``1234567`` as
    ``1,234,567``). Each form must be the whole number, not a run inside a
    longer one: ``1000`` is not in ``10000``, ``21000`` or ``1000.5``, and
    ``1,000`` is not in ``11,000``, ``1,0001`` or ``1,000,000``.
    """
    plain = digits.replace(",", "")
    forms = []
    for form in (digits, plain, _grouped(plain)):
        if form not in forms:
            forms.append(form)
    alternatives = []
    for form in forms:
        before, after = r"(?<!\d)(?<!\d\.)", r"(?!\.?\d)"
        if "," in form:
            before, after = before + r"(?<!\d,)", after + r"(?!,\d{3})"
        alternatives.append(before + re.escape(form) + after)
    return re.compile("|".join(alternatives))


def _holds(text: str, figure: Figure) -> bool:
    """True when the whitespace-normalised ``text`` holds ``figure`` word for word."""
    haystack = _normalise(text)
    if figure.kind == "number":
        return _number_pattern(figure.needle).search(haystack) is not None
    return figure.needle in haystack


def page_section(text: str, page: int) -> str:
    """The ``## Page N`` section of ``text`` for ``page``; empty when the text has none."""
    headings = list(_PAGE_HEADING.finditer(text))
    for index, heading in enumerate(headings):
        if int(heading.group(1)) == page:
            end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
            return text[heading.end():end]
    return ""


def _source_text(
    root: Path, cfg: SpecfloConfig, slug: str, source_id: str, texts: dict[str, str]
) -> str:
    """The stored text of ``source_id``, read once per run; refused when it has none."""
    if source_id not in texts:
        entry = SOURCES.show(root, cfg, slug, source_id)
        path = source_text_path(root, cfg, slug, entry)
        try:
            texts[source_id] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise SpecfloError(f"Cannot read the stored text of {source_id} at {path}: {exc}.") from exc
    return texts[source_id]


def check_finding(
    root: Path,
    cfg: SpecfloConfig,
    slug: str,
    entry: Entry,
    texts: dict[str, str] | None = None,
) -> list[Problem]:
    """The problems of one finding: each figure no cited source holds, in order.

    A citation with a page is held to that page's section. A finding with no
    citation has no problems. ``texts`` caches the source texts across calls.
    Refused when a cited source is unknown or has no stored text.
    """
    citations = [parse_citation(cite) for cite in entry.all(CITES_FIELD)]
    if not citations:
        return []
    texts = {} if texts is None else texts
    cited_texts = []
    for source_id, page in citations:
        text = _source_text(root, cfg, slug, source_id, texts)
        cited_texts.append(text if page is None else page_section(text, page))
    sources = [source_id for source_id, _ in citations]
    pages = [page for _, page in citations]
    return [
        Problem(entry.id, figure.text, sources, pages)
        for figure in figures(entry.title)
        if not any(_holds(text, figure) for text in cited_texts)
    ]


def cited_findings(root: Path, cfg: SpecfloConfig, slug: str) -> list[Entry]:
    """The active findings with at least one citation: what the lint checks."""
    return [
        entry
        for entry in FINDINGS.entries(root, cfg, slug, include_superseded=False)
        if entry.all(CITES_FIELD)
    ]


def lint(
    root: Path, cfg: SpecfloConfig, slug: str, entries: list[Entry] | None = None
) -> list[Problem]:
    """Every problem of the cited findings (or of ``entries``), in ledger order."""
    texts: dict[str, str] = {}
    if entries is None:
        entries = cited_findings(root, cfg, slug)
    return [
        problem
        for entry in entries
        for problem in check_finding(root, cfg, slug, entry, texts)
    ]


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def render(problems: list[Problem], checked: int) -> str:
    """The report: one line per problem, then one line with the counts.

    ``checked`` is how many findings with citations were looked at.
    """
    if not problems:
        if checked == 0:
            return "No findings with citations to check."
        return f"All {checked} findings with citations are grounded."
    lines = [
        f'{problem.finding_id}: "{problem.figure}" not found in {", ".join(problem.citations)}'
        for problem in problems
    ]
    failing = len({problem.finding_id for problem in problems})
    lines.append(
        f"{_plural(len(problems), 'problem')} in {failing} of {checked} findings with citations."
    )
    return "\n".join(lines)

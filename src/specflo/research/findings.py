"""The research findings: one fact or inference per entry, with checked citations.

A finding is an entry of the findings ledger (RF-NN). Its title is the claim,
one line; its fields are ``Kind`` (fact or inference), ``Status`` (supported,
contradicted or unsure) and one ``Cites`` line per citation. A citation is a
source ID, ``S-NN``, or a page of one, ``S-NN:pN``. A fact needs at least one
citation; an inference may stand on none.

Every citation is checked against the sources ledger before anything is
written: the source must exist, and a page is accepted only on a source with
a ``Pages`` field (a PDF) and only within its page count. The ledger's own
``Superseded by`` line marks a replaced finding; ``Status`` keeps saying what
support the claim had.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..config import SpecfloConfig
from ..errors import SpecfloError, require_one_line
from .ledger import FINDINGS, SOURCES, STATUS_FIELD, Entry

KINDS = ("fact", "inference")
STATUSES = ("supported", "contradicted", "unsure")
KIND_FIELD = "Kind"
CITES_FIELD = "Cites"
PAGES_FIELD = "Pages"
FORMAT_FIELD = "Format"

_CITATION = re.compile(r"^(S-\d+)(?::p(\d+))?$")


def parse_citation(text: str) -> tuple[str, int | None]:
    """``(source_id, page)`` of a citation ``S-NN`` or ``S-NN:pN``; page is None without one.

    Refuses anything else, a page of 0 included: pages count from 1.
    """
    text = (text or "").strip()
    match = _CITATION.match(text)
    if not match:
        raise SpecfloError(
            f"{text!r} is not a citation: cite a source as S-NN, or a page of a PDF as S-NN:pN."
        )
    page = int(match.group(2)) if match.group(2) is not None else None
    if page == 0:
        raise SpecfloError(f"{text!r} is not a citation: pages count from 1.")
    return match.group(1), page


def _check_citation(citation: str, sources: dict[str, Entry]) -> str:
    """The citation, trimmed, once its source and page are known to exist."""
    source_id, page = parse_citation(citation)
    citation = citation.strip()
    source = sources.get(source_id)
    if source is None:
        raise SpecfloError(f"Citation {citation}: unknown source {source_id}.")
    if page is None:
        return citation
    pages = source.get(PAGES_FIELD)
    if pages is None:
        fmt = source.get(FORMAT_FIELD) or source.get("Kind") or "non-PDF"
        raise SpecfloError(
            f"Citation {citation}: no pages on a {fmt} source; cite {source_id} without a page."
        )
    try:
        count = int(pages)
    except ValueError as exc:
        raise SpecfloError(
            f"Citation {citation}: {source_id} records {pages!r} as its page count, not a number."
        ) from exc
    if page > count:
        raise SpecfloError(f"Citation {citation}: page {page} beyond {count} pages of {source_id}.")
    return citation


def add_finding(
    root: Path,
    cfg: SpecfloConfig,
    slug: str,
    text: str,
    kind: str,
    status: str,
    cites: list[str] | tuple[str, ...] = (),
    supersedes: str | None = None,
    today: str | None = None,
) -> Entry:
    """Record a finding in the findings ledger and return its entry.

    ``kind`` is fact or inference, ``status`` supported, contradicted or
    unsure, ``text`` the claim on one line, ``cites`` its citations (S-NN or
    S-NN:pN) and ``supersedes`` the finding this one replaces. A fact with no
    citation, a citation to a source the sources ledger does not hold, a page
    on a source without a page count and a page beyond it are each refused,
    like a bad kind, status or text, before anything is written.
    """
    if kind not in KINDS:
        raise SpecfloError(f"{kind!r} is not a finding kind: use one of " + ", ".join(KINDS) + ".")
    if status not in STATUSES:
        raise SpecfloError(
            f"{status!r} is not a finding status: use one of " + ", ".join(STATUSES) + "."
        )
    if text is None or not text.strip():
        raise SpecfloError("A finding needs its text: the claim, on one line.")
    require_one_line("A finding's text", text)
    citations = [c for c in (cites or []) if c is not None]
    if kind == "fact" and not citations:
        raise SpecfloError(
            "A fact needs at least one citation (--cites S-NN or S-NN:pN); record it as an"
            " inference when no source supports it yet."
        )
    sources = {entry.id: entry for entry in SOURCES.entries(root, cfg, slug)}
    checked = [_check_citation(citation, sources) for citation in citations]
    fields = [(KIND_FIELD, kind), (STATUS_FIELD, status), *((CITES_FIELD, c) for c in checked)]
    return FINDINGS.add(root, cfg, slug, text.strip(), fields, supersedes=supersedes, today=today)

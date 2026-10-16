"""The session recap: what an agent reads before a research session starts.

The recap gathers, from the research document and the four ledgers, what the
next session needs to know: where the research is headed and why, who the
usual user is, what the last session did and left for the next one, which
questions still wait and who answers them, how much the ledgers hold, and the
one rule that keeps the user's private world out of an outgoing web search.

:func:`build_recap` reads every file and writes none; :func:`render_recap`
turns its payload into plain ASCII text with one short label per block. The
``spinoffs_on_replaced`` block names each spin-off that rests on an entry the
research has since superseded, with the old and the new ID, so a reader learns
which of them needs another look.
"""

from __future__ import annotations

from pathlib import Path

from ..config import SpecfloConfig
from . import document, glossary, questions, spinoff
from .ledger import FINDINGS, SOURCES

# The one fixed sentence every recap ends with. An outgoing web search leaves
# the machine, so nothing of the user's world may ride along in it.
PRIVACY_RULE = (
    "Never put names, private figures or text from the project's sources in an"
    " outgoing web search."
)

# The labels of the two question groups, keyed by who answers.
_GROUP_LABELS = (("user", "for the user"), ("developer", "for a developer"))
_INDENT = "  "


def build_recap(root: Path, cfg: SpecfloConfig, slug: str) -> dict:
    """The recap of project ``slug`` as a plain payload; nothing is written.

    ``reason`` and ``usual_user`` are None when not recorded, ``last_session``
    None before the first close. The open questions are grouped by who
    answers them; a question with no recorded answerer goes to the user, since
    guessing the user's world is what the research is there to stop. The
    findings count leaves superseded findings out. ``spinoffs_on_replaced``
    holds each spin-off that rests on a replaced entry, as
    :func:`spinoff.spinoffs_on_replaced` lists them. Refused, like the reads
    it makes, when the document or a ledger is missing.
    """
    parsed = document.load(root, cfg, slug)
    session = document.last_session(parsed)
    grouped: dict[str, list[dict[str, str]]] = {by: [] for by, _ in _GROUP_LABELS}
    for entry in questions.open_questions(root, cfg, slug):
        question = questions.question_of(entry)
        by = question.by if question.by in grouped else "user"
        grouped[by].append({"id": question.id, "text": question.text})
    return {
        "destination": parsed.destination,
        "reason": parsed.reason,
        "usual_user": parsed.usual_user or None,
        "last_session": None if session is None else {
            "date": session.date,
            "summary": session.summary,
            "next_steps": session.next_steps,
            "present": session.present,
        },
        "open_questions": grouped,
        "counts": {
            "sources": len(SOURCES.entries(root, cfg, slug)),
            "findings": len(FINDINGS.entries(root, cfg, slug, include_superseded=False)),
            "terms": len(glossary.terms(root, cfg, slug)),
        },
        "spinoffs_on_replaced": spinoff.spinoffs_on_replaced(root, cfg, slug),
        "privacy_rule": PRIVACY_RULE,
    }


def _spinoff_line(item: dict) -> str:
    """``y (fast, 2026-10-16): RF-01 replaced by RF-02``, one flagged spin-off."""
    changes = ", ".join(
        f"{each['id']} replaced by {each['by']}" for each in item.get("replaced") or []
    )
    return f"{item['slug']} ({item['level']}, {item['date']}): {changes}"


def _multiline(label: str, text: str) -> list[str]:
    """``label: first line`` with every further line of ``text`` indented under it."""
    first, *rest = text.splitlines() or [""]
    return [f"{label}: {first}", *(f"{_INDENT}{line}" for line in rest)]


def render_recap(payload: dict) -> str:
    """The recap text of ``payload``, block by block, in a fixed order.

    Destination with its reason, the usual user, the last session, the open
    questions by who answers them, the counts, the spin-offs on replaced
    entries and, last, the privacy rule. Each empty state is named in words.
    """
    lines = [f"Destination: {payload['destination']}"]
    lines.append(f"{_INDENT}why: {payload.get('reason') or 'not set yet'}")
    user = payload.get("usual_user")
    lines.extend(_multiline("Usual user", user) if user else ["Usual user: not recorded yet"])
    session = payload.get("last_session")
    if session is None:
        lines.append("Last session: no session closed yet")
    else:
        lines.append(f"Last session: {session.get('date') or 'undated'}")
        lines.append(f"{_INDENT}summary: {session['summary']}")
        lines.append(f"{_INDENT}next: {session['next_steps']}")
        if session.get("present"):
            lines.append(f"{_INDENT}present: {session['present']}")
    lines.append("Open questions")
    grouped = payload.get("open_questions") or {}
    for by, label in _GROUP_LABELS:
        found = grouped.get(by) or []
        if not found:
            lines.append(f"{_INDENT}{label}: none")
            continue
        lines.append(f"{_INDENT}{label}:")
        lines.extend(f"{_INDENT * 2}{q['id']} {q['text']}" for q in found)
    counts = payload.get("counts") or {}
    lines.append(
        "Counts: sources {sources}, findings {findings}, terms {terms}".format(
            sources=counts.get("sources", 0),
            findings=counts.get("findings", 0),
            terms=counts.get("terms", 0),
        )
    )
    spinoffs = payload.get("spinoffs_on_replaced") or []
    if spinoffs:
        lines.append("Spin-offs on replaced entries:")
        lines.extend(f"{_INDENT}{_spinoff_line(item)}" for item in spinoffs)
    else:
        lines.append("Spin-offs on replaced entries: none")
    lines.append(f"Privacy: {payload.get('privacy_rule') or PRIVACY_RULE}")
    return "".join(f"{line}\n" for line in lines)

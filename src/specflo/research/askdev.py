"""The open developer questions as email text, for the user to send.

A developer question is one about the build. The developer is not at the
keyboard during a research session, so the user carries the question over by
email. :func:`render_ask_dev` turns every open developer question into a
subject line and a plain-text body: the question with its ID, its options,
the default the work goes on with when no answer comes, and why it matters,
then a request for one reply that covers all of them.

The module only reads the project and its questions ledger. It writes no
file and sends nothing: specflo has no mail of its own, and the user pastes
the text into their own client and chooses who gets it. The text is plain
ASCII with no markdown markup, wrapped to :data:`WIDTH` columns, so it reads
the same in every mail client.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from ..config import SpecfloConfig
from ..projects import load_project
from .questions import Question, open_questions, question_of

DEVELOPER = "developer"
NO_OPEN_MESSAGE = "No open developer questions."
# The column the body wraps at: under the 80 of a plain-text mail, with room
# for one level of quoting in the reply.
WIDTH = 78


def open_developer_questions(root: Path, cfg: SpecfloConfig, slug: str) -> list[Question]:
    """The open questions a developer answers, in ledger order."""
    return [
        question
        for question in map(question_of, open_questions(root, cfg, slug))
        if question.by == DEVELOPER
    ]


def subject_line(name: str, count: int) -> str:
    """The subject: the research project's name and how many questions are open."""
    return f"Research questions for {name} ({count} open)"


def _wrap(text: str, first: str, rest: str) -> str:
    """``text`` wrapped to :data:`WIDTH`, ``first`` before its first line and ``rest`` before the others.

    A word longer than the width, such as a URL, stays whole rather than
    being cut, so a reader can still follow it.
    """
    return textwrap.fill(
        text, width=WIDTH, initial_indent=first, subsequent_indent=rest,
        break_long_words=False, break_on_hyphens=False,
    )


def _paragraph(text: str) -> str:
    return _wrap(text, "", "")


def _block(number: int, question: Question) -> str:
    """One numbered block: the question, its options, its default and its why."""
    lead = f"{number}. "
    indent = " " * len(lead)
    lines = [_wrap(f"{question.id}: {question.text}", lead, indent)]
    if question.options:
        lines.append(f"{indent}Options:")
        lines.extend(_wrap(option, f"{indent}- ", f"{indent}  ") for option in question.options)
    lines.append(_wrap(question.default or "none", f"{indent}Default: ", indent))
    if question.why:
        lines.append(_wrap(question.why, f"{indent}Why: ", indent))
    return "\n".join(lines)


def render_ask_dev(root: Path, cfg: SpecfloConfig, slug: str) -> tuple[str, str]:
    """``(subject, body)`` for every open developer question of project ``slug``.

    With none open the subject is empty and the body is :data:`NO_OPEN_MESSAGE`,
    one line the caller prints on its own. Reads the project and the questions
    ledger; refused, as they are, when either is missing.
    """
    found = open_developer_questions(root, cfg, slug)
    if not found:
        return "", NO_OPEN_MESSAGE
    name = load_project(root, cfg, slug).name
    count = len(found)
    noun = "question" if count == 1 else "questions"
    opening = _paragraph(
        f"The research project {name} has {count} open {noun} for a developer."
        " Each one lists the options we see, the default the work goes on with when"
        " no answer comes, and why it matters."
    )
    closing = _paragraph(
        "Please reply to all of them in one sitting, with the question ID next to"
        " each answer. An answer of 'default' keeps the default."
    )
    blocks = [_block(number, question) for number, question in enumerate(found, start=1)]
    body = "\n\n".join(["Hello,", opening, *blocks, closing, "Thanks."])
    return subject_line(name, count), body

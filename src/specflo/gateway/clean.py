"""Cleaning the issue text the agent reads and the report the bot posts.

Issue text comes from people the gateway does not trust, and real attacks on
issue bots hid their instructions where the operator cannot see them: in HTML
comments and in zero-width and bidi characters. The agent's input is the
issue's title and body and the operator's own comments, with those removed.
Another user's comment never reaches the agent.

The report the agent writes is posted as the bot, and the agent has read that
untrusted text, so the report is cleaned too. No @mention may notify anyone,
no image or raw HTML may render (an image URL can carry data out to whoever
serves it), only http and https links stay, and the whole fits in one
comment. Every @ before a name is broken wherever it is, in code and plain
URLs too, since what looks like code or a link may not render as one; only
an @ in a link's destination is left whole, so the link keeps working.
Fenced code blocks are otherwise left as they are; everything outside them
is cleaned, and where the cleaning is not sure what a renderer would make of
something it escapes it.

Everything here is a pure function of strings: it reads no configuration and
talks to no forge.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlsplit

#: The most characters a posted report may hold, the cut note included.
REPORT_LIMIT = 60000

#: What a report that was too long ends with.
CUT_NOTE = f"\n\n_The report was cut at {REPORT_LIMIT} characters._"

# Zero-width characters (U+200B to U+200D, U+2060, U+FEFF) and bidi controls
# (U+202A to U+202E, U+2066 to U+2069).
_HIDDEN = re.compile("[\u200b-\u200d\u2060\ufeff\u202a-\u202e\u2066-\u2069]")

# What a mention's @ becomes in a report: a zero-width space after it.
_ZERO_WIDTH_MENTION = "@\u200b"

# The character references a renderer shows as an @.
_AT_REFERENCE = re.compile(r"&#0*64;|&#[xX]0*40;|&commat;")

# An HTML comment, or one that is never closed: that hides the rest of the
# text from the reader, so it goes too.
_HTML_COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)

# A fenced code block, opening and closing fence of the same character, the
# closing one at least as long. A backtick fence's info string holds no
# backtick. A fence that is never closed is not matched, so what follows it
# is cleaned like any other text.
_FENCE = re.compile(
    r"^ {0,3}(?P<f>`{3,}(?=[^`\n]*$)|~{3,})[^\n]*\n"
    r".*?"
    r"^ {0,3}(?P=f)[`~]*[ \t]*(?:\n|\Z)",
    re.DOTALL | re.MULTILINE,
)

_AUTOLINK = re.compile(r"<([A-Za-z][A-Za-z0-9+.-]*:[^<>\s]*)>")
_TAG = re.compile(r"<[A-Za-z/!?][^<>]*>")
_TAG_START = re.compile(r"<(?=[A-Za-z/!?])")

_IMAGE = re.compile(
    r"!\[[^\]]*\]"
    r"(?:\((?:[^()]|\([^()]*\))*\)|\[[^\]]*\])?"
)

# An inline link: text, then a destination (in angle brackets or not, with
# one level of balanced parentheses) and an optional title.
_LINK = re.compile(
    r"\[(?P<text>[^\]]*)\]\(\s*"
    r"(?:<(?P<angled>[^<>\n]*)>|(?P<bare>(?:[^\s()]|\([^\s()]*\))*))"
    r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^()]*\)))?\s*\)"
)
_REFERENCE = re.compile(r"^ {0,3}\[[^\]]+\]:[ \t]*<?([^\s>]*)>?.*(?:\n|\Z)", re.MULTILINE)

# A mention is an @ not after an ASCII letter or digit, then a name. An @
# after an underscore counts too, since the underscore may be emphasis that
# leaves the @ at the start of the rendered text.
_MENTION = re.compile(r"(?<![A-Za-z0-9])@(?=[A-Za-z0-9])")

# An inline link a renderer is sure to see as one, for the destination whose
# @ is left whole. It is narrower than _LINK: not after a backslash, all on
# one line, its text not a footnote's, and with no backtick, pipe, backslash
# or angle bracket that a code span, a table cell, an escape or HTML could
# use to take it apart and leave the URL as plain text.
_SURE_LINK = re.compile(
    r"(?<!\\)\[(?!\^)[^\[\]\n`|\\<>]*\]\([ \t]*"
    r"(?:<(?P<angled>[^<>\n`|\\]*)>|(?P<bare>(?:[^\s()`|\\<>]|\([^\s()`|\\<>]*\))+))"
    r"(?:[ \t]+(?:\"[^\"\n`|\\]*\"|'[^'\n`|\\]*'|\([^()\n`|\\]*\)))?[ \t]*\)"
)


def strip_hidden(text: str) -> str:
    """*text* without zero-width and bidi characters and HTML comments.

    The characters go first, so that none of them can break up a comment's
    opening and keep it from being seen as one.
    """
    return _HTML_COMMENT.sub("", _HIDDEN.sub("", text))


def build_input(
    title: str,
    body: str | None,
    comments: Iterable[tuple[object, str]],
    operator_id: object,
) -> str:
    """The text of a run's input.md.

    *comments* are ``(author id, body)`` pairs in the order they were
    written; only those by *operator_id* are kept. The ids are compared as
    text, since the configured id and the forge's id may differ in type.
    """
    parts = [f"# {strip_hidden(title).strip()}"]
    if body and body.strip():
        parts.append(strip_hidden(body).strip())
    for author, text in comments:
        if str(author) == str(operator_id):
            parts.append(f"## Comment by the operator\n\n{strip_hidden(text).strip()}")
    return "\n\n".join(parts) + "\n"


def clean_report(report: str) -> str:
    """*report* made safe to post as the bot, cut to ``REPORT_LIMIT``.

    The hidden characters go first, a zero-width space after an @ among
    them, so that a mention already broken ends with one such space, not
    two. The references to an @ are then made plain @s, so that they are
    broken with the rest.
    """
    report = _AT_REFERENCE.sub("@", _HIDDEN.sub("", report))
    pieces = []
    at = 0
    for fence in _FENCE.finditer(report):
        pieces.append(_clean_prose(report[at:fence.start()]))
        # A fence in a list item ends with the item, so what the pattern
        # takes for code may render as prose.
        pieces.append(_break_mentions(fence.group()))
        at = fence.end()
    pieces.append(_clean_prose(report[at:]))
    cleaned = "".join(pieces)
    if len(cleaned) > REPORT_LIMIT:
        cleaned = cleaned[: REPORT_LIMIT - len(CUT_NOTE)] + CUT_NOTE
    return cleaned


def _clean_prose(text: str) -> str:
    """Text outside code fences without images, HTML, non-http links or live mentions."""
    text = _HTML_COMMENT.sub("", text)
    text = _AUTOLINK.sub(lambda m: m.group(1) if _is_http(m.group(1)) else "", text)
    text = _TAG.sub("", text)
    text = _TAG_START.sub("&lt;", text)
    text = _IMAGE.sub("", text)
    # An image form the pattern above missed can no longer be one.
    text = text.replace("![", "!\\[")
    text = _LINK.sub(_keep_http_link, text)
    text = _REFERENCE.sub(lambda m: m.group() if _is_http(m.group(1)) else "", text)
    # A character put into a link's destination would break the link, and
    # an @ there is not shown, so it notifies no one.
    pieces = []
    at = 0
    for link in _SURE_LINK.finditer(text):
        start, end = link.span("angled" if link.group("angled") is not None else "bare")
        pieces.append(_break_mentions(text[at:start]))
        pieces.append(text[start:end])
        at = end
    pieces.append(_break_mentions(text[at:]))
    return "".join(pieces)


def _break_mentions(text: str) -> str:
    """*text* with a zero-width space after each mention's @.

    That notifies no one and reads the same.
    """
    return _MENTION.sub(_ZERO_WIDTH_MENTION, text)


def _keep_http_link(match: re.Match) -> str:
    """The link as written when it goes to an http or https URL, else its text."""
    url = match.group("angled") if match.group("angled") is not None else match.group("bare")
    return match.group() if _is_http(url) else match.group("text")


def _is_http(url: str) -> bool:
    return urlsplit(url.strip()).scheme.lower() in ("http", "https")

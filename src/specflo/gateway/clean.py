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
comment. Every @ before a name is broken wherever it is, in code, URLs,
link destinations and emails too: what looks like code or a link may not
render as one, and a forge's autolinker can end an email or a URL right
before an @ and read what follows afresh. So a link whose URL holds an @
gets the zero-width space as well, and so does every email. For the same
reason fenced code is cleaned like the rest: a fence in a list item
ends with the item, so its lines may render as prose, and raw HTML there
lets the browser decode what the cleaning did not see. The cost is that
tags, images and non-http links in a report's code blocks go too. Where the
cleaning is not sure what a renderer would make of something it escapes it.

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

#: The most passes of the markup cleaning that may change a report. A pass
#: is needed again only when what a removal joined is markup or a character
#: reference (a link reduced to its text can form a tag), which a real
#: report does once if at all; a report still changing after this many is
#: escaped instead.
_PASSES = 8

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

# A mention is an @ before a name: a letter, a digit, or an & that may be a
# character reference the renderer decodes to a letter. Nothing before the
# @ exempts it: a forge's autolinker can end an email or a URL right before
# it (a@b.cd@octocat, www.example.com@octocat), and the text after an
# autolink is a new line to Gitea's mention finder. The cost is a zero-width
# space in every email of a report. The name may also follow the @ across
# hard line breaks (a backslash, or two or more spaces, then the line end,
# then what a continuation line may start with: spaces, tabs and blockquote
# markers), since that stripper drops a hard break and joins the two lines,
# while a soft line break stays a newline there. The quantifiers are
# possessive, so that the spaces that end one line and the spaces that start
# the next are never split another way on a failed match (that backtracking
# grows exponentially with the lines); nothing is lost, since a continuation
# line of whitespace only is blank and ends the paragraph.
_HARD_BREAKS = r"(?:(?:\\|[ \t]{2,}+)\n[ \t>]*+)*+"
_MENTION = re.compile(r"@(?=" + _HARD_BREAKS + r"[A-Za-z0-9&])")


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
    two. Every carriage return then becomes a newline, since the renderer
    ends a line at a lone one too, so that the patterns here see the lines
    it does.

    The markup is then cleaned, code fences included: a fence in a list item
    ends with the item, and a stray tag start before a fence opens HTML, so
    what looks like code may render as prose, and the report must be safe
    in both readings. The pass runs again while it changes the text, since
    what a removal joins may be markup again (a link reduced to its text
    can form a tag), at most ``_PASSES`` times. A report still changing
    then has every "&", "<" and "[" made a character reference instead,
    the "&" first, so that what the last pass joined holds nothing a
    renderer reads as a tag, an image, a link or a reference it decodes (a
    backslash escape could be one, after a backslash the report already
    holds).

    The mentions are broken last, so that a mention a removal joins is
    broken too. The cut comes after that and only takes characters away at
    the end, so it cannot make a mention: at worst it leaves an @ before
    the cut note's newline.
    """
    report = _HIDDEN.sub("", report).replace("\r\n", "\n").replace("\r", "\n")
    for _ in range(_PASSES):
        cleaned = _clean_markup(report)
        if cleaned == report:
            break
        report = cleaned
    else:
        report = report.replace("&", "&amp;").replace("<", "&lt;").replace("[", "&#91;")
    cleaned = _break_mentions(report)
    if len(cleaned) > REPORT_LIMIT:
        cleaned = cleaned[: REPORT_LIMIT - len(CUT_NOTE)] + CUT_NOTE
    return cleaned


def _clean_markup(text: str) -> str:
    """One pass of *text* without images, HTML or non-http links.

    The references to an @ are made plain @s first, so that one a removal
    joins out of its pieces is decoded by the next pass and broken with the
    rest. What a removal joins is not looked at again here; the caller runs
    the pass until it changes nothing.
    """
    text = _AT_REFERENCE.sub("@", text)
    text = _HTML_COMMENT.sub("", text)
    text = _AUTOLINK.sub(lambda m: m.group(1) if _is_http(m.group(1)) else "", text)
    text = _TAG.sub("", text)
    text = _TAG_START.sub("&lt;", text)
    text = _IMAGE.sub("", text)
    # An image form the pattern above missed can no longer be one.
    text = text.replace("![", "!\\[")
    text = _LINK.sub(_keep_http_link, text)
    return _REFERENCE.sub(lambda m: m.group() if _is_http(m.group(1)) else "", text)


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

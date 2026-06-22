"""Shared markdown helpers for specflo's on-disk artifacts.

Factored out of ``brainstorm.py`` so the spec artifact (and future ones) reuse a
single fence-aware parser rather than diverging copies. Every function is pure:
it takes a document string and returns a value or a new string.
"""

from __future__ import annotations

import datetime
import re


def iter_lines_with_fence(doc: str):
    """Yield ``(index, line, in_fence)`` for each line in *doc*.

    *in_fence* is True for lines inside a fenced code block (delimited by a line
    whose stripped text starts with triple backtick). The opening and closing
    fence lines themselves are yielded with ``in_fence=True`` so callers never
    need to skip them separately.
    """
    in_fence = False
    for i, line in enumerate(doc.splitlines(keepends=True)):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            yield i, line, True  # fence delimiter itself is "inside"
            continue
        yield i, line, in_fence


def strip_comments(text: str) -> str:
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


def section_body(doc: str, header: str) -> str | None:
    """Return the text under *header*, or None if the header is absent.

    Level-aware: the section ends at the next ATX header whose level is less than
    or equal to *header*'s level (so an ``## H2`` section spans its ``### H3``
    children, while an ``### H3`` stops at a sibling ``### H3`` or the next
    ``## H2``). Fence-aware: headers inside code fences are ignored.
    """
    header = header.strip()
    level = len(header) - len(header.lstrip("#"))
    lines = doc.splitlines(keepends=True)
    try:
        start = next(
            i for i, line, in_fence in iter_lines_with_fence(doc)
            if not in_fence and line.strip() == header
        )
    except StopIteration:
        return None
    end = len(lines)
    for i, line, in_fence in iter_lines_with_fence(doc):
        if i <= start or in_fence:
            continue
        stripped = line.strip()
        if stripped.startswith("#"):
            hashes = len(stripped) - len(stripped.lstrip("#"))
            if hashes <= level and stripped[hashes : hashes + 1] == " ":
                end = i
                break
    return "".join(lines[start + 1 : end])


def append_to_section(doc: str, header: str, entry: str) -> str:
    """Insert *entry* at the end of the *header* section (before the next ``## ``)."""
    lines = doc.splitlines(keepends=True)
    start = next(
        i for i, line, in_fence in iter_lines_with_fence(doc)
        if not in_fence and line.strip() == header
    )
    end = len(lines)
    for i, line, in_fence in iter_lines_with_fence(doc):
        if i > start and not in_fence and line.startswith("## "):
            end = i
            break
    lines.insert(end, entry + "\n")  # trailing blank line separates entries
    return "".join(lines)


def next_id(doc: str, prefix: str) -> str:
    """Return the next ``<prefix>NN`` id (zero-padded), scanning fence-aware.

    Existing ids are ``### <prefix>NN —`` headers outside code fences; the next
    id is max + 1, so superseded entries never cause a collision.
    """
    id_re = re.compile(rf"^### {re.escape(prefix)}(\d+) —")
    numbers = [
        int(id_re.match(line).group(1))
        for _, line, in_fence in iter_lines_with_fence(doc)
        if not in_fence and id_re.match(line)
    ]
    nxt = max(numbers) + 1 if numbers else 1
    return f"{prefix}{nxt:02d}"


def set_entry_field(doc: str, item_id: str, field: str, value: str) -> str:
    """Set the ``- {field}:`` line of the ``### {item_id} —`` entry to *value*.

    Updates the existing field line in place if present; otherwise inserts a new
    ``- {field}: {value}`` line after the entry's last ``- `` metadata line
    (before the next ``###``/``## `` header). Fence-aware via the heading scan.
    """
    lines = doc.splitlines(keepends=True)
    start = next(
        i for i, line, in_fence in iter_lines_with_fence(doc)
        if not in_fence and line.startswith(f"### {item_id} —")
    )
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("### ") or lines[i].startswith("## "):
            end = i
            break
    for i in range(start + 1, end):
        if lines[i].startswith(f"- {field}:"):
            lines[i] = f"- {field}: {value}\n"
            return "".join(lines)
    insert_at = start + 1
    for i in range(start + 1, end):
        if lines[i].startswith("- "):
            insert_at = i + 1
    lines.insert(insert_at, f"- {field}: {value}\n")
    return "".join(lines)


def clear_entry_field(doc: str, item_id: str, field: str) -> str:
    """Remove the ``- {field}:`` line from the ``### {item_id} —`` entry, if present."""
    lines = doc.splitlines(keepends=True)
    start = next(
        i for i, line, in_fence in iter_lines_with_fence(doc)
        if not in_fence and line.startswith(f"### {item_id} —")
    )
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("### ") or lines[i].startswith("## "):
            end = i
            break
    for i in range(start + 1, end):
        if lines[i].startswith(f"- {field}:"):
            del lines[i]
            break
    return "".join(lines)


def mark_superseded(doc: str, item_id: str, by_id: str) -> str:
    """Flip the ``- Status:`` line of the ``### <item_id> —`` entry to superseded."""
    return set_entry_field(doc, item_id, "Status", f"superseded by {by_id}")


def bump_updated(doc: str, today: str | None = None) -> str:
    today = today or datetime.date.today().isoformat()
    return re.sub(r"(?m)^updated:.*$", f"updated: {today}", doc, count=1)


def placeholder_issues(body: str) -> list[str]:
    """Return placeholder-text lint issues for *body* (comments already stripped)."""
    issues: list[str] = []
    for pattern in ("TBD", "TODO"):
        if re.search(rf"\b{pattern}\b", body):
            issues.append(f"placeholder text found: {pattern}")
    if "???" in body:
        issues.append("placeholder text found: ???")
    return issues

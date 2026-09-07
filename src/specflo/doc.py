"""Prose verbs over a project's artifacts: read a document, write a section.

Agents read and write artifacts through the CLI rather than opening files.
That keeps one contract whether a project's files live in the checkout or
behind a daemon: ``specflo doc show <artifact>`` names the document,
``specflo section set <artifact> <section>`` names the prose section, and the
CLI resolves where the bytes live. Managed sections (the ID-carrying entry
lists) stay behind their own verbs so no prose write can disturb an entry.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import markdown
from .brainstorm import BRAINSTORM_FILENAME
from .checkpoint import CHECKPOINT_FILENAME
from .config import SpecfloConfig
from .errors import SpecfloError
from .locking import lock_path_for, locked
from .plan import PLAN_FILENAME
from .projects import PROJECT_FILENAME, load_project
from .spec import SPEC_FILENAME

# Artifact name -> filename, in pipeline order. The name is the public
# contract; the filename is a local-mode detail.
ARTIFACTS: dict[str, str] = {
    "brainstorm": BRAINSTORM_FILENAME,
    "spec": SPEC_FILENAME,
    "plan": PLAN_FILENAME,
    "checkpoint": CHECKPOINT_FILENAME,
    "project": PROJECT_FILENAME,
}


# A review round, ``review-N``, is named by its number; there is one file per
# round, so the name maps to ``review-N.md`` directly.
_ROUND_NAME = re.compile(r"^review-\d+$")
ROUND_PATTERN = "review-<N>"

# The artifacts that carry authored prose sections. checkpoint.md is derived
# and regenerated; project.md is front matter with no sections.
PROSE_ARTIFACTS: tuple[str, ...] = ("brainstorm", "spec", "plan")

# Section title -> the verb that owns its entries. A prose write never touches
# these: the entries carry stable IDs that only their own verb may mint.
MANAGED_SECTIONS: dict[str, str] = {
    "Decisions": "specflo decision add",
    "Requirements": "specflo requirement add",
    "Tasks": "specflo task add",
    "Milestones": "specflo milestone add",
    "Pools": "specflo pool add",
}


def artifact_names() -> str:
    """The artifact names as a help line: the five documents, or a round by number."""
    return ", ".join(ARTIFACTS) + f", or {ROUND_PATTERN}"


def artifact_filename(name: str) -> str:
    """The filename behind an artifact name; unknown names are refused."""
    if name in ARTIFACTS:
        return ARTIFACTS[name]
    if _ROUND_NAME.match(name):
        return f"{name}.md"
    raise SpecfloError(f"Unknown artifact {name!r}: expected one of {artifact_names()}.")


def artifact_path(root: Path, cfg: SpecfloConfig, slug: str, name: str) -> Path:
    """Where ``name`` lives for a project held in this checkout."""
    project = load_project(root, cfg, slug)
    return project.path / artifact_filename(name)


def show_document(root: Path, cfg: SpecfloConfig, slug: str, name: str) -> str:
    """The verbatim text of one artifact of ``slug``.

    Refuses an unknown artifact name (listing the valid ones) and an artifact
    the project has not created yet.
    """
    path = artifact_path(root, cfg, slug, name)
    if not path.is_file():
        raise SpecfloError(f"Project {slug!r} has no {name} yet.")
    return path.read_text()


def _section_title(header: str) -> str:
    """``'## Current understanding'`` and ``'Current understanding'`` both name the section."""
    return header.strip().lstrip("#").strip()


def _is_h2(header: str) -> bool:
    return header.startswith("##") and not header.startswith("###")


def _managing_verb(headers: list[str], index: int) -> str | None:
    """The verb owning ``headers[index]``, or None for a header a prose write may target.

    A header is owned when its nearest enclosing H2 - itself, if it is one -
    is a managed section: the entries under such a section are the verb's,
    whatever level their headers carry.
    """
    for header in reversed(headers[: index + 1]):
        if _is_h2(header):
            return MANAGED_SECTIONS.get(_section_title(header))
    return None


def prose_sections(doc: str) -> list[str]:
    """The titles of the sections a prose write may target, in document order."""
    headers = markdown.section_headers(doc)
    return [
        _section_title(header)
        for index, header in enumerate(headers)
        if header.startswith("##") and _managing_verb(headers, index) is None
    ]


def set_section(
    root: Path,
    cfg: SpecfloConfig,
    slug: str,
    name: str,
    section: str,
    body: str,
    today: str | None = None,
) -> str:
    """Replace the body of one prose section of ``slug``'s ``name`` artifact.

    Returns the section title that was written. The header, every other
    section, and every managed entry stay byte-identical; ``updated`` is bumped.
    Refuses artifacts without prose sections, managed sections (naming the verb
    that owns them), and section titles the document does not carry.
    """
    filename = artifact_filename(name)
    if name not in PROSE_ARTIFACTS:
        raise SpecfloError(
            f"Artifact {name!r} has no prose sections to set: expected one of "
            + ", ".join(PROSE_ARTIFACTS) + "."
        )
    title = _section_title(section)
    if title in MANAGED_SECTIONS:
        raise SpecfloError(
            f"Section {title!r} is managed: use `{MANAGED_SECTIONS[title]}` "
            "instead of section set."
        )
    project = load_project(root, cfg, slug)
    path = project.path / filename
    if not path.is_file():
        raise SpecfloError(f"Project {slug!r} has no {name} yet.")
    with locked(lock_path_for(root, slug, path)):
        doc = path.read_text()
        headers = markdown.section_headers(doc)
        index = next(
            (i for i, h in enumerate(headers)
             if h.startswith("##") and _section_title(h) == title),
            None,
        )
        if index is None:
            raise SpecfloError(
                f"No section {title!r} in {slug}/{name}: expected one of "
                + ", ".join(repr(s) for s in prose_sections(doc)) + "."
            )
        verb = _managing_verb(headers, index)
        if verb is not None:
            raise SpecfloError(
                f"Section {title!r} is an entry of a managed section: use `{verb}` "
                "instead of section set."
            )
        doc = markdown.replace_section_body(doc, headers[index], body)
        doc = markdown.bump_updated(doc, today)
        path.write_text(doc)
    return title

"""Prose verbs over a project's artifacts: read a document, write a section.

Agents read and write artifacts through the CLI rather than opening files.
That keeps one contract whether a project's files live in the checkout or
behind a daemon: ``specflo doc show <artifact>`` names the document,
``specflo section set <artifact> <section>`` names the prose section, and the
CLI resolves where the bytes live. Managed sections (the ID-carrying entry
lists) stay behind their own verbs so no prose write can disturb an entry.
"""

from __future__ import annotations

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


def artifact_filename(name: str) -> str:
    """The filename behind an artifact name; unknown names are refused."""
    try:
        return ARTIFACTS[name]
    except KeyError:
        raise SpecfloError(
            f"Unknown artifact {name!r}: expected one of " + ", ".join(ARTIFACTS) + "."
        ) from None


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


def prose_sections(doc: str) -> list[str]:
    """The titles of the sections a prose write may target, in document order."""
    return [
        _section_title(header)
        for header in markdown.section_headers(doc)
        if header.startswith("##") and _section_title(header) not in MANAGED_SECTIONS
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
        header = next(
            (h for h in markdown.section_headers(doc)
             if h.startswith("##") and _section_title(h) == title),
            None,
        )
        if header is None:
            raise SpecfloError(
                f"No section {title!r} in {slug}/{name}: expected one of "
                + ", ".join(repr(s) for s in prose_sections(doc)) + "."
            )
        doc = markdown.replace_section_body(doc, header, body)
        doc = markdown.bump_updated(doc, today)
        path.write_text(doc)
    return title

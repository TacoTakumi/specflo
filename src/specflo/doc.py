"""Prose verbs over a project's artifacts: read a document by name.

Agents read and write artifacts through the CLI rather than opening files.
That keeps one contract whether a project's files live in the checkout or
behind a daemon: ``specflo doc show <artifact>`` names the document, and the
CLI resolves where its bytes come from.
"""

from __future__ import annotations

from pathlib import Path

from .brainstorm import BRAINSTORM_FILENAME
from .checkpoint import CHECKPOINT_FILENAME
from .config import SpecfloConfig
from .errors import SpecfloError
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

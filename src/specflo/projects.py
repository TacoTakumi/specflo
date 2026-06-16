"""Creating and reading project artifacts.

A project is a directory under the configured projects dir containing a
``project.md`` file. The file's YAML frontmatter is the source of truth for the
project's state (name, slug, created, phase, status); the body is for humans.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import SpecfloConfig
from .errors import SpecfloError

PROJECT_FILENAME = "project.md"
INITIAL_PHASE = "brainstorm"
INITIAL_STATUS = "active"


@dataclass
class Project:
    name: str
    slug: str
    created: str
    phase: str
    status: str
    path: Path


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    if not slug:
        raise SpecfloError(f"Cannot derive a project slug from {name!r}.")
    return slug


def project_dir(root: Path, cfg: SpecfloConfig, slug: str) -> Path:
    return root / cfg.projects_dir / slug


def create_project(
    root: Path, cfg: SpecfloConfig, name: str, created: str | None = None
) -> Project:
    slug = slugify(name)
    directory = project_dir(root, cfg, slug)
    if directory.exists():
        raise SpecfloError(f"Project {slug!r} already exists at {directory}.")

    project = Project(
        name=name,
        slug=slug,
        created=created or datetime.date.today().isoformat(),
        phase=INITIAL_PHASE,
        status=INITIAL_STATUS,
        path=directory,
    )
    directory.mkdir(parents=True)
    (directory / PROJECT_FILENAME).write_text(_render(project))
    return project


def load_project(root: Path, cfg: SpecfloConfig, slug: str) -> Project:
    path = project_dir(root, cfg, slug) / PROJECT_FILENAME
    if not path.is_file():
        raise SpecfloError(f"No project {slug!r} found at {path}.")
    fields = _parse_frontmatter(path.read_text())
    return Project(
        name=fields["name"],
        slug=fields["slug"],
        created=str(fields["created"]),
        phase=fields["phase"],
        status=fields["status"],
        path=path.parent,
    )


def _render(project: Project) -> str:
    frontmatter = yaml.safe_dump(
        {
            "name": project.name,
            "slug": project.slug,
            "created": project.created,
            "phase": project.phase,
            "status": project.status,
        },
        sort_keys=False,
    ).strip()
    return f"---\n{frontmatter}\n---\n\n# {project.name}\n\n_(phase: {project.phase})_\n"


def _parse_frontmatter(text: str) -> dict:
    parts = text.split("---", 2)
    if len(parts) < 3 or parts[0].strip():
        raise SpecfloError("Malformed project file: missing YAML frontmatter.")
    return yaml.safe_load(parts[1]) or {}

"""Resolve the ProjectService a CLI command performs its artifact I/O through.

Every command obtains its service here and nowhere else, so where a project's
bytes live is decided once. Today every project lives in the checkout, so
the answer is always the local service on the checkout root.
"""

from __future__ import annotations

from pathlib import Path

from ..config import SpecfloConfig
from .local import LocalProjectService
from .protocol import ProjectService


def resolve_service(root: Path, cfg: SpecfloConfig) -> ProjectService:
    """The service for the projects of the checkout at ``root``."""
    return LocalProjectService(root, cfg)

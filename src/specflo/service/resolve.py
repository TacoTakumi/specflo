"""Resolve the ProjectService a CLI command performs its artifact I/O through.

Every command obtains its service here and nowhere else, so where a project's
bytes live is decided once. A project the checkout's hosted registry maps to
a registered remote is served over HTTP by that daemon; every other project
is served in-process by the local service on the checkout root. The active
project decides, unless a command names a project of its own.
"""

from __future__ import annotations

from pathlib import Path

from ..config import SpecfloConfig, hosting_remote, load_remote
from .local import LocalProjectService
from .protocol import ProjectService
from .remote import RemoteProjectService


def local_service(root: Path, cfg: SpecfloConfig) -> ProjectService:
    """The service for the projects held in the checkout at ``root``."""
    return LocalProjectService(root, cfg)


def remote_service(root: Path, name: str) -> ProjectService:
    """The service for the projects held by the remote registered as ``name``."""
    remote = load_remote(root, name)
    return RemoteProjectService(remote.url, remote.token)


def resolve_service(
    root: Path, cfg: SpecfloConfig, slug: str | None = None
) -> ProjectService:
    """The service holding ``slug``, or the active project when no slug is given."""
    slug = cfg.active_project if slug is None else slug
    remote = hosting_remote(root, slug) if slug else None
    if remote is not None:
        return remote_service(root, remote)
    return local_service(root, cfg)

"""Promote a local project into a daemon: upload, verify, then remove.

Every file of the project directory is sent to the daemon through the
service facade. The daemon answers with the hash of each file as it wrote
it, and the client compares those against the bytes it sent. Only when
every hash matches is the local directory removed and the project recorded
as hosted; a mismatch aborts with the local copy untouched.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..config import SpecfloConfig, hosting_remote, record_hosted_project
from ..errors import SpecfloError
from .resolve import local_service, remote_service


@dataclass(frozen=True)
class Promotion:
    """What a promotion moved: the project, where to, and which files."""

    slug: str
    remote: str
    files: tuple[str, ...]


def promote_project(root: Path, cfg: SpecfloConfig, slug: str, remote: str) -> Promotion:
    """Move the local project ``slug`` onto the registered remote ``remote``.

    Refuses a project that is already hosted, one that does not exist here,
    and a remote that is not registered, before anything is sent. Refuses a
    hash mismatch after the upload, before anything local is removed.
    """
    hosting = hosting_remote(root, slug)
    if hosting is not None:
        raise SpecfloError(f"Project {slug!r} is already hosted on remote {hosting!r}.")
    local = local_service(root, cfg)
    directory = local.load_project(slug).path
    target = remote_service(root, remote)

    files = local.export_project(slug)
    expected = {name: hashlib.sha256(text.encode()).hexdigest() for name, text in files.items()}
    reported = target.import_project(slug, files)
    mismatched = sorted(name for name in expected if reported.get(name) != expected[name])
    if mismatched:
        raise SpecfloError(
            f"Hash mismatch after upload for {', '.join(mismatched)}: the local project"
            f" was left untouched. Inspect the copy on remote {remote!r} before retrying."
        )

    shutil.rmtree(directory)
    record_hosted_project(root, slug, remote)
    return Promotion(slug=slug, remote=remote, files=tuple(files))

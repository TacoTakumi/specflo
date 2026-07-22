"""Install the bundled pi extension from specflo's own package data.

The extension source ships inside the wheel at ``specflo/extension/`` and is
installed by copying that tree into one of pi's two auto-discovered extension
directories -- ``~/.pi/agent/extensions/`` (user scope) or ``<project>/.pi/
extensions/`` (project scope). pi 0.81's loader walks both without any settings
edit: a subdirectory whose ``package.json`` declares ``pi.extensions`` is picked
up as a package, so the install writes no ``packages`` entry into
``~/.pi/agent/settings.json`` and touches no pi configuration at all.

The shape mirrors agentsquire's skills installer -- harness detection, scopes, a
provenance stamp, and a staleness compare -- with the stamp written to a JSON
sidecar instead of SKILL.md frontmatter, because an extension has no manifest to
splice it into. ``source_version`` in that stamp is the running specflo version,
so an installed copy always names the specflo that produced it.

The install is a filesystem copy and nothing else: no network, no process, no
package manager. specflo's version is the extension's version; the shipped
``package.json`` carries an inert ``0.0.0`` and ``private: true`` so it can
never be published (the two version files stay the release's business).
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import __version__

#: Directory name the extension is installed under, and the name pi shows it by.
EXTENSION_NAME = "specflo"

#: Provenance sidecar written into the installed copy. Dotted so pi's loader
#: (which looks for package.json / index.ts) ignores it.
STAMP_FILENAME = ".specflo-extension.json"

#: pi's extension directories, relative to the scope root. Both are discovered
#: by pi automatically; neither requires a settings.json entry.
_SCOPE_DIRS = {
    "user": Path(".pi") / "agent" / "extensions",
    "project": Path(".pi") / "extensions",
}

#: Presence of this directory under the scope root means pi is in use, matching
#: agentsquire's pi harness markers.
_PI_MARKER = ".pi"

#: Top-level entries of the source tree that are not part of an install. The
#: end-to-end harness lives inside the package (it drives the very extension it
#: sits next to) but pi never loads it -- discovery resolves the package through
#: package.json's pi.extensions, which points into src/ only. Installing it
#: would put process-spawning test code in the user's pi directory and, worse,
#: fold it into the content hash, so editing a test would report every install
#: as stale.
_NOT_INSTALLED = frozenset({"test"})


class ExtensionInstallError(Exception):
    """The extension could not be installed as asked."""


@dataclass(frozen=True)
class InstalledExtension:
    """Where the extension landed, at which version, and what changed."""

    path: Path
    scope: str
    version: str
    content_hash: str
    #: ``installed`` (nothing was there), ``updated`` (a differing copy was
    #: replaced), or ``current`` (the copy on disk already matched).
    state: str


def extension_source() -> Path:
    """The bundled extension tree inside the installed specflo package."""
    return Path(__file__).resolve().parent / "extension"


def installable_files(path: Path) -> list[Path]:
    """The files of an extension tree that an install copies, sorted."""
    return sorted(
        p
        for p in path.rglob("*")
        if p.is_file()
        and p.relative_to(path).parts[0] not in _NOT_INSTALLED
        and p.name != STAMP_FILENAME
    )


def extension_content_hash(path: Path) -> str:
    """Deterministic hash of an extension tree, over what an install copies.

    Sorted by relative path so filesystem ordering never matters. The stamp
    sidecar is skipped, so a freshly stamped install hashes back to its own
    source value, and so are the entries an install leaves behind, so the source
    and the copy it produces hash identically.
    """
    digest = hashlib.sha256()
    for file in installable_files(path):
        relpath = file.relative_to(path).as_posix()
        digest.update(relpath.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(file.read_bytes()).digest())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def extensions_dir(scope: str, *, home: Path, project: Path) -> Path:
    """pi's extension directory for ``scope``."""
    try:
        relative = _SCOPE_DIRS[scope]
    except KeyError:
        raise ExtensionInstallError(
            f"unknown scope {scope!r}; expected one of {', '.join(_SCOPE_DIRS)}"
        ) from None
    root = home if scope == "user" else project
    return root / relative


def _detect_pi(scope: str, *, home: Path, project: Path) -> None:
    """Raise unless pi's marker directory exists under the scope's root."""
    root = home if scope == "user" else project
    if not (root / _PI_MARKER).is_dir():
        raise ExtensionInstallError(
            f"pi is not detected for {scope} scope: no {_PI_MARKER}/ under {root}. "
            "Run pi once, or install into the other scope."
        )


def installed_stamp(path: Path) -> dict | None:
    """The provenance stamp of an installed copy, or None if absent/unreadable."""
    stamp_path = path / STAMP_FILENAME
    if not stamp_path.is_file():
        return None
    try:
        stamp = json.loads(stamp_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return stamp if isinstance(stamp, dict) else None


def _remove(target: Path) -> None:
    """Remove a prior install symlink-safely: unlink a link, rmtree a real dir."""
    if target.is_symlink():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()


def install_extension(
    *,
    scope: str = "user",
    home: Path | None = None,
    project: Path | None = None,
) -> InstalledExtension:
    """Copy the bundled extension into pi's extension directory and stamp it.

    Idempotent: an existing copy whose content hash and version already match is
    left in place and reported as ``current``; anything else is replaced whole,
    so a file dropped from the source disappears from the install too.
    """
    home = Path.home() if home is None else Path(home)
    project = Path.cwd() if project is None else Path(project)

    directory = extensions_dir(scope, home=home, project=project)
    _detect_pi(scope, home=home, project=project)

    source = extension_source()
    if not source.is_dir():
        raise ExtensionInstallError(
            f"the bundled extension is missing from this specflo install ({source})"
        )
    content_hash = extension_content_hash(source)

    target = directory / EXTENSION_NAME
    # Staleness is decided by hashing the tree on disk, never by trusting the
    # stamp's recorded hash: an install edited in place still carries the stamp
    # it was written with, and believing it would leave the drift there forever.
    prior = installed_stamp(target) if target.is_dir() and not target.is_symlink() else None
    if (
        prior is not None
        and prior.get("source_version") == __version__
        and extension_content_hash(target) == content_hash
    ):
        return InstalledExtension(
            path=target,
            scope=scope,
            version=__version__,
            content_hash=content_hash,
            state="current",
        )

    state = "updated" if target.exists() or target.is_symlink() else "installed"
    _remove(target)
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(*_NOT_INSTALLED))
    (target / STAMP_FILENAME).write_text(
        json.dumps(
            {
                "installer": "specflo",
                "installer_version": __version__,
                "source_package": "specflo",
                "source_version": __version__,
                "content_hash": content_hash,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return InstalledExtension(
        path=target,
        scope=scope,
        version=__version__,
        content_hash=content_hash,
        state=state,
    )

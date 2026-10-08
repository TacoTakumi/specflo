"""The held-out archive: each level's hidden suite and reference solution, in one tarball.

Held-out tests and reference solutions are committed only inside
`bench/heldout.tar.gz`, so no run workdir can read them as plain text. A
grader extracts one level's suite into a separate grading directory after the
run ends.

Archive layout (every member is a regular file, paths are POSIX and relative):

    <level>/tests/...     the held-out pytest suite for that level
    <level>/solution/...  the reference solution, as an overlay of the files
                          that differ from the clean fixture

Applying a solution means: copy the fixture, then copy the overlay on top.

`pack` is deterministic (sorted members, fixed mtime, uid, gid, owner and mode,
a gzip header without name or time), so repacking unchanged content gives an
identical archive. `unpack` refuses absolute paths, `..` parts, links and
any other non-regular member.

To add a level: unpack to a tmp dir outside the repo, add `<level>/tests` and
`<level>/solution`, pack back, then delete the tmp dir. From the shell:

    uv run python bench/modelbench/heldout.py unpack bench/heldout.tar.gz /tmp/h
    uv run python bench/modelbench/heldout.py pack /tmp/h bench/heldout.tar.gz
    uv run python bench/modelbench/heldout.py list bench/heldout.tar.gz
"""

from __future__ import annotations

import argparse
import gzip
import io
import shutil
import sys
import tarfile
from pathlib import Path, PurePosixPath

PARTS = ("tests", "solution")
_IGNORE = shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc")


class HeldoutError(ValueError):
    """The archive or its source tree breaks the layout or is unsafe."""


def _check_name(name: str) -> PurePosixPath:
    """Return the member path, or raise if it is absolute, has `..`, or is not under a level part."""
    path = PurePosixPath(name)
    if not name or path.is_absolute() or "\\" in name or ".." in path.parts:
        raise HeldoutError(f"unsafe member path: {name!r}")
    if len(path.parts) < 3 or path.parts[1] not in PARTS:
        raise HeldoutError(f"member not under <level>/tests or <level>/solution: {name!r}")
    return path


def pack(src_dir: str | Path, archive: str | Path) -> list[str]:
    """Pack `src_dir` (laid out as `<level>/<part>/...`) into `archive`; return the member names."""
    src = Path(src_dir)
    names = []
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        if any(p in ("__pycache__", ".pytest_cache") for p in rel.parts) or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise HeldoutError(f"links are not allowed: {rel.as_posix()}")
        if path.is_file():
            names.append(_check_name(rel.as_posix()).as_posix())
    if not names:
        raise HeldoutError(f"nothing to pack in {src}")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name in sorted(names):
            data = (src / name).read_bytes()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    out = Path(archive)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
        gz.write(buf.getvalue())
    return sorted(names)


def read_members(archive: str | Path) -> dict[str, bytes]:
    """Return every member as {name: bytes}, after the safety checks."""
    members: dict[str, bytes] = {}
    with tarfile.open(archive, "r:gz") as tar:
        for info in tar.getmembers():
            name = _check_name(info.name).as_posix()
            if not info.isreg():
                raise HeldoutError(f"not a regular file: {info.name!r}")
            handle = tar.extractfile(info)
            assert handle is not None
            members[name] = handle.read()
    return members


def _write_tree(files: dict[str, bytes], dest: Path) -> list[Path]:
    root = dest.resolve()
    written = []
    for name, data in sorted(files.items()):
        target = (root / name).resolve()
        if not target.is_relative_to(root):
            raise HeldoutError(f"member escapes {root}: {name!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        written.append(target)
    return written


def unpack(archive: str | Path, dest: str | Path) -> list[Path]:
    """Extract the whole archive into `dest`; return the written paths."""
    return _write_tree(read_members(archive), Path(dest))


def list_levels(archive: str | Path) -> list[str]:
    """Return the levels in the archive, sorted."""
    return sorted({PurePosixPath(n).parts[0] for n in read_members(archive)})


def level_files(archive: str | Path, level: str, part: str) -> dict[str, bytes]:
    """Return one level's `tests` or `solution` files as {path relative to the part: bytes}."""
    if part not in PARTS:
        raise HeldoutError(f"unknown part {part!r}; expected one of {PARTS}")
    prefix = f"{level}/{part}/"
    return {n[len(prefix) :]: d for n, d in read_members(archive).items() if n.startswith(prefix)}


def extract_level(archive: str | Path, level: str, part: str, dest: str | Path) -> Path:
    """Write one level's part under `dest/<part>`, return that directory, or raise if it is empty."""
    files = level_files(archive, level, part)
    if not files:
        raise HeldoutError(f"no {part} for level {level!r} in {archive}")
    out = Path(dest) / part
    _write_tree(files, out)
    return out


def copy_fixture(fixture: str | Path, dest: str | Path) -> Path:
    """Copy the clean fixture to `dest` without caches; return `dest`."""
    shutil.copytree(fixture, dest, ignore=_IGNORE)
    return Path(dest)


def apply_solution(archive: str | Path, level: str, fixture: str | Path, dest: str | Path) -> Path:
    """Copy the fixture to `dest`, lay the level's solution overlay on top, return `dest`."""
    files = level_files(archive, level, "solution")
    if not files:
        raise HeldoutError(f"no solution for level {level!r} in {archive}")
    out = copy_fixture(fixture, dest)
    _write_tree(files, out)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="heldout", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("pack", help="pack a source dir into the archive")
    p.add_argument("src")
    p.add_argument("archive")
    u = sub.add_parser("unpack", help="extract the archive into a dir")
    u.add_argument("archive")
    u.add_argument("dest")
    ls = sub.add_parser("list", help="list the levels and members")
    ls.add_argument("archive")
    args = parser.parse_args(argv)
    try:
        if args.command == "pack":
            for name in pack(args.src, args.archive):
                print(name)
        elif args.command == "unpack":
            for path in unpack(args.archive, args.dest):
                print(path)
        else:
            print("levels:", " ".join(list_levels(args.archive)))
            for name in read_members(args.archive):
                print(name)
    except (HeldoutError, OSError, tarfile.TarError) as exc:
        print(f"heldout: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

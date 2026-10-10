"""The bench pages: the model bench's results and run records, for a signed-in browser.

The bench writes under the daemon root, in ``<root>/bench``: a folder per
result batch in ``results``, each with an ``index.html`` that links only to
the charts beside it. The menu page lists the batches, newest first, for the
requester and the developer alike; a folder without an ``index.html`` is not
a batch. The developer's menu also lists the runs in ``runs``, each a folder
with a ``record.json`` and, once normalised, a ``session.norm.json``; those two
files are the developer's alone. Reading the pages changes nothing.

A file is served by a route, never by a static mount of the bench folder,
so every request passes the session check and an allow-list: a name must be
one plain path segment, a batch serves only its index page and the charts
beside it, and a run only its two record files: never its raw harness output
or the harness folder that holds its credentials. Anything else answers 404,
whether it exists or not.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import FileResponse

from ..bench.cli import HOME_DIRNAME
from .web import BENCH_PATH, current_session, render

# The one identity that reads the run files.
RUN_READER = "developer"

RESULTS_DIRNAME = "results"
INDEX_NAME = "index.html"
BATCH_FILE_PATH = BENCH_PATH + "/results/{batch}/{name}"
# What a batch serves, by suffix, with the type each is served as.
BATCH_TYPES = {".html": "text/html", ".png": "image/png", ".txt": "text/plain"}
RUNS_DIRNAME = "runs"
RUN_FILE_PATH = BENCH_PATH + "/runs/{run}/{name}"
RECORD_NAME = "record.json"
# What a run serves: its record, which every run has, and its normalised session.
RUN_FILES = (RECORD_NAME, "session.norm.json")


@dataclasses.dataclass(frozen=True)
class Batch:
    name: str
    url: str


@dataclasses.dataclass(frozen=True)
class Run:
    name: str
    # (file name, url) for each run file the folder holds.
    files: list[tuple[str, str]]


def bench_home(root: Path) -> Path:
    """Where the bench of the daemon root ``root`` writes."""
    return Path(root) / HOME_DIRNAME


def batches(root: Path) -> list[Batch]:
    """Every result batch under ``root``, newest index page first."""
    results = bench_home(root) / RESULTS_DIRNAME
    if not results.is_dir():
        return []
    found = [
        ((folder / INDEX_NAME).stat().st_mtime_ns, folder.name)
        for folder in results.iterdir()
        if folder.is_dir() and not folder.is_symlink() and (folder / INDEX_NAME).is_file()
    ]
    found.sort(reverse=True)
    return [
        Batch(name, BATCH_FILE_PATH.format(batch=quote(name, safe=""), name=INDEX_NAME))
        for _, name in found
    ]


def runs(root: Path) -> list[Run]:
    """Every run under ``root`` that holds a record, by name."""
    folder = bench_home(root) / RUNS_DIRNAME
    if not folder.is_dir():
        return []
    found = []
    for run in sorted(folder.iterdir()):
        if not run.is_dir() or run.is_symlink() or not (run / RECORD_NAME).is_file():
            continue
        found.append(Run(run.name, [
            (name, RUN_FILE_PATH.format(run=quote(run.name, safe=""), name=name))
            for name in RUN_FILES
            if (run / name).is_file()
        ]))
    return found


def plain_name(name: str) -> bool:
    """True when ``name`` is one path segment that names no folder above it."""
    return (
        name not in ("", ".", "..")
        and Path(name).name == name
        and "\\" not in name
        and "\x00" not in name
    )


def served_file(base: Path, folder: str, name: str) -> Path | None:
    """The file ``name`` directly inside the folder ``folder`` of ``base``, or None.

    Both names must be plain, and neither the folder nor the file may be a
    link: a link planted in a bench folder could serve another file, beside
    it or outside the bench, under a name the allow-list lets through. A
    name the file system cannot look up (too long, say) serves nothing.
    """
    if not (plain_name(folder) and plain_name(name)):
        return None
    inside = base / folder
    path = inside / name
    try:
        if inside.is_symlink() or path.is_symlink() or not path.is_file():
            return None
    except OSError:
        return None
    return path


def _missing(identity: str) -> Response:
    return render("missing.html", status_code=404, identity=identity, message="No such bench file.")


pages = APIRouter()


@pages.get(BENCH_PATH, include_in_schema=False)
def bench_page(request: Request, identity: str = Depends(current_session)) -> Response:
    root = request.app.state.root
    return render(
        "bench.html",
        identity=identity,
        batches=batches(root),
        runs=runs(root) if identity == RUN_READER else None,
    )


@pages.get(BATCH_FILE_PATH, include_in_schema=False)
def batch_file(
    request: Request, batch: str, name: str, identity: str = Depends(current_session)
) -> Response:
    kind = BATCH_TYPES.get(Path(name).suffix)
    results = bench_home(request.app.state.root) / RESULTS_DIRNAME
    path = served_file(results, batch, name) if kind else None
    if path is None:
        return _missing(identity)
    return FileResponse(path, media_type=kind)


@pages.get(RUN_FILE_PATH, include_in_schema=False)
def run_file(
    request: Request, run: str, name: str, identity: str = Depends(current_session)
) -> Response:
    if identity != RUN_READER:
        return render(
            "error.html", status_code=403, identity=identity,
            message="Only the developer reads the run files.",
        )
    folder = bench_home(request.app.state.root) / RUNS_DIRNAME
    path = served_file(folder, run, name) if name in RUN_FILES else None
    if path is None:
        return _missing(identity)
    return FileResponse(path, media_type="application/json")

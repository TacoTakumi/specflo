"""The bench pages: the model bench's results, for a signed-in browser.

The bench writes under the daemon root, in ``<root>/bench``: a folder per
result batch in ``results``, each with an ``index.html`` that links only to
the charts beside it. The menu page lists the batches, newest first, for the
requester and the developer alike; a folder without an ``index.html`` is not
a batch. Reading the pages changes nothing.

A file is served by a route, never by a static mount of the bench folder,
so every request passes the session check and an allow-list: a name must be
one plain path segment, and a batch serves only its index page and the
charts beside it. Anything else answers 404, whether it exists or not.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import FileResponse

from ..bench.cli import HOME_DIRNAME
from .web import BENCH_PATH, current_session, render

RESULTS_DIRNAME = "results"
INDEX_NAME = "index.html"
BATCH_FILE_PATH = BENCH_PATH + "/results/{batch}/{name}"
# What a batch serves, by suffix, with the type each is served as.
BATCH_TYPES = {".html": "text/html", ".png": "image/png", ".txt": "text/plain"}


@dataclasses.dataclass(frozen=True)
class Batch:
    name: str
    url: str


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
        if folder.is_dir() and (folder / INDEX_NAME).is_file()
    ]
    found.sort(reverse=True)
    return [
        Batch(name, BATCH_FILE_PATH.format(batch=quote(name, safe=""), name=INDEX_NAME))
        for _, name in found
    ]


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

    Both names must be plain, and the resolved file must still sit in the
    resolved folder, which must still sit in ``base``: a link planted in a
    bench folder serves nothing outside it.
    """
    if not (plain_name(folder) and plain_name(name)):
        return None
    inside = (base / folder).resolve()
    path = (inside / name).resolve()
    if inside.parent != base.resolve() or path.parent != inside or not path.is_file():
        return None
    return path


def _missing(identity: str) -> Response:
    return render("missing.html", status_code=404, identity=identity, message="No such bench file.")


pages = APIRouter()


@pages.get(BENCH_PATH, include_in_schema=False)
def bench_page(request: Request, identity: str = Depends(current_session)) -> Response:
    return render("bench.html", identity=identity, batches=batches(request.app.state.root))


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

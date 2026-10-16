"""Research projects: the document, the ledgers and what works on them.

``document`` owns ``research.md`` and its log; ``ledger`` owns the four
ledgers under ``research/`` and the IDs they mint. Every file lives inside the
project folder and only the CLI writes them.
"""

from __future__ import annotations

from pathlib import Path

from ..config import SpecfloConfig
from . import document, ledger

__all__ = ["document", "ledger", "scaffold"]


def scaffold(root: Path, cfg: SpecfloConfig, slug: str, today: str | None = None) -> list[Path]:
    """Create the research document and the four empty ledgers of project ``slug``.

    Returns the files it created, in order; a file that already exists is left
    as it is and not returned, so a second call changes nothing.
    """
    created = []
    path, new = document.start_document(root, cfg, slug, today=today)
    if new:
        created.append(path)
    for each in ledger.LEDGERS:
        path, new = each.start(root, cfg, slug, today=today)
        if new:
            created.append(path)
    return created

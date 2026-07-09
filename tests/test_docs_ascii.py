"""Published top-level docs must stay pure ASCII.

specflo's CLI output is already ASCII-only (see ``test_cli_output_stays_ascii``);
this extends the same invariant to the docs that ship in the public repo, so a
stray em dash / smart quote / arrow can't slip into README.md or CHANGELOG.md.
Keeping these ASCII makes them render identically everywhere (terminals, plain
editors, models with narrow tokenizers) with no encoding surprises.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Docs that are published to the public repo and should read cleanly anywhere.
ASCII_DOCS = ["README.md", "CHANGELOG.md"]


@pytest.mark.parametrize("name", ASCII_DOCS)
def test_published_doc_is_pure_ascii(name):
    path = REPO_ROOT / name
    text = path.read_text(encoding="utf-8")
    non_ascii = sorted({ch for ch in text if ord(ch) > 0x7F})
    assert not non_ascii, (
        f"{name} contains non-ASCII characters: "
        + ", ".join(f"{ch!r} (U+{ord(ch):04X})" for ch in non_ascii)
    )

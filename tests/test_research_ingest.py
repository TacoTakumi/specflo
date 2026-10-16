"""The research ingest command: a markdown or text file kept as a document source.

``specflo research ingest <file>`` stores the file byte for byte under
``research/sources/S-NN/``, stores its text beside it and records S-NN in the
sources ledger with the file's name, format, sha256 and date. The same bytes
are never stored twice: a second ingest names the entry that holds them and
writes nothing. Every refusal leaves the checkout byte-identical, and every
path the command writes is inside the project folder.
"""

import datetime
import hashlib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from specflo import config, guide
from specflo.cli import app
from specflo.errors import SpecfloError
from specflo.research import document, ingest, ledger, sources

runner = CliRunner()

TODAY = datetime.date.today().isoformat()
INGEST = ["research", "ingest"]
# Non-ASCII content, written as an escape so the test file itself stays ASCII.
NOTES = "# Lease act\n\nRenewals run five years.\nCaf\u00e9 leases differ.\n"
DIGEST = hashlib.sha256(NOTES.encode("utf-8")).hexdigest()


def _ok(args, stdin=None):
    result = runner.invoke(app, args, input=stdin)
    assert result.exit_code == 0, (args, result.output)
    return result


def _refused(args):
    result = runner.invoke(app, args)
    assert result.exit_code != 0, (args, result.output)
    assert "Traceback" not in result.output, (args, result.output)
    return result.output


def _checkout(path, monkeypatch):
    path.mkdir()
    config.init_config(path)
    monkeypatch.chdir(path)
    return path


def _research_project(tmp_path, monkeypatch):
    root = _checkout(tmp_path / "local", monkeypatch)
    _ok(["new", "Thing", "--level", "research"])
    return root


def _tree(directory):
    """Every file under ``directory`` with its bytes, keyed by relative path."""
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _project(root, slug="thing"):
    return root / "docs" / "projects" / slug


def _entries(root, slug="thing"):
    return ledger.SOURCES.entries(root, config.load_config(root), slug)


def _notes(tmp_path, name="notes.md"):
    path = tmp_path / name
    path.write_bytes(NOTES.encode("utf-8"))
    return path


# --- a file is stored and recorded -------------------------------------------------


@pytest.mark.parametrize("suffix", [".md", ".txt"])
def test_ingest_keeps_the_original_and_the_text_and_records_the_entry(tmp_path, monkeypatch, suffix):
    root = _research_project(tmp_path, monkeypatch)
    # Pinned, so the first source prints no egress suggestion and the output is one line.
    _ok(["egress", "local"])
    source = _notes(tmp_path, f"notes{suffix}")
    data = source.read_bytes()

    shown = _ok([*INGEST, str(source)]).output

    folder = _project(root) / "research" / "sources" / "S-01"
    assert sorted(path.name for path in folder.iterdir()) == sorted([f"notes{suffix}", "text.md"])
    assert (folder / f"notes{suffix}").read_bytes() == data
    # For markdown and text the stored text is the file's own content.
    assert (folder / "text.md").read_bytes() == data
    assert (folder / "text.md").read_text(encoding="utf-8") == NOTES
    (entry,) = _entries(root)
    assert (entry.id, entry.title) == ("S-01", f"notes{suffix}")
    assert [name for name, _ in entry.fields] == [
        "Kind", "Date", "Format", "Sha256", "Original", "Text",
    ]
    assert entry.get("Kind") == "document"
    assert entry.get("Date") == TODAY
    assert entry.get("Format") == suffix[1:]
    assert entry.get("Sha256") == DIGEST
    assert entry.get("Original") == f"research/sources/S-01/notes{suffix}"
    assert entry.get("Text") == "research/sources/S-01/text.md"
    assert "S-01" in shown
    assert f"research/sources/S-01/notes{suffix}" in shown
    assert "research/sources/S-01/text.md" in shown
    assert shown.count("\n") == 1
    # The input file is left where it was, as it was.
    assert source.read_bytes() == data


def test_a_second_file_gets_the_next_id_and_the_entries_render_in_the_contract_order(
    tmp_path, monkeypatch
):
    root = _research_project(tmp_path, monkeypatch)
    cfg = config.load_config(root)
    first = _notes(tmp_path, "notes.md")
    second = tmp_path / "brief.txt"
    second.write_bytes(b"A plain brief.\n")

    _ok([*INGEST, str(first)])
    _ok([*INGEST, str(second)])

    entries = _entries(root)
    assert [(e.id, e.title, e.get("Format")) for e in entries] == [
        ("S-01", "notes.md", "md"), ("S-02", "brief.txt", "txt"),
    ]
    doc = ledger.SOURCES.path(root, cfg, "thing").read_text()
    assert (
        f"### S-01 - notes.md\n- Kind: document\n- Date: {TODAY}\n- Format: md\n"
        f"- Sha256: {DIGEST}\n- Original: research/sources/S-01/notes.md\n"
        "- Text: research/sources/S-01/text.md\n\n### S-02 - brief.txt\n"
    ) in doc
    assert f"updated: {TODAY}\n" in doc
    assert sources.sources(root, cfg, "thing") == entries
    assert sources.find_by_sha256(root, cfg, "thing", DIGEST) == entries[0]
    assert sources.find_by_sha256(root, cfg, "thing", DIGEST.upper()) == entries[0]
    assert sources.find_by_sha256(root, cfg, "thing", "0" * 64) is None
    assert sources.find_by_sha256(root, cfg, "thing", "") is None
    assert (_project(root) / "research" / "sources" / "S-02" / "brief.txt").read_bytes() == b"A plain brief.\n"


def test_the_suffix_is_matched_without_regard_to_case(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    source = _notes(tmp_path, "NOTES.MD")

    _ok([*INGEST, str(source)])

    (entry,) = _entries(root)
    assert (entry.title, entry.get("Format")) == ("NOTES.MD", "md")
    assert entry.get("Original") == "research/sources/S-01/NOTES.MD"


# --- the same bytes are stored once ---------------------------------------------------


def test_the_same_bytes_again_are_refused_naming_the_entry_and_nothing_changes(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    source = _notes(tmp_path, "notes.md")
    _ok([*INGEST, str(source)])
    before = _tree(root)

    shown = _refused([*INGEST, str(source)])
    assert "S-01" in shown

    renamed = tmp_path / "elsewhere" / "renamed.txt"
    renamed.parent.mkdir()
    renamed.write_bytes(source.read_bytes())
    shown = _refused([*INGEST, str(renamed)])
    assert "S-01" in shown

    assert _tree(root) == before
    assert [entry.id for entry in _entries(root)] == ["S-01"]


# --- refusals write nothing -------------------------------------------------------------


@pytest.mark.parametrize("name", ["paper.pdf", "memo.docx", "README", "image.PNG"])
def test_an_unsupported_suffix_is_refused_naming_the_supported_ones_and_writes_nothing(
    tmp_path, monkeypatch, name
):
    root = _research_project(tmp_path, monkeypatch)
    before = _tree(root)
    path = tmp_path / name
    path.write_bytes(b"%PDF not really a document\n")

    shown = _refused([*INGEST, str(path)])

    assert ".md" in shown and ".txt" in shown
    assert name in shown
    assert _tree(root) == before
    assert not (_project(root) / "research" / "sources").exists()


def test_a_missing_file_or_a_directory_is_refused_and_writes_nothing(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    before = _tree(root)
    directory = tmp_path / "folder.md"
    directory.mkdir()

    shown = _refused([*INGEST, str(tmp_path / "missing.md")])
    assert "missing.md" in shown

    shown = _refused([*INGEST, str(directory)])
    assert "folder.md" in shown

    assert _tree(root) == before


def test_a_file_that_is_not_utf_8_is_refused_before_anything_is_written(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    before = _tree(root)
    latin = tmp_path / "latin.txt"
    latin.write_bytes(b"caf\xe9 \xff\xfe lease\n")

    shown = _refused([*INGEST, str(latin)])

    assert "UTF-8" in shown and "latin.txt" in shown
    assert _tree(root) == before
    assert not (_project(root) / "research" / "sources").exists()


def test_a_file_named_like_the_stored_text_is_refused(tmp_path, monkeypatch):
    """The text of every source is stored as text.md beside the original, so an
    original of that name would be overwritten by its own text."""
    root = _research_project(tmp_path, monkeypatch)
    before = _tree(root)
    clash = _notes(tmp_path, "text.md")

    shown = _refused([*INGEST, str(clash)])

    assert "text.md" in shown
    assert _tree(root) == before


# --- every path stays inside the project folder ----------------------------------------


def test_every_stored_path_is_under_the_project_folder(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    cfg = config.load_config(root)
    before = set(_tree(root))
    source = _notes(tmp_path, "notes.md")

    _ok([*INGEST, str(source)])

    folder = _project(root).resolve()
    (entry,) = _entries(root)
    text = sources.source_text_path(root, cfg, "thing", entry)
    assert text.resolve().is_relative_to(folder)
    assert text.read_text(encoding="utf-8") == NOTES
    for field in ("Original", "Text"):
        stored = sources.project_path(root, cfg, "thing", entry.get(field))
        assert stored.resolve().is_relative_to(folder) and stored.is_file(), field
    new = set(_tree(root)) - before
    written = {path for path in new if not path.startswith(".specflo/")}
    assert written == {
        "docs/projects/thing/research/sources/S-01/notes.md",
        "docs/projects/thing/research/sources/S-01/text.md",
    }
    assert ledger.source_dir(root, cfg, "thing", "S-01").resolve().is_relative_to(folder)


@pytest.mark.parametrize(
    "text", ["../../etc/passwd", "/etc/passwd", "research/sources/S-01/../../../../x", "a\\..\\b"]
)
def test_source_text_path_refuses_a_text_field_that_leaves_the_project_folder(
    tmp_path, monkeypatch, text
):
    root = _research_project(tmp_path, monkeypatch)
    cfg = config.load_config(root)
    entry = ledger.Entry("S-09", "evil", (("Kind", "document"), ("Text", text)))

    with pytest.raises(SpecfloError):
        sources.source_text_path(root, cfg, "thing", entry)
    with pytest.raises(SpecfloError):
        sources.project_path(root, cfg, "thing", text)


def test_source_text_path_refuses_an_entry_without_stored_text(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    cfg = config.load_config(root)

    with pytest.raises(SpecfloError, match="S-09"):
        sources.source_text_path(root, cfg, "thing", ledger.Entry("S-09", "web page", (("Kind", "web"),)))


# --- the typed view and the converter seam ------------------------------------------------


def test_add_source_checks_the_kind_and_the_field_contract_before_minting(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    cfg = config.load_config(root)
    path = ledger.SOURCES.path(root, cfg, "thing")
    before = path.read_bytes()
    good = [("Date", TODAY), ("Format", "md"), ("Sha256", "a" * 64)]

    bad = [
        ("paper", good),
        ("Document", good),
        ("document", good[1:]),
        ("document", [("Date", "yesterday"), *good[1:]]),
        ("document", [("Date", "2026-13-01"), *good[1:]]),
        ("document", [good[0], ("Format", "odt"), good[2]]),
        ("document", [good[0], good[2]]),
        ("document", [good[0], good[1]]),
        ("document", [good[0], good[1], ("Sha256", "xyz")]),
        ("document", [*good, ("Url", "https://example.org")]),
        ("document", [*good, ("Colour", "red")]),
        ("document", [*good, ("Date", TODAY)]),
        ("document", [*good, ("Kind", "document")]),
        ("document", [*good, ("Text", "/etc/passwd")]),
        ("document", [*good, ("Text", "../x")]),
        ("document", [*good, ("Text", "notes/x.md")]),
        ("document", [*good, ("Pages", "0")]),
        ("web", [("Date", TODAY)]),
        ("web", [("Date", TODAY), ("Url", "https://example.org"), ("Sha256", "a" * 64)]),
        ("web", [("Date", TODAY), ("Url", "")]),
    ]
    for kind, fields in bad:
        with pytest.raises(SpecfloError):
            sources.add_source(root, cfg, "thing", "A title", kind, fields, today=TODAY)
        assert path.read_bytes() == before, (kind, fields)

    web = sources.add_source(
        root, cfg, "thing", "The statute online", "web",
        [("Url", "https://example.org/act"), ("Date", TODAY)], today=TODAY,
    )
    assert web.id == "S-01"
    assert list(web.fields) == [("Kind", "web"), ("Date", TODAY), ("Url", "https://example.org/act")]
    assert sources.KINDS == ("document", "interview", "web", "repo")
    assert sources.FORMATS == ("md", "txt", "pdf", "docx")


def test_the_converter_dispatches_on_the_suffix_and_names_the_supported_ones():
    assert ingest.SUPPORTED_SUFFIXES == (".md", ".txt")
    assert ingest.format_of(Path("a.md")) == "md"
    assert ingest.format_of(Path("a.TXT")) == "txt"
    assert ingest.convert(Path("a.txt"), b"plain\n") == ("plain\n", [])
    assert ingest.convert(Path("a.md"), NOTES.encode("utf-8")) == (NOTES, [])

    for name in ("a.pdf", "a.docx", "a"):
        with pytest.raises(SpecfloError, match=r"\.md"):
            ingest.format_of(Path(name))
        with pytest.raises(SpecfloError, match=r"\.txt"):
            ingest.convert(Path(name), b"")
    with pytest.raises(SpecfloError, match="UTF-8"):
        ingest.convert(Path("a.txt"), b"\xff\xfe")


# --- which project the command works on ------------------------------------------------


def test_ingest_refuses_a_project_without_a_research_document(tmp_path, monkeypatch):
    root = _checkout(tmp_path / "local", monkeypatch)
    _ok(["new", "Plain"])
    source = _notes(tmp_path, "notes.md")
    before = _tree(root)

    shown = _refused([*INGEST, str(source)])

    assert "plain" in shown and document.RESEARCH_FILENAME in shown
    assert _tree(root) == before


def test_ingest_needs_a_checkout_and_an_active_project(tmp_path, monkeypatch):
    source = _notes(tmp_path, "notes.md")
    monkeypatch.chdir(tmp_path)
    assert "specflo init" in _refused([*INGEST, str(source)])

    root = _checkout(tmp_path / "local", monkeypatch)
    before = _tree(root)
    assert "No active project" in _refused([*INGEST, str(source)])
    assert _tree(root) == before


# --- the guide ----------------------------------------------------------------------------


def test_the_guide_lists_research_ingest_first_among_the_research_rows(tmp_path, monkeypatch):
    rows = [entry for entry in guide.COMMANDS if entry["group"] == "research"]
    assert rows[0]["name"] == "research ingest"
    assert rows[0]["args"] == "<file>"
    assert ".md" in rows[0]["summary"] and ".txt" in rows[0]["summary"]
    assert rows[0]["summary"].isascii()

    monkeypatch.chdir(tmp_path)
    out = _ok(["guide"]).output

    assert "\n    research ingest " in out
    assert out.index("\n    followup close ") < out.index("\n    research ingest ")
    assert out.index("\n    research ingest ") < out.index("\n    research user set ")

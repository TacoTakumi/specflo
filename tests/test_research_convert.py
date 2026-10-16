"""PDF and Word documents through ``specflo research ingest`` with the docs extra.

A PDF is stored with one ``## Page N`` heading per page and its page count; a
Word document with one markdown heading per Word heading and the text under
it. The converters come from the ``docs`` extra (pypdfium2 for PDF, mammoth
and markdownify for DOCX) and are imported only when a file of that kind is
ingested: without the extra the ingest is refused naming ``specflo[docs]``
and nothing is written. A corrupt file is refused the same way. The fixtures
under ``tests/fixtures/research`` were written by hand, offline, and are
checked in.
"""

import shutil
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from specflo import config
from specflo.cli import app
from specflo.errors import SpecfloError
from specflo.research import convert, ingest, ledger

runner = CliRunner()

INGEST = ["research", "ingest"]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "research"
PDF = FIXTURES / "three-pages.pdf"
DOCX = FIXTURES / "headings.docx"
EXTRA = "specflo[docs]"
# The modules the docs extra brings; made unimportable to stand in for a core install.
DOCS_MODULES = ("pypdfium2", "mammoth", "markdownify")

PDF_TEXT = (
    "## Page 1\n\nPage one: rent rose 4 percent.\n\n"
    "## Page 2\n\nPage two: renewals run five years.\n\n"
    "## Page 3\n\nPage three: the act names the tenant.\n"
)
DOCX_TEXT = (
    "# Lease terms\n\nRenewals run five years.\n\n"
    "# Rent review\n\nRent rose 4 percent last year.\n\n"
    "## Exceptions\n\nCafe leases differ.\n"
)


def _ok(args):
    result = runner.invoke(app, args)
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


def _sources(root, slug="thing"):
    return root / "docs" / "projects" / slug / "research" / "sources"


def _entries(root, slug="thing"):
    return ledger.SOURCES.entries(root, config.load_config(root), slug)


def _copy(fixture, tmp_path, name=None):
    target = tmp_path / (name or fixture.name)
    shutil.copyfile(fixture, target)
    return target


def _without_docs_extra(monkeypatch):
    """Make the three modules unimportable, as in an install without the extra."""
    for name in DOCS_MODULES:
        monkeypatch.setitem(sys.modules, name, None)


# --- the fixtures themselves ---------------------------------------------------------


def test_the_fixtures_are_small_checked_in_files():
    assert 0 < PDF.stat().st_size < 8_000
    assert 0 < DOCX.stat().st_size < 8_000
    assert PDF.read_bytes().startswith(b"%PDF-1.4")
    assert DOCX.read_bytes().startswith(b"PK")
    assert PDF.read_bytes().isascii()


# --- a PDF is stored page by page ----------------------------------------------------


def test_ingest_stores_a_pdf_with_one_heading_per_page_and_its_page_count(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    source = _copy(PDF, tmp_path)
    data = source.read_bytes()

    shown = _ok([*INGEST, str(source)]).output

    folder = _sources(root) / "S-01"
    assert sorted(path.name for path in folder.iterdir()) == ["text.md", "three-pages.pdf"]
    assert (folder / "three-pages.pdf").read_bytes() == data
    assert (folder / "text.md").read_text(encoding="utf-8") == PDF_TEXT
    (entry,) = _entries(root)
    assert (entry.id, entry.title) == ("S-01", "three-pages.pdf")
    assert [name for name, _ in entry.fields] == [
        "Kind", "Date", "Format", "Pages", "Sha256", "Original", "Text",
    ]
    assert entry.get("Format") == "pdf"
    assert entry.get("Pages") == "3"
    assert entry.get("Original") == "research/sources/S-01/three-pages.pdf"
    assert entry.get("Text") == "research/sources/S-01/text.md"
    assert "S-01" in shown and "three-pages.pdf" in shown
    assert source.read_bytes() == data


def test_an_upper_case_pdf_suffix_converts_too(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    source = _copy(PDF, tmp_path, "THREE-PAGES.PDF")

    _ok([*INGEST, str(source)])

    (entry,) = _entries(root)
    assert (entry.title, entry.get("Format"), entry.get("Pages")) == ("THREE-PAGES.PDF", "pdf", "3")
    assert (_sources(root) / "S-01" / "text.md").read_text(encoding="utf-8") == PDF_TEXT
    assert (_sources(root) / "S-01" / "THREE-PAGES.PDF").read_bytes() == PDF.read_bytes()


# --- a Word document is stored section by section --------------------------------------


def test_ingest_stores_a_docx_with_one_markdown_heading_per_word_heading(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    source = _copy(DOCX, tmp_path)
    data = source.read_bytes()

    shown = _ok([*INGEST, str(source)]).output

    folder = _sources(root) / "S-01"
    assert sorted(path.name for path in folder.iterdir()) == ["headings.docx", "text.md"]
    assert (folder / "headings.docx").read_bytes() == data
    assert (folder / "text.md").read_text(encoding="utf-8") == DOCX_TEXT
    (entry,) = _entries(root)
    assert (entry.id, entry.title) == ("S-01", "headings.docx")
    assert [name for name, _ in entry.fields] == [
        "Kind", "Date", "Format", "Sha256", "Original", "Text",
    ]
    assert entry.get("Format") == "docx"
    assert entry.get("Pages") is None
    assert "S-01" in shown and "headings.docx" in shown


# --- without the extra, the ingest is refused and nothing is written ----------------------


@pytest.mark.parametrize("fixture", [PDF, DOCX])
def test_without_the_docs_extra_the_ingest_is_refused_naming_it_and_writes_nothing(
    tmp_path, monkeypatch, fixture
):
    root = _research_project(tmp_path, monkeypatch)
    source = _copy(fixture, tmp_path)
    before = _tree(root)
    _without_docs_extra(monkeypatch)

    shown = _refused([*INGEST, str(source)])

    assert EXTRA in shown and fixture.name in shown
    assert _tree(root) == before
    assert not _sources(root).exists()
    assert _entries(root) == []


@pytest.mark.parametrize("missing", DOCS_MODULES)
def test_each_missing_module_on_its_own_names_the_extra(monkeypatch, missing):
    monkeypatch.setitem(sys.modules, missing, None)
    path, data = (PDF, PDF.read_bytes()) if missing == "pypdfium2" else (DOCX, DOCX.read_bytes())
    converter = convert.convert_pdf if missing == "pypdfium2" else convert.convert_docx

    with pytest.raises(SpecfloError, match=r"specflo\[docs\]") as exc:
        converter(path, data)
    assert path.name in str(exc.value)


# --- a corrupt file is refused and nothing is written --------------------------------------


@pytest.mark.parametrize("name", ["broken.pdf", "broken.docx"])
def test_a_corrupt_file_is_refused_before_anything_is_written(tmp_path, monkeypatch, name):
    root = _research_project(tmp_path, monkeypatch)
    before = _tree(root)
    broken = tmp_path / name
    broken.write_bytes(bytes(range(256)) * 4)

    shown = _refused([*INGEST, str(broken)])

    assert name in shown
    assert EXTRA not in shown
    assert _tree(root) == before
    assert not _sources(root).exists()


def test_a_zip_that_is_not_a_word_document_is_refused(tmp_path, monkeypatch):
    import zipfile

    root = _research_project(tmp_path, monkeypatch)
    before = _tree(root)
    archive = tmp_path / "archive.docx"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("hello.txt", "not a document")

    shown = _refused([*INGEST, str(archive)])

    assert "archive.docx" in shown
    assert _tree(root) == before


# --- the converters on their own --------------------------------------------------------


def test_convert_pdf_returns_the_page_text_and_the_page_count():
    assert convert.convert_pdf(PDF, PDF.read_bytes()) == (PDF_TEXT, [("Pages", "3")])


def test_convert_docx_returns_markdown_with_atx_headings_and_no_extra_fields():
    assert convert.convert_docx(DOCX, DOCX.read_bytes()) == (DOCX_TEXT, [])


@pytest.mark.parametrize("converter", [convert.convert_pdf, convert.convert_docx])
def test_the_converters_refuse_garbage_with_a_clear_error(converter):
    with pytest.raises(SpecfloError, match="broken") as exc:
        converter(Path("broken.bin"), b"\x00garbage" * 50)
    assert "Traceback" not in str(exc.value)


def test_the_seam_registers_pdf_and_docx_and_names_them_in_the_refusal():
    assert ingest.SUPPORTED_SUFFIXES == (".md", ".txt", ".pdf", ".docx")
    assert ingest.CONVERTERS[".pdf"] == ("pdf", convert.convert_pdf)
    assert ingest.CONVERTERS[".docx"] == ("docx", convert.convert_docx)
    assert ingest.format_of(Path("a.pdf")) == "pdf"
    assert ingest.format_of(Path("a.DOCX")) == "docx"
    assert ingest.convert(PDF, PDF.read_bytes()) == (PDF_TEXT, [("Pages", "3")])
    with pytest.raises(SpecfloError, match=r"\.md, \.txt, \.pdf, \.docx"):
        ingest.format_of(Path("a.odt"))


def test_the_converter_module_imports_none_of_the_extra_at_import_time():
    # The core install has no converter installed, so importing the research
    # package must not import one; a converter imports its module when called.
    import importlib
    import subprocess

    code = (
        "import sys, importlib\n"
        "importlib.import_module('specflo.research.convert')\n"
        "importlib.import_module('specflo.research.ingest')\n"
        "importlib.import_module('specflo.cli')\n"
        f"loaded = [m for m in {list(DOCS_MODULES)!r} if m in sys.modules]\n"
        "print(loaded)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "[]", out.stdout
    assert importlib.import_module("specflo.research.convert") is convert

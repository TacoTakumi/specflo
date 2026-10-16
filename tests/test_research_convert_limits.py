"""The limits on ``specflo research ingest``: a size cap, a conversion timeout, no scans.

Two config keys bound a document ingest. ``research_source_limit_mb`` (50 by
default) is the largest file taken: a bigger one is refused before it is read.
``research_convert_timeout`` (120 seconds by default) bounds a conversion: a
PDF or DOCX converter runs in a child process the parent stops when the time
is up, so a runaway library call cannot hang the command; the UTF-8 decoder
for markdown and text runs inline, since it cannot hang and a fork per text
file would be waste. A PDF with no text on any page
is a scan, and a scan is refused naming OCR as the missing piece. Every one of
these refusals names its cause and comes before anything is written. The
``scan.pdf`` fixture is a hand-written one-page PDF with a drawn box and no
text operator.
"""

import os
import shutil
import time
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
SCAN = FIXTURES / "scan.pdf"
LIMIT_KEY = "research_source_limit_mb"
TIMEOUT_KEY = "research_convert_timeout"
MB = 1024 * 1024


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


def _pdf(pages):
    """A PDF with one page per entry: the entry's text, or a drawn box for None."""
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        None,
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    kids = []
    for text in pages:
        content = (
            "0.5 g 72 600 200 100 re f" if text is None
            else f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET"
        )
        page = len(objects) + 1
        objects.append(
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
            f" /Resources << /Font << /F1 3 0 R >> >> /Contents {page + 1} 0 R >>"
        )
        objects.append(f"<< /Length {len(content)} >>\nstream\n{content}\nendstream")
        kids.append(f"{page} 0 R")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>"
    out = "%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n"
    out += "".join(f"{offset:010d} 00000 n \n" for offset in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    return out.encode("ascii")


# The fake converters the timeout runner is exercised with. Module level, so
# they are importable should the runner ever have to pickle one.


def _sleeping(path, data):
    time.sleep(30)
    return "never\n", []


def _sleeping_after_noting_its_pid(path, data):
    path.with_suffix(".pid").write_text(str(os.getpid()))
    time.sleep(30)
    return "never\n", []


def _refusing(path, data):
    raise SpecfloError(f"Cannot ingest {path.name}: the fake converter said no.")


def _breaking(path, data):
    raise ValueError("the fake converter broke")


def _dying(path, data):
    os._exit(3)


def _echoing(path, data):
    return data.decode("ascii").upper(), [("Pages", "9")]


# --- the two config keys -------------------------------------------------------------


def test_the_two_keys_ship_with_their_defaults_and_round_trip_through_config(tmp_path, monkeypatch):
    root = _checkout(tmp_path / "local", monkeypatch)
    assert _ok(["config", "get", LIMIT_KEY]).output == "50\n"
    assert _ok(["config", "get", TIMEOUT_KEY]).output == "120\n"
    cfg = config.load_config(root)
    assert (cfg.research_source_limit_mb, cfg.research_convert_timeout) == (50, 120)
    listed = _ok(["config", "list"]).output.splitlines()
    assert f"{LIMIT_KEY}: 50 (default)" in listed
    assert f"{TIMEOUT_KEY}: 120 (default)" in listed

    _ok(["config", "set", LIMIT_KEY, "5"])
    _ok(["config", "set", TIMEOUT_KEY, "7"])

    assert _ok(["config", "get", LIMIT_KEY]).output == "5\n"
    assert _ok(["config", "get", TIMEOUT_KEY]).output == "7\n"
    cfg = config.load_config(root)
    assert (cfg.research_source_limit_mb, cfg.research_convert_timeout) == (5, 7)
    listed = _ok(["config", "list"]).output.splitlines()
    assert f"{LIMIT_KEY}: 5" in listed and f"{TIMEOUT_KEY}: 7" in listed
    text = config.config_path(root).read_text()
    assert f"# {config.field_for(LIMIT_KEY).description}\n{LIMIT_KEY}: 5\n" in text
    assert f"# {config.field_for(TIMEOUT_KEY).description}\n{TIMEOUT_KEY}: 7\n" in text

    _ok(["config", "unset", LIMIT_KEY])
    _ok(["config", "unset", TIMEOUT_KEY])
    cfg = config.load_config(root)
    assert (cfg.research_source_limit_mb, cfg.research_convert_timeout) == (50, 120)


def test_each_key_says_its_unit_in_one_ascii_line():
    limit = config.field_for(LIMIT_KEY)
    timeout = config.field_for(TIMEOUT_KEY)
    assert limit.type is int and limit.default == 50
    assert timeout.type is int and timeout.default == 120
    assert "MB" in limit.description and "second" in timeout.description.lower()
    for spec in (limit, timeout):
        assert spec.description.isascii() and "\n" not in spec.description
        assert str(spec.validate) == "a whole number 1 or greater"


@pytest.mark.parametrize("key", [LIMIT_KEY, TIMEOUT_KEY])
@pytest.mark.parametrize("bad", ["0", "-3", "x", "1.5", ""])
def test_a_value_that_is_not_a_positive_whole_number_is_refused_naming_the_key(
    tmp_path, monkeypatch, key, bad
):
    root = _checkout(tmp_path / "local", monkeypatch)
    before = config.config_path(root).read_bytes()

    # After `--` a negative number is a value, not an option the parser rejects.
    shown = _refused(["config", "set", key, "--", bad])

    assert key in shown and "1 or greater" in shown
    assert config.config_path(root).read_bytes() == before


def test_the_modules_name_the_keys_the_registry_has():
    # The refusal messages name the key to set; a renamed key would leave them
    # pointing at nothing, so each name is checked against the registry.
    assert config.field_for(ingest.SIZE_LIMIT_KEY).name == LIMIT_KEY
    assert config.field_for(convert.TIMEOUT_KEY).name == TIMEOUT_KEY


# --- a file over the size limit is refused before it is read -----------------------------


@pytest.mark.parametrize("name", ["big.pdf", "big.docx", "big.txt", "big.md"])
def test_a_file_over_the_limit_is_refused_naming_the_size_the_limit_and_the_key(
    tmp_path, monkeypatch, name
):
    root = _research_project(tmp_path, monkeypatch)
    _ok(["config", "set", LIMIT_KEY, "1"])
    before = _tree(root)
    big = tmp_path / name
    # Not a PDF or DOCX at all: were the file read, the converter would refuse
    # it as unreadable, so the size message proves the check came first.
    big.write_bytes(b"x" * (2 * MB))

    shown = _refused([*INGEST, str(big)])

    assert name in shown and "2.0 MB" in shown and "1 MB" in shown and LIMIT_KEY in shown
    assert "can be read" not in shown
    assert _tree(root) == before
    assert not _sources(root).exists()
    assert _entries(root) == []


def test_the_limit_is_checked_from_the_size_on_disk_before_the_bytes_are_read(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    _ok(["config", "set", LIMIT_KEY, "1"])
    cfg = config.load_config(root)
    big = tmp_path / "big.pdf"
    big.write_bytes(b"x" * (MB + 1))

    def never(self):
        raise AssertionError("the file was read")

    monkeypatch.setattr(Path, "read_bytes", never)
    with pytest.raises(SpecfloError, match=LIMIT_KEY) as exc:
        ingest.ingest_file(root, cfg, "thing", big)
    assert "1.0 MB" in str(exc.value) and "1 MB limit" in str(exc.value)


def test_a_file_at_the_limit_is_taken_and_the_default_limit_takes_a_two_mb_file(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    _ok(["config", "set", LIMIT_KEY, "1"])
    exact = tmp_path / "exact.txt"
    exact.write_bytes(b"x" * MB)

    _ok([*INGEST, str(exact)])

    _ok(["config", "unset", LIMIT_KEY])
    big = tmp_path / "big.txt"
    big.write_bytes(b"y" * (2 * MB))

    _ok([*INGEST, str(big)])

    assert [(e.id, e.title) for e in _entries(root)] == [("S-01", "exact.txt"), ("S-02", "big.txt")]
    assert (_sources(root) / "S-02" / "big.txt").stat().st_size == 2 * MB


# --- a conversion past the timeout is stopped and refused ----------------------------------


def test_a_conversion_past_the_timeout_is_refused_naming_the_timeout_and_the_key(
    tmp_path, monkeypatch
):
    root = _research_project(tmp_path, monkeypatch)
    _ok(["config", "set", TIMEOUT_KEY, "1"])
    # A registered converter needs the runner unless it is marked inline, so
    # the fake one goes through it as a PDF or DOCX converter would.
    monkeypatch.setitem(ingest.CONVERTERS, ".slow", ("txt", _sleeping))
    before = _tree(root)
    slow = tmp_path / "notes.slow"
    slow.write_bytes(b"takes a while")
    started = time.monotonic()

    shown = _refused([*INGEST, str(slow)])

    assert time.monotonic() - started < 10
    assert "notes.slow" in shown and "1 second" in shown and TIMEOUT_KEY in shown
    assert _tree(root) == before
    assert not _sources(root).exists()
    assert _entries(root) == []


def test_the_ingest_runs_a_pdf_converter_under_the_configured_timeout(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    _ok(["config", "set", TIMEOUT_KEY, "3"])
    source = _copy(PDF, tmp_path)
    seen = []

    def spy(converter, path, data, timeout):
        seen.append((converter, path.name, data, timeout))
        return converter(path, data)

    monkeypatch.setattr(ingest, "run_with_timeout", spy)

    _ok([*INGEST, str(source)])

    assert seen == [(convert.convert_pdf, "three-pages.pdf", PDF.read_bytes(), 3)]
    (entry,) = _entries(root)
    assert (entry.id, entry.get("Pages")) == ("S-01", "3")


@pytest.mark.parametrize("name", ["note.md", "note.txt"])
def test_a_markdown_or_text_ingest_runs_inline_whatever_the_timeout(tmp_path, monkeypatch, name):
    # The decoder cannot hang, so no child process is started for it: with a
    # one-second timeout set, a text file still ingests and the runner is
    # never called.
    root = _research_project(tmp_path, monkeypatch)
    _ok(["config", "set", TIMEOUT_KEY, "1"])

    def never(converter, path, data, timeout):
        raise AssertionError(f"forked for {path.name}")

    monkeypatch.setattr(ingest, "run_with_timeout", never)
    monkeypatch.setattr(convert, "run_with_timeout", never)
    note = tmp_path / name
    note.write_text("# A note\n", encoding="utf-8")

    _ok([*INGEST, str(note)])

    (entry,) = _entries(root)
    assert (entry.id, entry.title) == ("S-01", name)
    assert (_sources(root) / "S-01" / "text.md").read_text(encoding="utf-8") == "# A note\n"
    assert ingest.convert(Path(name), b"plain\n", timeout=1) == ("plain\n", [])


def test_only_the_decoder_is_marked_inline():
    decoder = ingest.CONVERTERS[".md"][1]
    assert ingest.CONVERTERS[".txt"][1] is decoder
    assert decoder.inline is True
    for suffix in (".pdf", ".docx"):
        assert not getattr(ingest.CONVERTERS[suffix][1], "inline", False), suffix
    assert not getattr(_sleeping, "inline", False)


# --- the timeout runner on its own -----------------------------------------------------


def test_the_runner_returns_what_a_converter_that_finishes_in_time_returns(tmp_path):
    path = tmp_path / "a.txt"
    assert convert.run_with_timeout(_echoing, path, b"abc", 5) == ("ABC", [("Pages", "9")])
    assert convert.run_with_timeout(convert.convert_pdf, PDF, PDF.read_bytes(), 60) == (
        convert.convert_pdf(PDF, PDF.read_bytes())
    )


def test_the_runner_stops_a_converter_that_runs_past_the_timeout(tmp_path):
    path = tmp_path / "a.txt"
    started = time.monotonic()

    with pytest.raises(SpecfloError) as exc:
        convert.run_with_timeout(_sleeping_after_noting_its_pid, path, b"", 1)

    assert time.monotonic() - started < 10
    message = str(exc.value)
    assert "a.txt" in message and "1 second" in message and TIMEOUT_KEY in message
    pid = int(path.with_suffix(".pid").read_text())
    assert pid != os.getpid()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_the_runner_pluralises_the_seconds_it_names():
    with pytest.raises(SpecfloError, match="2 seconds"):
        convert.run_with_timeout(_sleeping, Path("b.txt"), b"", 2)


def test_a_refusal_raised_in_the_child_comes_back_as_the_same_refusal():
    with pytest.raises(SpecfloError) as exc:
        convert.run_with_timeout(_refusing, Path("c.pdf"), b"", 5)
    assert str(exc.value) == "Cannot ingest c.pdf: the fake converter said no."


def test_a_converter_that_breaks_or_dies_in_the_child_is_a_refusal_naming_the_file():
    with pytest.raises(SpecfloError) as broke:
        convert.run_with_timeout(_breaking, Path("d.pdf"), b"", 5)
    assert "d.pdf" in str(broke.value) and "the fake converter broke" in str(broke.value)
    assert "Traceback" not in str(broke.value)

    with pytest.raises(SpecfloError) as died:
        convert.run_with_timeout(_dying, Path("e.pdf"), b"", 5)
    assert "e.pdf" in str(died.value) and "exit code 3" in str(died.value)


def test_the_seam_runs_inline_without_a_timeout_and_in_the_runner_with_one(monkeypatch):
    assert ingest.convert(Path("a.txt"), b"plain\n") == ("plain\n", [])
    assert ingest.convert(Path("a.txt"), b"plain\n", timeout=5) == ("plain\n", [])
    monkeypatch.setitem(ingest.CONVERTERS, ".slow", ("txt", _sleeping))
    with pytest.raises(SpecfloError, match=TIMEOUT_KEY):
        ingest.convert(Path("a.slow"), b"", timeout=1)


# --- a scanned PDF is refused ------------------------------------------------------------


def test_the_scan_fixture_is_a_small_ascii_pdf_with_no_text_operator():
    data = SCAN.read_bytes()
    assert 0 < len(data) < 1_000
    assert data.startswith(b"%PDF-1.4") and data.isascii()
    assert b"Tj" not in data and b"TJ" not in data and b"BT" not in data and b"/Font" not in data


def test_a_scanned_pdf_is_refused_naming_ocr_and_nothing_is_written(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    source = _copy(SCAN, tmp_path)
    before = _tree(root)

    shown = _refused([*INGEST, str(source)])

    assert "scan.pdf" in shown and "scanned" in shown and "not supported" in shown and "OCR" in shown
    assert _tree(root) == before
    assert not _sources(root).exists()
    assert _entries(root) == []


def test_convert_pdf_refuses_a_pdf_with_no_text_on_any_page():
    with pytest.raises(SpecfloError, match="OCR") as exc:
        convert.convert_pdf(SCAN, SCAN.read_bytes())
    assert "scan.pdf" in str(exc.value) and "not supported" in str(exc.value)

    two_boxes = _pdf([None, None])
    with pytest.raises(SpecfloError, match="scanned"):
        convert.convert_pdf(Path("boxes.pdf"), two_boxes)


def test_a_pdf_with_text_on_one_page_and_none_on_another_is_not_a_scan(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    mixed = tmp_path / "mixed.pdf"
    mixed.write_bytes(_pdf(["Only this page has text.", None]))
    expected = "## Page 1\n\nOnly this page has text.\n\n## Page 2\n"
    assert convert.convert_pdf(mixed, mixed.read_bytes()) == (expected, [("Pages", "2")])

    _ok([*INGEST, str(mixed)])

    (entry,) = _entries(root)
    assert (entry.id, entry.get("Pages")) == ("S-01", "2")
    assert (_sources(root) / "S-01" / "text.md").read_text(encoding="utf-8") == expected


# --- a normal PDF still ingests with the defaults ------------------------------------------


def test_a_normal_pdf_ingests_under_the_default_limits(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    source = _copy(PDF, tmp_path)

    shown = _ok([*INGEST, str(source)]).output

    (entry,) = _entries(root)
    assert (entry.id, entry.title, entry.get("Pages")) == ("S-01", "three-pages.pdf", "3")
    assert "S-01" in shown
    assert (_sources(root) / "S-01" / "three-pages.pdf").read_bytes() == PDF.read_bytes()
    text = (_sources(root) / "S-01" / "text.md").read_text(encoding="utf-8")
    assert text.startswith("## Page 1\n\nPage one: rent rose 4 percent.\n")

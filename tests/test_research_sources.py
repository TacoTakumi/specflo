"""The research source command: interview, web and repo sources, and the egress hint.

``specflo research source add --kind interview|web|repo --title <text>
(--file <path> | --stdin) [--url <url>]`` records a source whose text the user
gives, stores that text as given under ``research/sources/S-NN/text.md`` and
records S-NN in the sources ledger with its kind, date, title and, for a web
page or a repository, its URL. A document comes in through ``research
ingest`` instead. Every refusal leaves the checkout byte-identical.

The first source of a project that pins no egress class prints one
suggestion to pin one; it sets no pin. A later source, or a pinned project,
prints none.
"""

import datetime

import pytest
from typer.testing import CliRunner

from specflo import config, guide
from specflo.cli import app
from specflo.errors import SpecfloError
from specflo.research import ledger, sources

runner = CliRunner()

TODAY = datetime.date.today().isoformat()
SOURCE_ADD = ["research", "source", "add"]
INGEST = ["research", "ingest"]
FINDING_ADD = ["research", "finding", "add"]
# Non-ASCII content, written as an escape so the test file itself stays ASCII.
TEXT = "Q: How long do renewals run?\nA: Five years, at the caf\u00e9 as elsewhere.\n"
URL = "https://example.org/lease-act"
PIN = "`specflo egress local|no-train|open`"


def _ok(args, stdin=None):
    result = runner.invoke(app, args, input=stdin)
    assert result.exit_code == 0, (args, result.output)
    return result


def _refused(args, stdin=None):
    result = runner.invoke(app, args, input=stdin)
    assert result.exit_code != 0, (args, result.output)
    assert "Traceback" not in result.output, (args, result.output)
    return result.output


def _checkout(path, monkeypatch):
    path.mkdir()
    config.init_config(path)
    monkeypatch.chdir(path)
    return path


def _research_project(tmp_path, monkeypatch, pinned=True):
    """A research project; pinned by default so a success prints no egress hint."""
    root = _checkout(tmp_path / "local", monkeypatch)
    _ok(["new", "Thing", "--level", "research"])
    if pinned:
        _ok(["egress", "local"])
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


def _text_file(tmp_path, name="interview.md", text=TEXT):
    path = tmp_path / name
    path.write_bytes(text.encode("utf-8"))
    return path


def _add(kind, title, path, url=None):
    args = [*SOURCE_ADD, "--kind", kind, "--title", title, "--file", str(path)]
    if url is not None:
        args += ["--url", url]
    return args


# --- each kind is recorded and its text stored -------------------------------------------


@pytest.mark.parametrize(
    "kind,url", [("interview", None), ("web", URL), ("repo", "https://example.org/repo.git")]
)
def test_each_kind_is_recorded_with_its_fields_and_its_text_stored_as_given(
    tmp_path, monkeypatch, kind, url
):
    root = _research_project(tmp_path, monkeypatch)
    source = _text_file(tmp_path)
    data = source.read_bytes()

    shown = _ok(_add(kind, "Ana on renewals", source, url)).output

    folder = _project(root) / "research" / "sources" / "S-01"
    assert [path.name for path in folder.iterdir()] == ["text.md"]
    assert (folder / "text.md").read_bytes() == data
    (entry,) = _entries(root)
    assert (entry.id, entry.title) == ("S-01", "Ana on renewals")
    expected = ["Kind", "Date", "Url", "Text"] if url else ["Kind", "Date", "Text"]
    assert [name for name, _ in entry.fields] == expected
    assert entry.get("Kind") == kind
    assert entry.get("Date") == TODAY
    assert entry.get("Url") == url
    assert entry.get("Text") == "research/sources/S-01/text.md"
    assert entry.get("Original") is None
    assert "S-01" in shown
    assert "research/sources/S-01/text.md" in shown
    assert shown.count("\n") == 1
    # The input file is left where it was, as it was.
    assert source.read_bytes() == data


def test_the_text_may_come_from_standard_input(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)

    shown = _ok([*SOURCE_ADD, "--kind", "interview", "--title", "Ana", "--stdin"], stdin=TEXT).output

    text = _project(root) / "research" / "sources" / "S-01" / "text.md"
    assert text.read_bytes() == TEXT.encode("utf-8")
    (entry,) = _entries(root)
    assert (entry.get("Kind"), entry.title) == ("interview", "Ana")
    assert "S-01" in shown and "research/sources/S-01/text.md" in shown


def test_sources_of_several_kinds_get_the_next_ids_and_render_in_the_contract_order(
    tmp_path, monkeypatch
):
    root = _research_project(tmp_path, monkeypatch)
    cfg = config.load_config(root)
    _ok([*INGEST, str(_text_file(tmp_path, "notes.md"))])
    _ok(_add("interview", "Ana on renewals", _text_file(tmp_path, "ana.txt")))
    _ok(_add("web", "The act online", _text_file(tmp_path, "act.txt", "The act.\n"), URL))

    entries = _entries(root)
    assert [(e.id, e.get("Kind"), e.title) for e in entries] == [
        ("S-01", "document", "notes.md"),
        ("S-02", "interview", "Ana on renewals"),
        ("S-03", "web", "The act online"),
    ]
    doc = ledger.SOURCES.path(root, cfg, "thing").read_text()
    assert (
        f"### S-02 - Ana on renewals\n- Kind: interview\n- Date: {TODAY}\n"
        "- Text: research/sources/S-02/text.md\n\n"
        f"### S-03 - The act online\n- Kind: web\n- Date: {TODAY}\n- Url: {URL}\n"
        "- Text: research/sources/S-03/text.md\n"
    ) in doc
    for entry in entries:
        stored = sources.source_text_path(root, cfg, "thing", entry)
        assert stored.resolve().is_relative_to(_project(root).resolve()) and stored.is_file()
    assert (_project(root) / "research" / "sources" / "S-03" / "text.md").read_text() == "The act.\n"


# --- web and repo need a URL; an interview has none ----------------------------------------


@pytest.mark.parametrize("kind", ["web", "repo"])
def test_web_and_repo_without_a_url_are_refused_and_nothing_changes(tmp_path, monkeypatch, kind):
    root = _research_project(tmp_path, monkeypatch)
    source = _text_file(tmp_path)
    before = _tree(root)

    shown = _refused(_add(kind, "A page", source))

    assert "--url" in shown and kind in shown
    assert _tree(root) == before
    assert not (_project(root) / "research" / "sources").exists()
    assert _entries(root) == []


def test_an_interview_with_a_url_is_refused_and_nothing_changes(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    before = _tree(root)

    shown = _refused(_add("interview", "Ana", _text_file(tmp_path), URL))

    assert "--url" in shown and "interview" in shown
    assert _tree(root) == before


# --- other refusals write nothing ----------------------------------------------------------


def test_a_document_kind_is_refused_pointing_at_ingest(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    before = _tree(root)

    shown = _refused(_add("document", "notes.md", _text_file(tmp_path)))

    assert "research ingest" in shown
    assert _tree(root) == before

    shown = _refused(_add("paper", "notes.md", _text_file(tmp_path)))
    assert "interview" in shown and "web" in shown and "repo" in shown
    assert _tree(root) == before


def test_both_or_neither_of_file_and_stdin_are_refused(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    before = _tree(root)
    head = [*SOURCE_ADD, "--kind", "interview", "--title", "Ana"]

    shown = _refused([*head, "--file", str(_text_file(tmp_path)), "--stdin"], stdin=TEXT)
    assert "--file" in shown and "--stdin" in shown

    shown = _refused(head)
    assert "--file" in shown and "--stdin" in shown

    assert _tree(root) == before


def test_a_missing_file_or_a_directory_is_refused_and_writes_nothing(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    before = _tree(root)
    directory = tmp_path / "folder.md"
    directory.mkdir()

    shown = _refused(_add("interview", "Ana", tmp_path / "missing.md"))
    assert "missing.md" in shown

    shown = _refused(_add("interview", "Ana", directory))
    assert "folder.md" in shown

    assert _tree(root) == before


def test_a_file_that_is_not_utf_8_is_refused_before_anything_is_written(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    before = _tree(root)
    latin = tmp_path / "latin.txt"
    latin.write_bytes(b"caf\xe9 \xff\xfe lease\n")

    shown = _refused(_add("interview", "Ana", latin))

    assert "UTF-8" in shown and "latin.txt" in shown
    assert _tree(root) == before
    assert not (_project(root) / "research" / "sources").exists()


def test_an_empty_text_or_a_bad_title_is_refused(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    before = _tree(root)
    empty = tmp_path / "empty.md"
    empty.write_bytes(b"")
    blank = tmp_path / "blank.md"
    blank.write_bytes(b"  \n\n")

    assert "text" in _refused(_add("interview", "Ana", empty))
    assert "text" in _refused(_add("interview", "Ana", blank))
    assert "title" in _refused(_add("interview", "   ", _text_file(tmp_path)))
    assert "title" in _refused(_add("interview", "Ana\non two lines", _text_file(tmp_path)))
    assert _tree(root) == before


def test_add_text_source_validates_before_it_touches_the_ledger(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    cfg = config.load_config(root)
    before = _tree(root)

    bad = [
        dict(kind="document", title="Ana", text=TEXT),
        dict(kind="paper", title="Ana", text=TEXT),
        dict(kind="interview", title="", text=TEXT),
        dict(kind="interview", title="a\nb", text=TEXT),
        dict(kind="interview", title="Ana", text=""),
        dict(kind="interview", title="Ana", text="   "),
        dict(kind="interview", title="Ana", text=TEXT, url=URL),
        dict(kind="web", title="Ana", text=TEXT),
        dict(kind="web", title="Ana", text=TEXT, url=""),
        dict(kind="web", title="Ana", text=TEXT, url="https://a\nb"),
        dict(kind="repo", title="Ana", text=TEXT),
    ]
    for kwargs in bad:
        with pytest.raises(SpecfloError):
            sources.add_text_source(root, cfg, "thing", **kwargs)
        assert _tree(root) == before, kwargs

    entry = sources.add_text_source(root, cfg, "thing", "repo", "The repo", "README\n", url=URL, today=TODAY)
    assert entry.id == "S-01"
    assert list(entry.fields) == [
        ("Kind", "repo"), ("Date", TODAY), ("Url", URL), ("Text", "research/sources/S-01/text.md"),
    ]
    assert sources.text_path(root, cfg, "thing", "S-01").read_text() == "README\n"


# --- a finding may cite an interview source, without a page --------------------------------


def test_a_finding_can_cite_an_interview_source_but_not_a_page_of_it(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    _ok(_add("interview", "Ana on renewals", _text_file(tmp_path)))
    cfg = config.load_config(root)

    shown = _ok([
        *FINDING_ADD, "--text", "Renewals run five years.", "--kind", "fact",
        "--status", "supported", "--cites", "S-01",
    ]).output
    assert "S-01" in shown
    (finding,) = ledger.FINDINGS.entries(root, cfg, "thing")
    assert finding.all("Cites") == ["S-01"]

    before = _tree(root)
    shown = _refused([
        *FINDING_ADD, "--text", "Renewals run five years.", "--kind", "fact",
        "--status", "supported", "--cites", "S-01:p2",
    ])
    assert "S-01:p2" in shown
    assert _tree(root) == before


# --- the egress hint ---------------------------------------------------------------------


def test_the_first_source_on_an_unpinned_project_prints_the_hint_once_and_sets_no_pin(
    tmp_path, monkeypatch
):
    root = _research_project(tmp_path, monkeypatch, pinned=False)
    project_md = _project(root) / "project.md"
    assert "egress" not in project_md.read_text()
    project_before = project_md.read_bytes()

    first = _ok(_add("interview", "Ana", _text_file(tmp_path, "ana.md"))).output
    lines = first.splitlines()
    assert len(lines) == 2
    assert "S-01" in lines[0]
    assert PIN in lines[1]
    assert lines[1] == sources.EGRESS_HINT
    assert lines[1].isascii()
    # The hint is a suggestion: the project file is untouched and pins nothing.
    assert project_md.read_bytes() == project_before
    assert "egress" not in project_md.read_text()

    second = _ok(_add("web", "The act", _text_file(tmp_path, "act.md"), URL)).output
    assert second.count("\n") == 1
    assert "egress" not in second
    assert project_md.read_bytes() == project_before


@pytest.mark.parametrize("egress_class", ["local", "no-train", "open"])
def test_a_pinned_project_prints_no_hint_at_its_first_source(tmp_path, monkeypatch, egress_class):
    root = _research_project(tmp_path, monkeypatch, pinned=False)
    _ok(["egress", egress_class])

    shown = _ok(_add("interview", "Ana", _text_file(tmp_path))).output

    assert shown.count("\n") == 1
    assert "egress" not in shown
    assert f"egress: {egress_class}" in (_project(root) / "project.md").read_text()


def test_ingest_as_the_first_source_prints_the_hint_and_a_later_source_add_does_not(
    tmp_path, monkeypatch
):
    root = _research_project(tmp_path, monkeypatch, pinned=False)
    project_md = _project(root) / "project.md"

    first = _ok([*INGEST, str(_text_file(tmp_path, "notes.md"))]).output
    assert first.splitlines()[-1] == sources.EGRESS_HINT
    assert first.count("\n") == 2
    assert "egress" not in project_md.read_text()

    second = _ok(_add("interview", "Ana", _text_file(tmp_path, "ana.md"))).output
    assert "egress" not in second
    third = _ok([*INGEST, str(_text_file(tmp_path, "more.md", "More notes.\n"))]).output
    assert "egress" not in third


def test_egress_hint_reads_the_pin_and_the_ledger_and_writes_nothing(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch, pinned=False)
    cfg = config.load_config(root)
    before = _tree(root)

    hint = sources.egress_hint(root, cfg, "thing")
    assert hint == sources.EGRESS_HINT
    assert "specflo egress local|no-train|open" in hint
    assert hint.splitlines() == [hint]
    assert _tree(root) == before

    sources.add_text_source(root, cfg, "thing", "interview", "Ana", TEXT, today=TODAY)
    assert sources.egress_hint(root, cfg, "thing") is None

    _ok(["egress", "no-train"])
    assert sources.egress_hint(root, cfg, "thing") is None


def test_a_refused_first_source_leaves_the_hint_for_the_next_one(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch, pinned=False)
    _refused(_add("web", "The act", _text_file(tmp_path, "act.md")))
    assert "egress" not in (_project(root) / "project.md").read_text()

    shown = _ok(_add("web", "The act", _text_file(tmp_path, "act.md"), URL)).output
    assert shown.splitlines()[-1] == sources.EGRESS_HINT


# --- which project the command works on ------------------------------------------------


def test_source_add_refuses_a_project_without_a_research_document(tmp_path, monkeypatch):
    root = _checkout(tmp_path / "local", monkeypatch)
    _ok(["new", "Plain"])
    before = _tree(root)

    shown = _refused(_add("interview", "Ana", _text_file(tmp_path)))

    assert "plain" in shown and "research" in shown
    assert _tree(root) == before


# --- the guide ----------------------------------------------------------------------------


def test_the_guide_lists_research_source_add_directly_after_research_ingest(tmp_path, monkeypatch):
    rows = [entry for entry in guide.COMMANDS if entry["group"] == "research"]
    assert [row["name"] for row in rows[:3]] == [
        "research ingest", "research source add", "research user set",
    ]
    row = rows[1]
    assert "--kind interview|web|repo" in row["args"]
    assert "--title" in row["args"] and "--file" in row["args"] and "--stdin" in row["args"]
    assert "--url" in row["args"]
    assert row["summary"].isascii() and row["args"].isascii()

    monkeypatch.chdir(tmp_path)
    out = _ok(["guide"]).output

    assert "\n    research source add " in out
    assert out.index("\n    research ingest ") < out.index("\n    research source add ")
    assert out.index("\n    research source add ") < out.index("\n    research user set ")

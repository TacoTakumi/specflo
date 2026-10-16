"""The glossary command: ``specflo research term set``.

The glossary is the one ledger keyed by term. Setting a term adds its entry,
a ``### <term>`` heading with a Definition line and one Avoid line per word;
setting it again rewrites those lines in place and the research log keeps the
definition it replaced, so nothing is lost. The CLI is the only writer of the
file and nothing deletes an entry.
"""

import datetime
import re

import pytest
from typer.testing import CliRunner

from specflo import config, guide
from specflo.cli import app
from specflo.errors import SpecfloError
from specflo.research import document, glossary, ledger

runner = CliRunner()

TODAY = datetime.date.today().isoformat()
TERM_SET = ["research", "term", "set"]
CAP_RATE = [*TERM_SET, "--term", "Cap rate", "--definition", "net income over price"]


def _ok(args):
    result = runner.invoke(app, args)
    assert result.exit_code == 0, (args, result.output)
    return result


def _refused(args):
    result = runner.invoke(app, args)
    assert result.exit_code != 0, (args, result.output)
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


def _cfg(root):
    return config.load_config(root)


def _glossary(root, slug="thing"):
    return ledger.GLOSSARY.path(root, _cfg(root), slug).read_text()


def _log(root, slug="thing"):
    return document.load(root, _cfg(root), slug).log


def _terms(root, slug="thing"):
    return glossary.terms(root, _cfg(root), slug)


# --- term set -----------------------------------------------------------------


def test_term_set_records_the_term_its_definition_and_the_words_to_avoid(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)

    shown = _ok([*CAP_RATE, "--avoid", "yield", "--avoid", "cap"]).output

    (entry,) = _terms(root)
    assert (entry.id, entry.title) == ("Cap rate", "")
    assert entry.get(glossary.DEFINITION_FIELD) == "net income over price"
    assert entry.all(glossary.AVOID_FIELD) == ["yield", "cap"]
    assert entry.fields == (
        ("Definition", "net income over price"), ("Avoid", "yield"), ("Avoid", "cap"),
    )
    assert _log(root) == ()
    assert "Cap rate" in shown and "thing" in shown
    assert "log" not in shown.lower()
    doc = _glossary(root)
    assert doc.endswith(
        "### Cap rate\n- Definition: net income over price\n- Avoid: yield\n- Avoid: cap\n\n"
    )
    assert f"updated: {TODAY}\n" in doc


def test_term_set_without_words_to_avoid_writes_the_definition_alone(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)

    _ok(CAP_RATE)

    (entry,) = _terms(root)
    assert entry.fields == (("Definition", "net income over price"),)
    assert _glossary(root).endswith("### Cap rate\n- Definition: net income over price\n\n")


def test_term_set_again_replaces_the_definition_in_place_and_the_log_keeps_the_old_one(
    tmp_path, monkeypatch
):
    root = _research_project(tmp_path, monkeypatch)
    _ok([*CAP_RATE, "--avoid", "yield"])

    shown = _ok(
        [*TERM_SET, "--term", "Cap rate", "--definition", "the first year's return on the price",
         "--avoid", "yield", "--avoid", "return"]
    ).output

    (entry,) = _terms(root)
    assert entry.id == "Cap rate"
    assert entry.get("Definition") == "the first year's return on the price"
    assert entry.all("Avoid") == ["yield", "return"]
    doc = _glossary(root)
    assert doc.count("### Cap rate\n") == 1
    assert "net income over price" not in doc
    assert doc.endswith(
        "### Cap rate\n- Definition: the first year's return on the price\n"
        "- Avoid: yield\n- Avoid: return\n\n"
    )
    (logged,) = _log(root)
    assert (logged.date, logged.kind) == (TODAY, glossary.TERM_KIND)
    assert logged.kind == "term"
    assert "Cap rate" in logged.text
    assert "net income over price" in logged.text
    assert "yield" in logged.text
    assert "the first year's return on the price" not in logged.text
    assert "log" in shown.lower()


def test_term_set_again_without_words_to_avoid_drops_the_old_ones(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    _ok([*CAP_RATE, "--avoid", "yield", "--avoid", "cap"])

    _ok([*TERM_SET, "--term", "Cap rate", "--definition", "a return on the price"])

    (entry,) = _terms(root)
    assert entry.fields == (("Definition", "a return on the price"),)
    assert "- Avoid:" not in _glossary(root)
    (logged,) = _log(root)
    assert "yield" in logged.text and "cap" in logged.text


def test_a_re_set_term_keeps_its_place_and_each_set_logs_once(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    _ok(CAP_RATE)
    _ok([*TERM_SET, "--term", "NOI", "--definition", "net operating income"])

    _ok([*TERM_SET, "--term", "Cap rate", "--definition", "second"])
    _ok([*TERM_SET, "--term", "Cap rate", "--definition", "third"])

    assert [(e.id, e.get("Definition")) for e in _terms(root)] == [
        ("Cap rate", "third"), ("NOI", "net operating income"),
    ]
    doc = _glossary(root)
    assert doc.index("### Cap rate\n") < doc.index("### NOI\n")
    log = _log(root)
    assert [e.kind for e in log] == ["term", "term"]
    assert "net income over price" in log[0].text
    assert "second" in log[1].text


def test_a_different_term_adds_a_second_entry_and_matching_is_exact(tmp_path, monkeypatch):
    """A term is matched on its trimmed text, case and all: 'cap rate' is not
    'Cap rate', so a glossary can hold a term and its lower-case variant."""
    root = _research_project(tmp_path, monkeypatch)
    _ok(CAP_RATE)

    _ok([*TERM_SET, "--term", "  Cap rate  ", "--definition", "trimmed"])
    _ok([*TERM_SET, "--term", "cap rate", "--definition", "lower"])

    assert [(e.id, e.get("Definition")) for e in _terms(root)] == [
        ("Cap rate", "trimmed"), ("cap rate", "lower"),
    ]
    assert len(_log(root)) == 1


def test_term_set_refuses_an_empty_term_or_definition_and_writes_nothing(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    before = _tree(root)

    for args in (
        [*TERM_SET, "--definition", "no term"],
        [*TERM_SET, "--term", "", "--definition", "empty term"],
        [*TERM_SET, "--term", "   ", "--definition", "blank term"],
        [*TERM_SET, "--term", "one\ntwo", "--definition", "two lines"],
        [*TERM_SET, "--term", "Cap rate"],
        [*TERM_SET, "--term", "Cap rate", "--definition", ""],
        [*TERM_SET, "--term", "Cap rate", "--definition", "   "],
        [*TERM_SET, "--term", "Cap rate", "--definition", "one\ntwo"],
        [*CAP_RATE, "--avoid", ""],
        [*CAP_RATE, "--avoid", "  "],
        [*CAP_RATE, "--avoid", "one\ntwo"],
    ):
        shown = _refused(args)
        assert "Traceback" not in shown, args
        assert _tree(root) == before, args


def test_term_set_refuses_a_project_without_a_research_document(tmp_path, monkeypatch):
    root = _checkout(tmp_path / "local", monkeypatch)
    _ok(["new", "Plain"])
    before = _tree(root)

    shown = _refused(CAP_RATE)

    assert "plain" in shown and document.RESEARCH_FILENAME in shown
    assert "Traceback" not in shown
    assert _tree(root) == before


def test_term_set_refuses_a_project_whose_glossary_is_missing(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    _ok(CAP_RATE)
    ledger.GLOSSARY.path(root, _cfg(root), "thing").unlink()
    before = _tree(root)

    shown = _refused([*TERM_SET, "--term", "NOI", "--definition", "net operating income"])

    assert "glossary" in shown and "Traceback" not in shown
    assert _tree(root) == before


def test_term_set_needs_a_checkout_and_an_active_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert "specflo init" in _refused(CAP_RATE)

    root = _checkout(tmp_path / "local", monkeypatch)
    before = _tree(root)
    assert "No active project" in _refused(CAP_RATE)
    assert _tree(root) == before


# --- the glossary functions on their own -----------------------------------------


def test_set_term_returns_the_entry_and_get_term_finds_it(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    cfg = _cfg(root)

    entry = glossary.set_term(root, cfg, "thing", " Cap rate ", " net income over price ",
                              avoid=("yield", " cap ", "yield"), today="2026-10-17")

    assert entry == ledger.Entry(
        "Cap rate", "", (("Definition", "net income over price"), ("Avoid", "yield"), ("Avoid", "cap")),
    )
    assert glossary.get_term(root, cfg, "thing", "Cap rate") == entry
    assert glossary.get_term(root, cfg, "thing", " Cap rate ") == entry
    assert glossary.get_term(root, cfg, "thing", "cap rate") is None
    assert glossary.get_term(root, cfg, "thing", "") is None
    assert glossary.terms(root, cfg, "thing") == [entry]
    assert "updated: 2026-10-17\n" in _glossary(root)

    again = glossary.set_term(root, cfg, "thing", "Cap rate", "a return", today="2026-10-18")

    assert again == ledger.Entry("Cap rate", "", (("Definition", "a return"),))
    assert glossary.terms(root, cfg, "thing") == [again]
    (logged,) = _log(root)
    assert logged == document.LogEntry(
        "2026-10-18", "term", glossary.format_replaced("Cap rate", "net income over price", ["yield", "cap"]),
    )
    assert "net income over price" in logged.text and "yield, cap" in logged.text
    assert "updated: 2026-10-18\n" in _glossary(root)
    assert "updated: 2026-10-18\n" in document.read_document(root, cfg, "thing")


def test_set_term_validates_before_it_touches_the_files(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    cfg = _cfg(root)
    glossary.set_term(root, cfg, "thing", "Cap rate", "net income over price", today=TODAY)
    before = _tree(root)

    for term, definition, avoid in (
        ("", "x", ()), ("  ", "x", ()), (None, "x", ()), ("a\nb", "x", ()),
        ("Cap rate", "", ()), ("Cap rate", "  ", ()), ("Cap rate", None, ()),
        ("Cap rate", "a\nb", ()),
        ("Cap rate", "x", ("",)), ("Cap rate", "x", ("  ",)), ("Cap rate", "x", ("a\nb",)),
    ):
        with pytest.raises(SpecfloError):
            glossary.set_term(root, cfg, "thing", term, definition, avoid=avoid, today=TODAY)
        assert _tree(root) == before, (term, definition, avoid)

    document.research_path(root, cfg, "thing").unlink()
    before = _tree(root)
    # A re-set logs to the research document, so with none it refuses and the glossary stays.
    with pytest.raises(SpecfloError, match="research"):
        glossary.set_term(root, cfg, "thing", "Cap rate", "another", today=TODAY)
    assert _tree(root) == before

    ledger.GLOSSARY.path(root, cfg, "thing").unlink()
    with pytest.raises(SpecfloError, match="glossary"):
        glossary.set_term(root, cfg, "thing", "NOI", "net operating income", today=TODAY)
    with pytest.raises(SpecfloError, match="glossary"):
        glossary.terms(root, cfg, "thing")
    with pytest.raises(SpecfloError, match="glossary"):
        glossary.get_term(root, cfg, "thing", "Cap rate")


def test_set_term_refuses_a_glossary_without_its_section(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    cfg = _cfg(root)
    path = ledger.GLOSSARY.path(root, cfg, "thing")
    path.write_text("# Glossary: thing\n\nno section here\n")

    with pytest.raises(SpecfloError, match="## Glossary"):
        glossary.set_term(root, cfg, "thing", "Cap rate", "x", today=TODAY)
    assert path.read_text() == "# Glossary: thing\n\nno section here\n"


def test_the_glossary_module_offers_no_delete():
    names = [name for name in dir(glossary) if not name.startswith("_")]
    assert not [n for n in names if re.search(r"delete|remove|drop|unlink|clear", n, re.I)], names


# --- the guide ------------------------------------------------------------------


def test_term_set_has_a_guide_row_under_the_research_group():
    rows = {entry["name"]: entry for entry in guide.COMMANDS if entry["group"] == "research"}

    row = rows["research term set"]
    assert row["args"] == "--term <term> --definition <text> [--avoid <word>]..."
    assert "log" in row["summary"]
    assert row["summary"].isascii() and row["args"].isascii()
    names = [entry["name"] for entry in guide.COMMANDS]
    assert names.index("research session close") + 1 == names.index("research term set")


def test_the_guide_text_shows_the_term_set_row_after_session_close(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    out = _ok(["guide"]).output

    assert "\n    research term set " in out
    assert out.index("\n    research session close ") < out.index("\n    research term set ")
    assert out.index("\n    research term set ") < out.index("\n    agent start ")

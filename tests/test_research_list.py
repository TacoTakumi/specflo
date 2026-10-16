"""The research list command: each ledger's entries with their IDs and fields.

``specflo research list sources|questions|findings|terms`` prints one block
per entry: the ID (or the term) and title on the first line, each field
indented under it, a repeated field one line per value, and a superseded
entry marked on its first line. ``--open`` keeps the questions still waiting
for an answer; ``--json`` gives every entry with its ID and status. The
command reads only: it changes no file.
"""

import datetime
import json

from typer.testing import CliRunner

from specflo import config, guide
from specflo.cli import app
from specflo.research import document, ledger

runner = CliRunner()

TODAY = datetime.date.today().isoformat()
LIST = ["research", "list"]
LEDGER_NAMES = ("sources", "questions", "findings", "terms")
QUESTION_ADD = ["research", "question", "add"]
USER_QUESTION = [*QUESTION_ADD, "--text", "Who signs the lease?", "--by", "user"]
DEVELOPER_QUESTION = [
    *QUESTION_ADD, "--text", "Which market first?", "--by", "developer",
    "--option", "Toronto", "--option", "Calgary", "--default", "Toronto",
    "--why", "the user lives there",
]
PDF_FIELDS = [("Kind", "document"), ("Date", "2026-10-16"), ("Format", "pdf"), ("Pages", "10")]
MD_FIELDS = [("Kind", "document"), ("Date", "2026-10-16"), ("Format", "md")]
EVERY_COMMAND = [[*LIST, name] for name in LEDGER_NAMES]


def _ok(args):
    result = runner.invoke(app, args)
    assert result.exit_code == 0, (args, result.output)
    return result


def _refused(args):
    result = runner.invoke(app, args)
    assert result.exit_code != 0, (args, result.output)
    assert "Traceback" not in result.output, args
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


def _with_sources(root):
    """S-01, a 10-page PDF, and S-02, a markdown file, in the sources ledger."""
    cfg = config.load_config(root)
    ledger.SOURCES.add(root, cfg, "thing", "paper.pdf", fields=PDF_FIELDS, today=TODAY)
    ledger.SOURCES.add(root, cfg, "thing", "notes.md", fields=MD_FIELDS, today=TODAY)


def _listed(args):
    return json.loads(_ok([*args, "--json"]).output)


# --- the text of each ledger ------------------------------------------------------


def test_sources_lists_each_entry_with_its_id_title_and_fields(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    _with_sources(root)

    out = _ok([*LIST, "sources"]).output

    assert (
        "S-01 - paper.pdf\n  Kind: document\n  Date: 2026-10-16\n  Format: pdf\n  Pages: 10\n"
    ) in out
    assert "S-02 - notes.md\n  Kind: document\n  Date: 2026-10-16\n  Format: md\n" in out
    assert out.index("S-01 - ") < out.index("S-02 - ")


def test_questions_lists_the_fields_in_order_and_repeats_each_option(tmp_path, monkeypatch):
    _research_project(tmp_path, monkeypatch)
    _ok(DEVELOPER_QUESTION)
    _ok([*USER_QUESTION, "--why", "the lease names a signer"])

    out = _ok([*LIST, "questions"]).output

    assert (
        "Q-01 - Which market first?\n  By: developer\n  Option: Toronto\n  Option: Calgary\n"
        "  Default: Toronto\n  Why: the user lives there\n  Status: open\n"
    ) in out
    assert "Q-02 - Who signs the lease?\n  By: user\n  Why: the lease names a signer\n  Status: open\n" in out
    assert "[superseded" not in out


def test_findings_lists_citations_and_marks_a_superseded_finding_on_its_first_line(
    tmp_path, monkeypatch
):
    root = _research_project(tmp_path, monkeypatch)
    _with_sources(root)
    _ok([
        "research", "finding", "add", "--text", "Rent rose 4% in 2025.", "--kind", "fact",
        "--status", "supported", "--cites", "S-01:p3", "--cites", "S-02",
    ])
    _ok([
        "research", "finding", "add", "--text", "Rent rose 5% in 2025.", "--kind", "fact",
        "--status", "supported", "--cites", "S-01:p4", "--supersedes", "RF-01",
    ])

    out = _ok([*LIST, "findings"]).output

    assert (
        "RF-01 - Rent rose 4% in 2025. [superseded by RF-02]\n  Kind: fact\n"
        "  Status: supported\n  Cites: S-01:p3\n  Cites: S-02\n  Superseded by: RF-02\n"
    ) in out
    assert (
        "RF-02 - Rent rose 5% in 2025.\n  Kind: fact\n  Status: supported\n"
        "  Cites: S-01:p4\n  Supersedes: RF-01\n"
    ) in out


def test_terms_lists_each_term_with_its_definition_and_words_to_avoid(tmp_path, monkeypatch):
    _research_project(tmp_path, monkeypatch)
    _ok([
        "research", "term", "set", "--term", "Cap rate",
        "--definition", "Net operating income over the price", "--avoid", "yield", "--avoid", "ROI",
    ])
    _ok(["research", "term", "set", "--term", "Net lease", "--definition", "The tenant pays the costs"])

    out = _ok([*LIST, "terms"]).output

    assert "Cap rate\n  Definition: Net operating income over the price\n  Avoid: yield\n  Avoid: ROI\n" in out
    assert "Net lease\n  Definition: The tenant pays the costs\n" in out
    assert " - " not in out.split("Cap rate", 1)[0]


def test_an_empty_ledger_prints_one_line_and_json_gives_an_empty_list(tmp_path, monkeypatch):
    _research_project(tmp_path, monkeypatch)

    for name in LEDGER_NAMES:
        out = _ok([*LIST, name]).output
        assert out.count("\n") == 1 and name in out, name
        assert _listed([*LIST, name]) == [], name
    out = _ok([*LIST, "questions", "--open"]).output
    assert out.count("\n") == 1 and "open" in out and "questions" in out


# --- --open ---------------------------------------------------------------------------


def test_open_keeps_the_open_questions_and_omits_resolved_and_superseded_ones(
    tmp_path, monkeypatch
):
    _research_project(tmp_path, monkeypatch)
    _ok(USER_QUESTION)
    _ok([*USER_QUESTION, "--supersedes", "Q-01"])
    _ok([*QUESTION_ADD, "--text", "How long is the term?", "--by", "user"])
    _ok(["research", "question", "resolve", "Q-03", "--resolution", "Five years."])
    _ok(DEVELOPER_QUESTION)

    out = _ok([*LIST, "questions", "--open"]).output

    assert "Q-02 - Who signs the lease?" in out and "Q-04 - Which market first?" in out
    assert "Q-01" not in out.replace("Supersedes: Q-01", "") and "Q-03" not in out
    assert [q["id"] for q in _listed([*LIST, "questions", "--open"])] == ["Q-02", "Q-04"]

    out = _ok([*LIST, "questions"]).output
    assert "Q-01 - Who signs the lease? [superseded by Q-02]" in out
    assert "Q-03 - How long is the term?\n" in out and "  Status: resolved\n" in out
    assert [q["id"] for q in _listed([*LIST, "questions"])] == ["Q-01", "Q-02", "Q-03", "Q-04"]


def test_open_is_refused_on_any_ledger_but_questions(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    before = _tree(root)

    for name in ("sources", "findings", "terms"):
        shown = _refused([*LIST, name, "--open"])
        assert "questions" in shown, name
        assert "--open" in shown, name
    assert _tree(root) == before


# --- --json ---------------------------------------------------------------------------


def test_json_carries_each_entry_with_its_id_status_and_fields(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    _with_sources(root)
    _ok(DEVELOPER_QUESTION)
    _ok([*USER_QUESTION, "--supersedes", "Q-01"])
    _ok(["research", "question", "resolve", "Q-02", "--resolution", "The tenant.", "--by", "Ana"])
    _ok([
        "research", "finding", "add", "--text", "Rent rose 4% in 2025.", "--kind", "fact",
        "--status", "supported", "--cites", "S-01:p3", "--cites", "S-02",
    ])
    _ok(["research", "term", "set", "--term", "Cap rate", "--definition", "NOI over price", "--avoid", "yield"])

    old, new = _listed([*LIST, "questions"])
    assert set(old) == {"id", "title", "status", "supersedes", "superseded_by", "active", "fields"}
    assert old == {
        "id": "Q-01", "title": "Which market first?", "status": "open",
        "supersedes": None, "superseded_by": "Q-02", "active": False,
        "fields": {
            "By": "developer", "Option": ["Toronto", "Calgary"], "Default": "Toronto",
            "Why": "the user lives there", "Status": "open", "Superseded by": "Q-02",
        },
    }
    assert (new["id"], new["status"], new["supersedes"], new["active"]) == ("Q-02", "resolved", "Q-01", True)
    assert new["fields"]["Resolved by"] == "Ana"

    (finding,) = _listed([*LIST, "findings"])
    assert (finding["id"], finding["title"], finding["status"]) == ("RF-01", "Rent rose 4% in 2025.", "supported")
    assert finding["fields"]["Cites"] == ["S-01:p3", "S-02"]

    first, second = _listed([*LIST, "sources"])
    assert (first["id"], first["title"], first["status"]) == ("S-01", "paper.pdf", None)
    assert first["fields"] == {"Kind": "document", "Date": "2026-10-16", "Format": "pdf", "Pages": "10"}
    assert second["id"] == "S-02"

    (term,) = _listed([*LIST, "terms"])
    assert (term["id"], term["title"], term["status"], term["active"]) == ("Cap rate", "Cap rate", None, True)
    assert term["fields"] == {"Definition": "NOI over price", "Avoid": "yield"}


# --- refusals -------------------------------------------------------------------------


def test_an_unknown_ledger_name_is_refused_and_names_the_four(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    before = _tree(root)

    for name in ("glossary", "source", "Questions", ""):
        shown = _refused([*LIST, name])
        for known in LEDGER_NAMES:
            assert known in shown, (name, known)
    assert _tree(root) == before


def test_listing_changes_no_file(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    _with_sources(root)
    _ok(DEVELOPER_QUESTION)
    _ok(["research", "term", "set", "--term", "Cap rate", "--definition", "NOI over price"])
    before = _tree(root)

    for name in LEDGER_NAMES:
        _ok([*LIST, name])
        _ok([*LIST, name, "--json"])
    _ok([*LIST, "questions", "--open"])
    _ok([*LIST, "questions", "--open", "--json"])

    assert _tree(root) == before


def test_a_missing_ledger_file_is_refused_by_name(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    ledger.FINDINGS.path(root, config.load_config(root), "thing").unlink()

    shown = _refused([*LIST, "findings"])

    assert "findings" in shown and "thing" in shown
    assert _ok([*LIST, "questions"]).exit_code == 0


def test_list_refuses_a_project_without_a_research_document(tmp_path, monkeypatch):
    root = _checkout(tmp_path / "local", monkeypatch)
    _ok(["new", "Plain"])
    before = _tree(root)

    for args in EVERY_COMMAND:
        shown = _refused(args)
        assert "plain" in shown and document.RESEARCH_FILENAME in shown, args
    assert _tree(root) == before


def test_list_needs_a_checkout_and_an_active_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for args in EVERY_COMMAND:
        assert "specflo init" in _refused(args), args

    root = _checkout(tmp_path / "local", monkeypatch)
    before = _tree(root)
    for args in EVERY_COMMAND:
        assert "No active project" in _refused(args), args
    assert _tree(root) == before


# --- the guide ------------------------------------------------------------------------


def test_list_has_a_guide_row_after_finding_add_under_the_research_group():
    rows = {entry["name"]: entry for entry in guide.COMMANDS if entry["group"] == "research"}

    row = rows["research list"]
    assert row["args"] == "sources|questions|findings|terms [--open] [--json]"
    assert "--open" in row["summary"] and "--json" in row["summary"]
    assert row["summary"].isascii() and row["args"].isascii()
    names = [entry["name"] for entry in guide.COMMANDS]
    assert names.index("research finding add") + 1 == names.index("research list")


def test_the_guide_text_shows_the_list_row_after_finding_add(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    out = _ok(["guide"]).output

    assert "\n    research list " in out
    assert out.index("\n    research finding add ") < out.index("\n    research list ")
    assert out.index("\n    research list ") < out.index("\n    research session close ")

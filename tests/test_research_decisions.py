"""Decisions on a research project live in the research document.

A research project has no brainstorm, so `specflo decision add` writes its
D-NN entries into the Decisions section of ``research.md``, in the format the
brainstorm uses. One D-NN sequence runs across the research document, the
brainstorm and every brief: after the project moves up to fast or full, the
next decision goes into the brainstorm, the research document keeps what it
holds, and `specflo decision list` names where each decision lives.
"""

import json

import pytest
from typer.testing import CliRunner

from specflo import brainstorm, config, markdown, projects
from specflo.cli import app
from specflo.errors import SpecfloError
from specflo.research import document

runner = CliRunner()

LEDGER_FILES = ("sources.md", "questions.md", "findings.md", "glossary.md")
FIRST = ["decision", "add", "--text", "Keep the glossary", "--rationale", "the expert's words"]
SECOND = ["decision", "add", "--text", "Use SQLite", "--rationale", "simplest"]


def _ok(args):
    result = runner.invoke(app, args)
    assert result.exit_code == 0, (args, result.output)
    assert "checkpoint not written" not in result.output, (args, result.output)
    return result


def _refused(args):
    result = runner.invoke(app, args)
    assert result.exit_code != 0, (args, result.output)
    return result.output


def _research_project(tmp_path, monkeypatch):
    root = tmp_path / "local"
    root.mkdir()
    config.init_config(root)
    monkeypatch.chdir(root)
    _ok(["new", "Thing", "--level", "research"])
    return root, projects.load_project(root, config.load_config(root), "thing").path


def _ledgers(project_dir):
    return {name: (project_dir / "research" / name).read_bytes() for name in LEDGER_FILES}


# --- a research project before any move up --------------------------------------


def test_decision_add_records_d01_in_the_research_document_and_nowhere_else(tmp_path, monkeypatch):
    root, project_dir = _research_project(tmp_path, monkeypatch)
    ledgers_before = _ledgers(project_dir)

    result = _ok(FIRST)

    assert result.output.strip() == "Recorded D-01."
    doc = (project_dir / "research.md").read_text()
    entry = "### D-01 \u2014 Keep the glossary\n- Rationale: the expert's words\n- Status: active\n"
    assert doc.index("## Decisions") < doc.index(entry) < doc.index("## Log")
    assert markdown.section_body(doc, document.DECISIONS_HEADER).count("### D-") == 1
    assert not (project_dir / "brainstorm.md").exists()
    assert _ledgers(project_dir) == ledgers_before
    # The document still parses as the research commands read it.
    assert document.parse(doc).log == ()
    assert document.parse(doc).destination == document.UNKNOWN_DESTINATION


def test_decision_add_on_a_research_project_names_the_research_document_in_its_json(
    tmp_path, monkeypatch
):
    _research_project(tmp_path, monkeypatch)

    info = json.loads(_ok(FIRST + ["--json"]).output)

    assert info == {"id": "D-01", "supersedes": None, "source": "research", "diverges": False}


def test_decision_list_shows_a_research_decision_with_its_source(tmp_path, monkeypatch):
    _research_project(tmp_path, monkeypatch)
    _ok(FIRST)

    shown = _ok(["decision", "list"]).output
    info = json.loads(_ok(["decision", "list", "--json"]).output)

    assert shown == "D-01  research  Keep the glossary\n"
    assert [(d["id"], d["source"], d["text"], d["status"]) for d in info] == [
        ("D-01", "research", "Keep the glossary", "active"),
    ]


def test_research_decisions_take_one_sequence_and_supersede_in_place(tmp_path, monkeypatch):
    root, project_dir = _research_project(tmp_path, monkeypatch)
    _ok(FIRST)

    result = _ok(SECOND + ["--supersedes", "D-01"])

    assert result.output.strip() == "Recorded D-02. Supersedes D-01."
    doc = (project_dir / "research.md").read_text()
    assert "### D-01 \u2014 Keep the glossary\n- Rationale: the expert's words\n- Status: superseded by D-02\n" in doc
    assert "### D-02 \u2014 Use SQLite\n- Rationale: simplest\n- Supersedes: D-01\n- Status: active\n" in doc
    assert _ok(["decision", "list"]).output == "D-02  research  Use SQLite\n"
    assert _ok(["decision", "list", "--all"]).output == (
        "D-01  research  Keep the glossary  [superseded by D-02]\n"
        "D-02  research  Use SQLite\n"
    )


def test_a_duplicate_research_decision_is_refused_naming_the_list_and_writes_nothing(
    tmp_path, monkeypatch
):
    root, project_dir = _research_project(tmp_path, monkeypatch)
    _ok(FIRST)
    before = (project_dir / "research.md").read_bytes()

    shown = _refused(["decision", "add", "--text", "keep  the glossary"])

    assert "D-01 already records this decision" in shown
    assert "specflo decision list" in shown
    assert "brainstorm" not in shown
    assert (project_dir / "research.md").read_bytes() == before


def test_a_research_decision_is_written_under_the_research_lock(tmp_path, monkeypatch):
    # The research commands write research.md under the project's one research
    # lock, so a decision into it takes that lock and the series lock, not a
    # lock named for the file that nothing else takes.
    root, _ = _research_project(tmp_path, monkeypatch)
    cfg = config.load_config(root)
    taken = []
    real_locked = brainstorm.locked

    def recording_locked(path):
        taken.append(path.name)
        return real_locked(path)

    monkeypatch.setattr(brainstorm, "locked", recording_locked)

    brainstorm.add_decision(root, cfg, "thing", "Keep the glossary")

    assert taken[0] == brainstorm.DECISIONS_LOCK + ".lock"
    assert document.LOCK_NAME + ".lock" in taken
    assert "research.md.lock" not in taken


def test_a_project_with_neither_document_is_still_refused_as_before(tmp_path):
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Bare", created="2026-10-16")

    with pytest.raises(SpecfloError, match="No brainstorm yet"):
        brainstorm.add_decision(tmp_path, cfg, "bare", "x")

    assert brainstorm.decision_documents(tmp_path, cfg, "bare") == []
    assert brainstorm.list_decisions(tmp_path, cfg, "bare") == []


# --- after the move up to fast or full ------------------------------------------


@pytest.mark.parametrize("target", ["fast", "full"])
def test_after_the_move_up_the_next_decision_goes_into_the_brainstorm(tmp_path, monkeypatch, target):
    root, project_dir = _research_project(tmp_path, monkeypatch)
    _ok(FIRST)
    research_before = (project_dir / "research.md").read_bytes()

    _ok(["level", target])
    result = _ok(SECOND)

    assert result.output.strip() == "Recorded D-02."
    brainstorm_text = (project_dir / "brainstorm.md").read_text()
    assert "### D-02 \u2014 Use SQLite\n- Rationale: simplest\n- Status: active\n" in brainstorm_text
    assert "D-01" not in brainstorm_text
    assert (project_dir / "research.md").read_bytes() == research_before
    cfg = config.load_config(root)
    assert [(source, path.name) for source, path in brainstorm.decision_documents(root, cfg, "thing")] == [
        ("research", "research.md"), ("brainstorm", "brainstorm.md"),
    ]


def test_decision_list_spans_the_research_document_and_the_brainstorm_in_order(tmp_path, monkeypatch):
    _research_project(tmp_path, monkeypatch)
    _ok(FIRST)
    _ok(["level", "full"])
    _ok(SECOND)

    shown = _ok(["decision", "list"]).output
    info = json.loads(_ok(["decision", "list", "--json"]).output)

    assert shown == (
        "D-01  research    Keep the glossary\n"
        "D-02  brainstorm  Use SQLite\n"
    )
    assert [(d["id"], d["source"]) for d in info] == [("D-01", "research"), ("D-02", "brainstorm")]


def test_a_brainstorm_decision_supersedes_one_held_by_the_research_document(tmp_path, monkeypatch):
    root, project_dir = _research_project(tmp_path, monkeypatch)
    _ok(FIRST)
    _ok(["level", "full"])

    result = _ok(["decision", "add", "--text", "Drop the glossary", "--supersedes", "D-01"])

    assert result.output.strip() == "Recorded D-02. Supersedes D-01."
    research_text = (project_dir / "research.md").read_text()
    assert "### D-01 \u2014 Keep the glossary\n- Rationale: the expert's words\n- Status: superseded by D-02\n" in research_text
    assert "D-02" not in markdown.section_body(research_text, document.DECISIONS_HEADER).split("### D-01")[0]
    brainstorm_text = (project_dir / "brainstorm.md").read_text()
    assert "### D-02 \u2014 Drop the glossary\n- Rationale: \u2014\n- Supersedes: D-01\n- Status: active\n" in brainstorm_text
    assert _ok(["decision", "list"]).output == "D-02  brainstorm  Drop the glossary\n"
    assert _ok(["decision", "list", "--all"]).output == (
        "D-01  research    Keep the glossary  [superseded by D-02]\n"
        "D-02  brainstorm  Drop the glossary\n"
    )
    # The research document still parses as the research commands read it.
    assert document.parse(research_text).log == ()


def test_a_brief_decision_still_counts_the_research_document_in_the_sequence(tmp_path, monkeypatch):
    root, project_dir = _research_project(tmp_path, monkeypatch)
    cfg = config.load_config(root)
    brainstorm.add_decision(root, cfg, "thing", "Keep the glossary")
    _ok(["level", "full"])
    from specflo import briefs

    created = briefs.add_brief(root, cfg, "thing", "Feedback form", sha="abc1234")

    in_brief = brainstorm.add_decision(root, cfg, "thing", "Post to /feedback", brief_id="B-01")
    after = brainstorm.add_decision(root, cfg, "thing", "Use ruff")

    assert (in_brief.id, in_brief.source) == ("D-02", "B-01")
    assert (after.id, after.source) == ("D-03", "brainstorm")
    assert "### D-02 \u2014 Post to /feedback" in created.path.read_text()
    # Document order: the research document, then the brainstorm, then the briefs.
    assert [(d.id, d.source) for d in brainstorm.list_decisions(root, cfg, "thing")] == [
        ("D-01", "research"), ("D-03", "brainstorm"), ("D-02", "B-01"),
    ]

"""Research level: one phase, research, in which a user builds up sources,
findings, questions and a glossary over sessions and nothing is built.

The level stands apart from the quick-fast-full ladder as harden does: `new`
starts a project at it, the ladder never climbs to it. Its phase is known to
the workflow, terminal, and has its own skill in the continuation."""

import json

import pytest
from typer.testing import CliRunner

from specflo import config, continuation, projects, workflow
from specflo.cli import app
from specflo.errors import SpecfloError
from specflo.research import document, ledger, scaffold
from specflo.service import promote
from specflo.service.local import LocalProjectService

runner = CliRunner()

LADDER_AND_HARDEN = ("quick", "fast", "full", "harden")
LEDGER_FILES = ("sources.md", "questions.md", "findings.md", "glossary.md")
# A registered remote nothing listens on: a call to it is refused as
# unreachable, so a refusal in other words proves no call was made.
DEAD_REMOTE = ["remote", "add", "home", "http://127.0.0.1:9", "--token", "secret"]


def _ok(args, stdin=None):
    result = runner.invoke(app, args, input=stdin)
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
    return root, _ok(["new", "Thing", "--level", "research"])


def _tree(directory):
    """Every file under ``directory`` with its bytes, keyed by relative path."""
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


# --- the level --------------------------------------------------------------


def test_research_is_a_level_new_accepts_and_the_ladder_leaves_out():
    assert projects.RESEARCH_LEVEL == "research"
    assert projects.RESEARCH_LEVEL in projects.NEW_LEVELS
    assert projects.RESEARCH_LEVEL not in projects.LEVELS


def test_the_ladder_is_unchanged_by_the_research_level():
    assert projects.LEVELS == ("quick", "fast", "full")
    assert projects.NEW_LEVELS[: len(projects.LEVELS)] == projects.LEVELS


def test_validate_level_takes_research_among_the_new_levels_only():
    assert projects.validate_level("research", projects.NEW_LEVELS) == "research"
    with pytest.raises(projects.SpecfloError, match="Unknown level 'research'"):
        projects.validate_level("research")


# --- the phase --------------------------------------------------------------


def test_research_has_the_one_phase_research():
    assert workflow.RESEARCH_PHASE == "research"
    assert workflow.phases_for("research") == ["research"]


def test_no_other_level_has_the_research_phase():
    assert "research" not in workflow.PHASES
    for level in LADDER_AND_HARDEN:
        assert "research" not in workflow.phases_for(level), level


def test_the_research_phase_is_known_and_terminal():
    assert "research" in workflow.KNOWN_PHASES
    assert workflow.next_phase("research") is None


def test_the_ladder_phases_keep_their_order_and_end():
    assert list(workflow.KNOWN_PHASES[: len(workflow.PHASES)]) == workflow.PHASES
    assert workflow.next_phase("execute") is None


def test_an_unknown_phase_is_still_refused():
    with pytest.raises(ValueError, match="Unknown phase 'nonsense'"):
        workflow.next_phase("nonsense")
    with pytest.raises(ValueError, match="Unknown phase 'nonsense'"):
        workflow.next_step("nonsense")


def test_reopen_has_nowhere_earlier_to_go_from_research():
    with pytest.raises(ValueError, match="first phase 'research'"):
        workflow.resolve_reopen_target("research", level="research")
    with pytest.raises(ValueError, match="no 'brainstorm' phase"):
        workflow.resolve_reopen_target("research", "brainstorm", level="research")


# --- the next step ----------------------------------------------------------


def test_the_research_next_step_starts_the_session_with_the_recap():
    hint = workflow.next_step("research")

    assert "`specflo research recap`" in hint
    assert hint not in {workflow.next_step(phase) for phase in workflow.PHASES}


def test_the_research_next_step_is_the_same_at_its_own_level_and_when_it_validates():
    hint = workflow.next_step("research")

    assert workflow.next_step("research", level="research") == hint
    # The document validating early in the research does not end it: the
    # hint keeps the session loop rather than offering the move.
    assert workflow.next_step("research", level="research", validates=True) == hint


def test_a_complete_research_project_reads_as_complete():
    hint = workflow.next_step("research", level="research", complete=True)

    assert hint == workflow.next_step("execute", complete=True)
    assert "specflo research recap" not in hint


def test_a_shelved_research_project_reads_as_shelved():
    hint = workflow.next_step("research", level="research", shelved=True)

    assert hint == workflow.next_step("execute", shelved=True)


# --- the continuation -------------------------------------------------------


def test_the_research_phase_has_its_own_skill_in_the_continuation():
    # The default would be the research subagent skill, which scans a landscape
    # for a brainstorm; the research project skill runs a research session.
    assert continuation.PHASE_SKILLS["research"] == "specflo-research-project"
    assert continuation.phase_skill("research") == "specflo-research-project"
    assert continuation.phase_skill("research") != "specflo-research"


def test_the_research_continuation_names_the_skill_and_the_next_step():
    hint = workflow.next_step("research", level="research")

    text = continuation.build_continuation("research", hint, offer_auto=False)

    assert "Current phase: research." in text
    assert hint in text
    assert "specflo-research-project" in text
    assert continuation.CLEAR_POINT_MARKER in text


def test_the_four_ladder_skills_are_unchanged():
    for phase in workflow.PHASES:
        assert continuation.PHASE_SKILLS[phase] == f"specflo-{phase}"


# --- specflo new --level research ----------------------------------------------


def test_new_research_starts_at_the_research_phase_with_the_document_and_the_ledgers(
    tmp_path, monkeypatch
):
    root, result = _research_project(tmp_path, monkeypatch)

    cfg = config.load_config(root)
    project = projects.load_project(root, cfg, "thing")
    assert (project.level, project.phase) == ("research", "research")
    assert cfg.active_project == "thing"
    assert "Created project 'thing' (now active). Phase: research." in result.output
    assert "thing/research" in result.output
    text = (project.path / "project.md").read_text()
    assert "\nlevel: research\n" in text and "\nphase: research\n" in text
    names = {p.name for p in project.path.iterdir()}
    assert {"project.md", "research.md", "research"} <= names
    assert not names & {"brainstorm.md", "brief.md", "spec.md", "plan.md"}
    ledgers = sorted(p.name for p in (project.path / "research").iterdir())
    assert ledgers == sorted(LEDGER_FILES)


def test_the_files_new_makes_are_the_research_scaffold_with_nothing_left_to_make(
    tmp_path, monkeypatch
):
    root, _ = _research_project(tmp_path, monkeypatch)
    cfg = config.load_config(root)

    parsed = document.load(root, cfg, "thing")
    assert parsed.destination == document.UNKNOWN_DESTINATION
    assert (parsed.reason, parsed.usual_user, parsed.log) == (None, "", ())
    for each in ledger.LEDGERS:
        assert each.path(root, cfg, "thing").parent == document.research_dir(root, cfg, "thing")
        assert each.entries(root, cfg, "thing") == []
    assert scaffold(root, cfg, "thing") == []


def test_status_shows_level_research_at_the_research_phase_with_no_next_phase(
    tmp_path, monkeypatch
):
    _research_project(tmp_path, monkeypatch)

    shown = _ok(["status"]).output
    info = json.loads(_ok(["status", "--json"]).output)

    assert "Phase:   research" in shown
    assert "Level: research" in shown
    assert (info["level"], info["phase"], info["next_phase"]) == ("research", "research", None)
    assert "specflo research recap" in info["next_step"]


def test_list_shows_the_research_project_at_its_phase(tmp_path, monkeypatch):
    _research_project(tmp_path, monkeypatch)

    info = json.loads(_ok(["list", "--json"]).output)

    assert info["active_project"] == "thing"
    assert [(p["slug"], p["phase"]) for p in info["projects"]] == [("thing", "research")]


# --- research is local only ----------------------------------------------------


@pytest.mark.parametrize("registered", [True, False])
def test_new_research_on_a_remote_is_refused_as_local_only_and_creates_nothing(
    tmp_path, monkeypatch, registered
):
    root = _checkout(tmp_path / "local", monkeypatch)
    if registered:
        _ok(DEAD_REMOTE)
    before = _tree(root)

    shown = _refused(["new", "Thing", "--level", "research", "--remote", "home"])

    assert "'research'" in shown and "local only" in shown
    assert "Cannot reach" not in shown and "home" not in shown
    assert not (root / "docs" / "projects" / "thing").exists()
    assert config.hosted_projects(root) == {}
    assert config.load_config(root).active_project is None
    assert _tree(root) == before


def test_promote_refuses_a_research_project_as_local_only_and_changes_nothing(
    tmp_path, monkeypatch
):
    root, _ = _research_project(tmp_path, monkeypatch)
    _ok(DEAD_REMOTE)
    before = _tree(root)

    shown = _refused(["promote", "thing", "--remote", "home"])

    assert "'thing'" in shown and "research level" in shown and "local only" in shown
    assert "Cannot reach" not in shown
    assert config.hosted_projects(root) == {}
    assert _tree(root) == before


def test_promote_project_refuses_the_research_level_before_it_looks_at_the_remote(tmp_path):
    cfg = config.init_config(tmp_path)
    LocalProjectService(tmp_path, cfg).create_project("Thing", level="research")

    # No remote is registered: a look at it first would refuse that instead.
    with pytest.raises(SpecfloError, match="research level.*local only"):
        promote.promote_project(tmp_path, cfg, "thing", "home")

    assert (tmp_path / "docs" / "projects" / "thing" / "project.md").is_file()
    assert config.hosted_projects(tmp_path) == {}


def test_a_daemon_creates_and_imports_no_research_project(tmp_path):
    cfg = config.init_config(tmp_path)
    local = LocalProjectService(tmp_path, cfg)
    hosted = LocalProjectService(
        tmp_path / "daemon", config.init_config(tmp_path / "daemon"), hosted=True
    )
    project = local.create_project("Thing", level="research")
    files = {"project.md": (project.path / "project.md").read_text()}

    with pytest.raises(SpecfloError, match="'research'.*local only"):
        hosted.create_project("Other", level="research")
    with pytest.raises(SpecfloError, match="'research'.*local only"):
        hosted.import_project("thing", files)

    assert hosted.list_projects() == []
    assert list((tmp_path / "daemon").rglob("project.md")) == []
    # A checkout keeps taking both, as it did.
    copied = {"project.md": files["project.md"].replace("slug: thing", "slug: copy", 1)}
    assert local.import_project("copy", copied)
    assert local.load_project("copy").level == "research"


# --- specflo level: up to fast or full, never to quick, never to research ---------


def _research_files(project_dir):
    """The research document and every ledger with its bytes, by relative path."""
    return {
        name: data
        for name, data in _tree(project_dir).items()
        if name == "research.md" or name.startswith("research/")
    }


@pytest.mark.parametrize("target", ["fast", "full"])
def test_level_moves_a_research_project_up_to_brainstorm_and_scaffolds_the_brainstorm(
    tmp_path, monkeypatch, target
):
    root, _ = _research_project(tmp_path, monkeypatch)
    cfg = config.load_config(root)
    project_dir = projects.load_project(root, cfg, "thing").path
    research_before = _research_files(project_dir)
    assert len(research_before) == 1 + len(LEDGER_FILES)
    assert not (project_dir / "brainstorm.md").exists()

    result = _ok(["level", target])

    project = projects.load_project(root, cfg, "thing")
    assert (project.level, project.phase, project.status) == (target, "brainstorm", "active")
    assert f"Level for 'thing': {target}. Phase: brainstorm." in result.output
    assert "thing/brainstorm" in result.output
    assert "confirm or supersede" not in result.output
    brainstorm_text = (project_dir / "brainstorm.md").read_text()
    assert "project: thing" in brainstorm_text and "phase: brainstorm" in brainstorm_text
    assert _research_files(project_dir) == research_before


@pytest.mark.parametrize("target", ["fast", "full"])
def test_a_research_project_moved_up_reads_as_a_ladder_project_at_brainstorm(
    tmp_path, monkeypatch, target
):
    root, _ = _research_project(tmp_path, monkeypatch)
    cfg = config.load_config(root)

    _ok(["level", target])

    shown = _ok(["status"]).output
    info = json.loads(_ok(["status", "--json"]).output)
    assert "Phase:   brainstorm" in shown and f"Level: {target}" in shown
    assert (info["level"], info["phase"], info["next_phase"]) == (target, "brainstorm", "spec")
    assert "specflo research recap" not in info["next_step"]
    checkpoint_text = (projects.load_project(root, cfg, "thing").path / "checkpoint.md").read_text()
    assert "brainstorm" in checkpoint_text and "research recap" not in checkpoint_text
    # The research stays where the research commands find it: nothing is left
    # to make, and the document still reads as it was scaffolded.
    assert scaffold(root, cfg, "thing") == []
    assert document.load(root, cfg, "thing").destination == document.UNKNOWN_DESTINATION


def test_the_service_scaffolds_the_brainstorm_on_the_move_with_nothing_to_review(tmp_path):
    cfg = config.init_config(tmp_path)
    svc = LocalProjectService(tmp_path, cfg)
    project = svc.create_project("Thing", level="research")
    from specflo import research as research_module

    research_module.scaffold(tmp_path, cfg, "thing")
    research_before = _research_files(project.path)

    moved, review = svc.set_level("thing", "full")

    assert (moved.level, moved.phase, review) == ("full", "brainstorm", [])
    assert (project.path / "brainstorm.md").is_file()
    assert _research_files(project.path) == research_before


def test_level_quick_on_a_research_project_is_refused_naming_fast_and_full_and_changes_no_file(
    tmp_path, monkeypatch
):
    root, _ = _research_project(tmp_path, monkeypatch)
    projects_dir = projects.load_project(root, config.load_config(root), "thing").path.parent
    before = _tree(projects_dir)

    shown = _refused(["level", "quick"])

    assert shown.startswith("error: ")
    assert "'thing' is at research level" in shown
    assert "fast or full" in shown and "quick" in shown
    assert _tree(projects_dir) == before
    project = projects.load_project(root, config.load_config(root), "thing")
    assert (project.level, project.phase) == ("research", "research")


@pytest.mark.parametrize("start", ["research", "full"])
def test_level_research_is_refused_on_any_project_and_changes_no_file(
    tmp_path, monkeypatch, start
):
    root = _checkout(tmp_path / "local", monkeypatch)
    _ok(["new", "Thing", "--level", start])
    projects_dir = projects.load_project(root, config.load_config(root), "thing").path.parent
    before = _tree(projects_dir)

    shown = _refused(["level", "research"])

    assert shown.startswith("error: ")
    assert "No project moves to research level" in shown
    assert "`specflo new --level research`" in shown
    assert "fast or full" in shown
    assert _tree(projects_dir) == before
    assert projects.load_project(root, config.load_config(root), "thing").level == start


def test_level_on_a_research_project_refuses_an_unknown_level_as_before(tmp_path, monkeypatch):
    root, _ = _research_project(tmp_path, monkeypatch)
    projects_dir = projects.load_project(root, config.load_config(root), "thing").path.parent
    before = _tree(projects_dir)

    shown = _refused(["level", "huge"])

    assert "Unknown level 'huge'" in shown and "'fast', 'full'" in shown
    assert _tree(projects_dir) == before

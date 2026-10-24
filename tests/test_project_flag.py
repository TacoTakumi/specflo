"""The project override: a command names its project without moving the
stored active-project pointer."""

import json
import re
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from specflo import config, projects
from specflo.cli import app

runner = CliRunner()

SRC = Path(__file__).resolve().parent.parent / "src" / "specflo"


def _stored(root: Path) -> str | None:
    return yaml.safe_load(config.config_path(root).read_text()).get("active_project")


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A checkout with projects a (stored active) and b; the override is reset after."""
    monkeypatch.setattr(config, "_project_override", None)
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "a")
    projects.create_project(tmp_path, cfg, "b")
    config.set_active_project(cfg, "a")
    config.save_config(tmp_path, cfg)
    return tmp_path


def test_stored_pointer_survives_a_save_under_an_override(checkout):
    config.set_project_override("b")
    cfg = config.load_config(checkout)
    assert cfg.active_project == "b"

    config.save_config(checkout, cfg)

    assert _stored(checkout) == "a"


def test_stored_pointer_moves_through_the_helper_under_an_override(checkout):
    config.set_project_override("b")
    cfg = config.load_config(checkout)

    config.set_active_project(cfg, "b")
    config.save_config(checkout, cfg)
    assert _stored(checkout) == "b"

    cfg = config.load_config(checkout)
    config.set_active_project(cfg, None)
    config.save_config(checkout, cfg)
    assert _stored(checkout) is None


def test_stored_pointer_is_the_loaded_value_without_an_override(checkout):
    cfg = config.load_config(checkout)

    assert cfg.active_project == "a"
    assert cfg.project_override is None
    cfg.active_project = "b"
    config.save_config(checkout, cfg)
    assert _stored(checkout) == "b"


def test_stored_pointer_moves_only_through_the_helper():
    """Outside config.py no module assigns the pointer directly: under an
    override such an assignment would not be saved."""
    assignment = re.compile(r"\.active_project\s*=(?!=)")
    offenders = [
        f"{path.relative_to(SRC)}:{number}"
        for path in sorted(SRC.rglob("*.py"))
        if path.name != "config.py" or path.parent != SRC
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if assignment.search(line)
    ]
    assert offenders == []


# --- the root option -------------------------------------------------------


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A checkout made through the CLI: projects a and b, a stored active."""
    monkeypatch.chdir(tmp_path)
    for args in (["init"], ["new", "b", "--level", "quick"], ["new", "a", "--level", "quick"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.output
    assert _stored(tmp_path) == "a"
    return tmp_path


def _followups(root: Path, slug: str) -> str:
    path = root / "docs" / "projects" / slug / "followup.md"
    return path.read_text() if path.is_file() else ""


def _status_project(*args: str, env: dict | None = None) -> str:
    result = runner.invoke(app, [*args, "status", "--json"], env=env)
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["active_project"]


@pytest.mark.parametrize("flag", ["-p", "--project"])
def test_root_option_runs_a_verb_on_the_named_project(repo, flag):
    before = _followups(repo, "a")

    result = runner.invoke(app, [flag, "b", "followup", "add", "Left over", "--do", "Finish it"])

    assert result.exit_code == 0, result.output
    assert "Left over" in _followups(repo, "b")
    assert _followups(repo, "a") == before
    assert _stored(repo) == "a"


def test_root_option_precedence_flag_env_and_empty_env(repo):
    assert _status_project(env={"SPECFLO_PROJECT": "b"}) == "b"
    assert _status_project("-p", "a", env={"SPECFLO_PROJECT": "b"}) == "a"
    assert _status_project(env={"SPECFLO_PROJECT": ""}) == "a"
    assert _stored(repo) == "a"


def test_root_option_env_does_not_reach_the_file(repo):
    result = runner.invoke(
        app, ["config", "set", "autonomy", "safe"], env={"SPECFLO_PROJECT": "b"}
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["-p", "b", "config", "set", "autonomy", "safe"])
    assert result.exit_code == 0, result.output
    assert _stored(repo) == "a"


@pytest.mark.parametrize(
    "args", [["followup", "add", "X", "--do", "Y"], ["status"], ["decision", "list"]]
)
def test_root_option_unknown_project_fails_and_creates_nothing(repo, args):
    before = sorted(p.relative_to(repo) for p in repo.rglob("*"))

    result = runner.invoke(app, ["-p", "nosuch", *args])

    assert result.exit_code != 0
    assert "nosuch" in result.output and "specflo list" in result.output
    assert sorted(p.relative_to(repo) for p in repo.rglob("*")) == before


def test_root_option_unknown_project_from_env_fails(repo):
    result = runner.invoke(app, ["status"], env={"SPECFLO_PROJECT": "nosuch"})
    assert result.exit_code != 0
    assert "nosuch" in result.output


def test_root_option_ignored_by_commands_on_no_project(repo):
    result = runner.invoke(app, ["-p", "nosuch", "list"])
    assert result.exit_code == 0, result.output
    assert "a" in result.output and "b" in result.output

    result = runner.invoke(app, ["-p", "nosuch", "new", "c", "--level", "quick"])
    assert result.exit_code == 0, result.output
    assert (repo / "docs" / "projects" / "c" / "project.md").is_file()
    assert _stored(repo) == "c"


def test_root_option_with_directory_resolves_in_that_checkout(repo, tmp_path_factory, monkeypatch):
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    monkeypatch.chdir(elsewhere)

    result = runner.invoke(
        app, ["-C", str(repo), "-p", "b", "followup", "add", "From afar", "--do", "Do it"]
    )

    assert result.exit_code == 0, result.output
    assert "From afar" in _followups(repo, "b")
    assert _stored(repo) == "a"


# --- status ----------------------------------------------------------------


def _project_line(output: str) -> str:
    return next(line for line in output.splitlines() if line.startswith("Project:"))


def test_status_names_the_override_source(repo):
    result = runner.invoke(app, ["-p", "b", "status"])
    assert result.exit_code == 0, result.output
    assert _project_line(result.output).endswith("(via -p)")

    result = runner.invoke(app, ["status"], env={"SPECFLO_PROJECT": "b"})
    assert result.exit_code == 0, result.output
    assert _project_line(result.output).endswith("(via SPECFLO_PROJECT)")

    result = runner.invoke(app, ["-p", "b", "status", "--json"])
    assert json.loads(result.output)["project_source"] == "flag"
    result = runner.invoke(app, ["status", "--json"], env={"SPECFLO_PROJECT": "b"})
    assert json.loads(result.output)["project_source"] == "env"


def test_status_without_an_override_is_unchanged(repo):
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert _project_line(result.output) == "Project: a"

    result = runner.invoke(app, ["status", "--json"])
    assert "project_source" not in json.loads(result.output)


# --- the verbs that move the pointer ---------------------------------------


def _status_of(root: Path, slug: str) -> str:
    return projects.load_project(root, config.load_config(root), slug).status


def test_leave_with_another_project_changes_nothing(repo):
    result = runner.invoke(app, ["-p", "b", "leave"])
    assert result.exit_code == 0, result.output
    assert "'b' is not the active project" in result.output
    assert _stored(repo) == "a"

    result = runner.invoke(app, ["-p", "b", "leave", "--json"])
    assert json.loads(result.output) == {"left": None, "active_project": "a"}
    assert _stored(repo) == "a"


def test_leave_with_the_active_project_clears_it(repo):
    result = runner.invoke(app, ["-p", "a", "leave", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"left": "a", "active_project": None}
    assert _stored(repo) is None


def test_shelve_then_resume_act_on_the_named_project(repo):
    result = runner.invoke(app, ["-p", "b", "shelve"])
    assert result.exit_code == 0, result.output
    assert _status_of(repo, "b") == projects.SHELVED_STATUS
    assert _status_of(repo, "a") == "active"
    assert _stored(repo) == "a"

    result = runner.invoke(app, ["-p", "b", "resume"])
    assert result.exit_code == 0, result.output
    assert _status_of(repo, "b") == "active"
    assert _stored(repo) == "b"


def test_switch_takes_the_named_project(repo):
    result = runner.invoke(app, ["-p", "b", "switch"])
    assert result.exit_code == 0, result.output
    assert _stored(repo) == "b"


def test_switch_with_no_name_and_no_override_is_a_usage_error(repo):
    result = runner.invoke(app, ["switch"])
    assert result.exit_code == 2
    assert "<name>" in result.output
    assert _stored(repo) == "a"


# --- a name argument beside -p ---------------------------------------------


def _project_files(root: Path) -> dict[str, str]:
    return {
        slug: (root / "docs" / "projects" / slug / "project.md").read_text()
        for slug in ("a", "b")
    }


@pytest.mark.parametrize(
    "verb", [["shelve"], ["summary"], ["switch"], ["resume"]], ids=lambda v: v[0]
)
def test_name_conflict_with_the_override_is_refused(repo, verb):
    before = _project_files(repo)
    stored = _stored(repo)
    args = [*verb, "a"] + (["New summary"] if verb == ["summary"] else [])

    result = runner.invoke(app, ["-p", "b", *args])

    assert result.exit_code != 0
    assert "'a'" in result.output and "'b'" in result.output
    assert _project_files(repo) == before
    assert _stored(repo) == stored


def test_name_conflict_same_project_after_slugify_runs(repo):
    result = runner.invoke(app, ["-p", "b", "shelve", "B"])
    assert result.exit_code == 0, result.output
    assert _status_of(repo, "b") == projects.SHELVED_STATUS

    result = runner.invoke(app, ["-p", "b", "resume", "B"])
    assert result.exit_code == 0, result.output
    assert _stored(repo) == "b"

    result = runner.invoke(app, ["-p", "a", "summary", "A", "Same project"])
    assert result.exit_code == 0, result.output
    assert "Same project" in (repo / "docs" / "projects" / "a" / "project.md").read_text()

    result = runner.invoke(app, ["-p", "a", "switch", "A"])
    assert result.exit_code == 0, result.output
    assert _stored(repo) == "a"

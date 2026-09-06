"""The prose verbs: ``specflo doc show`` prints an artifact by name.

Agents read a project's artifacts through the CLI rather than opening files,
so the same verb serves a project whose files live in the checkout and one
whose files live behind a daemon.
"""

import pytest
from typer.testing import CliRunner

from specflo import config, doc, projects
from specflo.cli import app
from specflo.errors import SpecfloError

runner = CliRunner()


def _project(tmp_path, monkeypatch):
    """An active 'Thing' at ``tmp_path``, with the cwd moved into it."""
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing", created="2026-09-06")
    projects.switch_project(tmp_path, cfg, "Thing")
    monkeypatch.chdir(tmp_path)
    return tmp_path / "docs" / "projects" / "thing"


# --- the module ----------------------------------------------------------


def test_artifact_names_are_the_five_project_documents():
    assert list(doc.ARTIFACTS) == ["brainstorm", "spec", "plan", "checkpoint", "project"]
    assert doc.ARTIFACTS["project"] == "project.md"
    assert doc.ARTIFACTS["checkpoint"] == "checkpoint.md"


def test_show_document_returns_the_file_content_verbatim(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    cfg = config.load_config(tmp_path)
    body = "---\nproject: thing\n---\n\n# Brainstorm: thing\n\nno trailing newline"
    (project_dir / "brainstorm.md").write_text(body)

    assert doc.show_document(tmp_path, cfg, "thing", "brainstorm") == body


def test_show_document_refuses_an_unknown_name_listing_the_valid_ones(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    cfg = config.load_config(tmp_path)

    with pytest.raises(SpecfloError) as excinfo:
        doc.show_document(tmp_path, cfg, "thing", "notes")

    message = str(excinfo.value)
    assert "notes" in message
    for name in ("brainstorm", "spec", "plan", "checkpoint", "project"):
        assert name in message


def test_show_document_refuses_an_artifact_that_does_not_exist_yet(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    cfg = config.load_config(tmp_path)

    with pytest.raises(SpecfloError) as excinfo:
        doc.show_document(tmp_path, cfg, "thing", "spec")

    assert "spec" in str(excinfo.value)


# --- the CLI verb --------------------------------------------------------


def test_doc_show_prints_project_md_verbatim(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["doc", "show", "project"])

    assert result.exit_code == 0, result.output
    assert result.output == (project_dir / "project.md").read_text()


@pytest.mark.parametrize("name", ["brainstorm", "spec", "plan", "checkpoint"])
def test_doc_show_prints_each_artifact_by_name(tmp_path, monkeypatch, name):
    # project.md is covered above: it must keep valid front matter to load.
    project_dir = _project(tmp_path, monkeypatch)
    body = f"# The {name} document\n\nline two\n"
    (project_dir / doc.ARTIFACTS[name]).write_text(body)

    result = runner.invoke(app, ["doc", "show", name])

    assert result.exit_code == 0, result.output
    assert result.output == body


def test_doc_show_adds_nothing_to_the_content(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    body = "# Plan\n\nno trailing newline"
    (project_dir / "plan.md").write_text(body)

    result = runner.invoke(app, ["doc", "show", "plan"])

    assert result.exit_code == 0, result.output
    assert result.output == body


def test_doc_show_unknown_artifact_exits_non_zero_listing_valid_names(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["doc", "show", "notes"])

    assert result.exit_code != 0
    out = result.output
    assert "notes" in out
    for name in ("brainstorm", "spec", "plan", "checkpoint", "project"):
        assert name in out


def test_doc_show_missing_artifact_exits_non_zero(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["doc", "show", "spec"])

    assert result.exit_code != 0
    assert "spec" in result.output


def test_doc_show_needs_an_active_project(tmp_path, monkeypatch):
    config.init_config(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["doc", "show", "project"])

    assert result.exit_code != 0

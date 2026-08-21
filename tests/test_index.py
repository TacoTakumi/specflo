"""The project index ledger: specflo-index.md (project-index REQ-02, REQ-05).

One generated file at the projects dir root answering "what projects exist and
how do they bind new work": a mode-dependent rule header plus one table row per
project in created-date order, the active project marked.
"""

import pytest
from typer.testing import CliRunner

from specflo import config, projects
from specflo.cli import app
from specflo.index import INDEX_FILENAME, index_path, write_index

runner = CliRunner()


@pytest.fixture
def root(tmp_path):
    config.init_config(tmp_path)
    return tmp_path


@pytest.fixture
def cfg(root):
    return config.load_config(root)


def _rows(text):
    """The table's project rows: pipe-lines minus the header and separator."""
    lines = [ln for ln in text.splitlines() if ln.startswith("|")]
    return lines[2:]


def test_index_lands_at_the_projects_dir_root(root, cfg):
    projects.create_project(root, cfg, "Alpha", summary="A.")
    path = write_index(root, cfg)
    assert path == root / "docs" / "projects" / INDEX_FILENAME
    assert path == index_path(root, cfg)
    assert path.is_file()


def test_index_writes_one_row_per_project_in_created_order(root, cfg):
    projects.create_project(root, cfg, "Newer", created="2026-03-01", summary="N.")
    projects.create_project(root, cfg, "Older", created="2025-01-15", summary="O.")
    projects.create_project(root, cfg, "Middle", created="2025-06-30", summary="M.")

    rows = _rows(write_index(root, cfg).read_text())
    assert len(rows) == 3
    assert ["Older" in rows[0], "Middle" in rows[1], "Newer" in rows[2]] == [True] * 3


def test_index_rows_carry_the_frontmatter_fields(root, cfg):
    projects.create_project(root, cfg, "Thing", created="2026-01-02", summary="Does it.")
    p = projects.load_project(root, cfg, "thing")
    p.completed = "2026-02-03"
    p.status = projects.COMPLETE_STATUS
    (p.path / projects.PROJECT_FILENAME).write_text(projects._render(p))

    (row,) = _rows(write_index(root, cfg).read_text())
    for piece in ("Thing", "2026-01-02", "2026-02-03", "complete", "Does it."):
        assert piece in row


def test_index_leaves_completed_blank_while_unfinished(root, cfg):
    projects.create_project(root, cfg, "Thing", created="2026-01-02", summary="S.")
    (row,) = _rows(write_index(root, cfg).read_text())
    cells = [c.strip() for c in row.strip("|").split("|")]
    assert "" in cells  # the completed cell


def test_index_marks_the_active_project(root, cfg):
    projects.create_project(root, cfg, "One", created="2025-01-01", summary="1.")
    projects.create_project(root, cfg, "Two", created="2025-02-01", summary="2.")
    cfg.active_project = "two"

    rows = _rows(write_index(root, cfg).read_text())
    assert "(active)" not in rows[0]
    assert "(active)" in rows[1]


def test_index_header_defaults_to_the_historical_rule(root, cfg):
    projects.create_project(root, cfg, "Thing", summary="S.")
    text = write_index(root, cfg).read_text()
    assert config.rule_text("historical") in text
    assert config.rule_text("binding") not in text


def test_flipping_the_config_flips_the_header(root, cfg):
    projects.create_project(root, cfg, "Thing", summary="S.")
    write_index(root, cfg)

    cfg.prior_projects = "binding"
    text = write_index(root, cfg).read_text()
    assert config.rule_text("binding") in text
    assert config.rule_text("historical") not in text


def test_index_is_pure_ascii(root, cfg):
    projects.create_project(root, cfg, "Thing", summary="S.")
    write_index(root, cfg).read_text().encode("ascii")


def test_index_escapes_pipes_in_summaries(root, cfg):
    # An unescaped pipe would split the summary into an extra table cell.
    projects.create_project(root, cfg, "Thing", summary="a | b")
    (row,) = _rows(write_index(root, cfg).read_text())
    assert "a \\| b" in row


def test_index_write_takes_the_lock_seam(root, cfg):
    # The index is an artifact: its write must leave the seam's lock file.
    projects.create_project(root, cfg, "Thing", summary="S.")
    write_index(root, cfg)
    locks = list((root / ".specflo" / "locks").rglob(f"{INDEX_FILENAME}.lock"))
    assert locks


def test_index_command_writes_the_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "Thing", "--summary", "Does it."])

    result = runner.invoke(app, ["index"])
    assert result.exit_code == 0
    path = tmp_path / "docs" / "projects" / INDEX_FILENAME
    assert path.is_file()
    assert "Thing" in path.read_text()

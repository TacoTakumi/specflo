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


# --- the preserved Notes section (REQ-04) --------------------------------
# Explicit markers delimit the one human-owned area; regeneration carries it
# byte-for-byte and rewrites everything else.


def _reindex_with_notes(root, cfg, notes_body):
    """Regenerate, plant ``notes_body`` inside the markers, return the path."""
    from specflo.index import NOTES_BEGIN, NOTES_END

    path = write_index(root, cfg)
    text = path.read_text()
    begin = text.index(NOTES_BEGIN) + len(NOTES_BEGIN)
    end = text.index(NOTES_END)
    path.write_text(text[:begin] + notes_body + text[end:])
    return path


def test_a_fresh_index_has_an_empty_notes_section(root, cfg):
    from specflo.index import NOTES_BEGIN, NOTES_END

    projects.create_project(root, cfg, "Thing", summary="S.")
    text = write_index(root, cfg).read_text()
    assert "## Notes" in text
    assert text.index(NOTES_BEGIN) < text.index(NOTES_END)


def test_notes_survive_regeneration_byte_for_byte(root, cfg):
    projects.create_project(root, cfg, "Thing", summary="S.")
    body = "\nkeep me exactly:  two spaces,\n\n  indentation, all of it.\n"
    _reindex_with_notes(root, cfg, body)

    from specflo.index import NOTES_BEGIN, NOTES_END

    text = write_index(root, cfg).read_text()
    begin = text.index(NOTES_BEGIN) + len(NOTES_BEGIN)
    assert text[begin : text.index(NOTES_END)] == body


def test_edits_outside_the_notes_markers_are_replaced(root, cfg):
    projects.create_project(root, cfg, "Thing", summary="S.")
    path = write_index(root, cfg)
    path.write_text("GRAFFITI\n" + path.read_text())

    text = write_index(root, cfg).read_text()
    assert "GRAFFITI" not in text


def test_notes_with_a_lost_end_marker_recover_the_tail(root, cfg):
    from specflo.index import NOTES_BEGIN, NOTES_END

    projects.create_project(root, cfg, "Thing", summary="S.")
    path = _reindex_with_notes(root, cfg, "\nprecious note\n")
    path.write_text(path.read_text().replace(NOTES_END, ""))

    text = write_index(root, cfg).read_text()
    assert "precious note" in text
    assert text.index(NOTES_BEGIN) < text.index(NOTES_END)  # pair recreated


def test_notes_with_a_lost_begin_marker_recover_the_content(root, cfg):
    from specflo.index import NOTES_BEGIN, NOTES_END

    projects.create_project(root, cfg, "Thing", summary="S.")
    path = _reindex_with_notes(root, cfg, "\nprecious note\n")
    path.write_text(path.read_text().replace(NOTES_BEGIN, ""))

    text = write_index(root, cfg).read_text()
    assert "precious note" in text
    assert text.index(NOTES_BEGIN) < text.index(NOTES_END)


def test_summary_verb_regenerates_the_index_row_in_one_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "Thing"])
    runner.invoke(app, ["index"])
    assert "(needs summary)" in (tmp_path / "docs" / "projects" / INDEX_FILENAME).read_text()

    result = runner.invoke(app, ["summary", "Now summarized."])
    assert result.exit_code == 0
    text = (tmp_path / "docs" / "projects" / INDEX_FILENAME).read_text()
    assert "Now summarized." in text
    assert "(needs summary)" not in text


# --- completion banners (REQ-09) -----------------------------------------
# The final advance stamps a one-line blockquote under the frontmatter of
# brainstorm.md and spec.md - never plan.md - worded for the configured mode.


def _seed_artifacts(root, cfg, slug):
    pdir = root / "docs" / "projects" / slug
    for name in ("brainstorm.md", "spec.md", "plan.md"):
        (pdir / name).write_text(
            f"---\ntitle: {name}\nstatus: complete\n---\n\n# {name}\n\nBody.\n"
        )
    return pdir


def test_banner_lands_under_frontmatter_of_brainstorm_and_spec(root, cfg):
    from specflo import index as index_mod

    projects.create_project(root, cfg, "Thing", summary="S.")
    pdir = _seed_artifacts(root, cfg, "thing")
    project = projects.complete_project(root, cfg, "thing")

    index_mod.stamp_banners(root, cfg, project)
    for name in ("brainstorm.md", "spec.md"):
        text = (pdir / name).read_text()
        body = text.split("---", 2)[2].lstrip("\n")
        assert body.startswith("> Complete (")
        assert project.completed in body.splitlines()[0]
        assert "specflo-index.md" in body.splitlines()[0]
        assert "do not bind new work unless restated" in body.splitlines()[0]


def test_banner_never_touches_plan_md(root, cfg):
    from specflo import index as index_mod

    projects.create_project(root, cfg, "Thing", summary="S.")
    pdir = _seed_artifacts(root, cfg, "thing")
    before = (pdir / "plan.md").read_bytes()
    project = projects.complete_project(root, cfg, "thing")

    index_mod.stamp_banners(root, cfg, project)
    assert (pdir / "plan.md").read_bytes() == before


def test_banner_stamping_twice_leaves_exactly_one(root, cfg):
    from specflo import index as index_mod

    projects.create_project(root, cfg, "Thing", summary="S.")
    pdir = _seed_artifacts(root, cfg, "thing")
    project = projects.complete_project(root, cfg, "thing")

    index_mod.stamp_banners(root, cfg, project)
    index_mod.stamp_banners(root, cfg, project)
    assert (pdir / "spec.md").read_text().count("> Complete (") == 1


def test_banner_binding_mode_swaps_the_last_sentence(root, cfg):
    from specflo import index as index_mod

    projects.create_project(root, cfg, "Thing", summary="S.")
    pdir = _seed_artifacts(root, cfg, "thing")
    project = projects.complete_project(root, cfg, "thing")
    cfg.prior_projects = "binding"

    index_mod.stamp_banners(root, cfg, project)
    line = (pdir / "spec.md").read_text().split("---", 2)[2].lstrip("\n").splitlines()[0]
    assert "remain binding on new work unless explicitly superseded" in line
    assert "do not bind" not in line


def test_banner_is_one_ascii_line_and_frontmatter_still_parses(root, cfg):
    import yaml as _yaml

    from specflo import index as index_mod

    projects.create_project(root, cfg, "Thing", summary="S.")
    pdir = _seed_artifacts(root, cfg, "thing")
    project = projects.complete_project(root, cfg, "thing")

    index_mod.stamp_banners(root, cfg, project)
    text = (pdir / "brainstorm.md").read_text()
    text.encode("ascii")
    assert _yaml.safe_load(text.split("---", 2)[1]) == {
        "title": "brainstorm.md",
        "status": "complete",
    }


def test_banner_skips_a_missing_artifact(root, cfg):
    from specflo import index as index_mod

    projects.create_project(root, cfg, "Thing", summary="S.")
    project = projects.complete_project(root, cfg, "thing")
    index_mod.stamp_banners(root, cfg, project)  # no brainstorm/spec: no crash


def test_banner_stamped_by_the_final_cli_advance(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from test_cli import _project_at_execute

    _project_at_execute(runner, app, tmp_path)
    runner.invoke(app, ["task", "start", "T-01"])
    runner.invoke(app, ["task", "done", "T-01"])
    runner.invoke(app, ["advance"])

    pdir = tmp_path / "docs" / "projects" / "thing"
    for name in ("brainstorm.md", "spec.md"):
        body = (pdir / name).read_text().split("---", 2)[2].lstrip("\n")
        assert body.startswith("> Complete (")
    assert "> Complete (" not in (pdir / "plan.md").read_text()


# --- first-run backfill (REQ-10) -----------------------------------------
# The first `specflo index` in a repo with pre-existing projects adopts them:
# rows for everyone, banners and completed dates for the finished, and the
# needs-summary placeholder wherever frontmatter is silent.


def _age_project(root, slug, complete=True):
    """Make a project look pre-index: no summary field, optionally completed."""
    path = root / "docs" / "projects" / slug / "project.md"
    lines = [
        ln for ln in path.read_text().splitlines() if not ln.startswith("summary:")
    ]
    if complete:
        lines = [ln.replace("status: active", "status: complete") for ln in lines]
    path.write_text("\n".join(lines) + "\n")


def test_backfill_outside_git_stamps_banners_placeholders_and_unknown(root, cfg):
    projects.create_project(root, cfg, "Old One", created="2025-01-01")
    projects.create_project(root, cfg, "Old Two", created="2025-02-01")
    _seed_artifacts(root, cfg, "old-one")
    _seed_artifacts(root, cfg, "old-two")
    _age_project(root, "old-one")
    _age_project(root, "old-two", complete=False)

    text = write_index(root, cfg).read_text()
    rows = _rows(text)
    assert len(rows) == 2
    assert "unknown" in rows[0]              # completed, no git history
    assert "unknown" not in rows[1]          # unfinished: completed stays blank
    assert "(needs summary)" in rows[0] and "(needs summary)" in rows[1]

    # The completed project's artifacts carry the banner; the active one's don't.
    pdir = root / "docs" / "projects" / "old-one"
    for name in ("brainstorm.md", "spec.md"):
        assert "> Complete (" in (pdir / name).read_text()
    assert "> Complete (" not in (root / "docs" / "projects" / "old-two" / "spec.md").read_text()

    # The backfill persisted the fields, not just the rows.
    aged = projects.load_project(root, cfg, "old-one")
    assert aged.summary == "(needs summary)"
    assert aged.completed == "unknown"


def test_backfill_derives_the_completed_date_from_git(root, cfg):
    import subprocess

    projects.create_project(root, cfg, "Old", created="2025-01-01")
    _seed_artifacts(root, cfg, "old")
    _age_project(root, "old")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": __import__("os").environ["PATH"]}
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "history"], cwd=root, check=True, env=env)

    import datetime

    (row,) = _rows(write_index(root, cfg).read_text())
    assert datetime.date.today().isoformat() in row
    assert "unknown" not in row


def test_backfill_runs_only_on_the_first_index(root, cfg):
    projects.create_project(root, cfg, "Thing", summary="S.")
    write_index(root, cfg)                    # the index now exists

    _age_project(root, "thing")               # damage arrives afterwards
    write_index(root, cfg)
    text = (root / "docs" / "projects" / "thing" / "project.md").read_text()
    assert "summary:" not in text             # no backfill write happened
    assert "completed:" not in text


# --- the backfill handoff prompt (REQ-11) --------------------------------


def test_backfill_prompt_offers_a_distillation_pass(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "First"])
    runner.invoke(app, ["new", "Second"])
    _age_project(tmp_path, "first", complete=False)
    _age_project(tmp_path, "second", complete=False)

    out = runner.invoke(app, ["index"]).output
    assert "Offer the user" in out
    assert "specflo summary" in out
    assert "yes" in out


def test_backfill_prompt_absent_without_placeholders(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "First", "--summary", "Has one."])

    out = runner.invoke(app, ["index"]).output
    assert "Offer the user" not in out
    assert "specflo summary" not in out


def test_backfill_prompt_absent_on_a_rerun(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "First"])
    _age_project(tmp_path, "first", complete=False)
    runner.invoke(app, ["index"])             # the backfill run

    out = runner.invoke(app, ["index"]).output
    assert "Offer the user" not in out

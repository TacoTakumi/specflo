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


def test_artifact_names_are_the_six_project_documents():
    assert list(doc.ARTIFACTS) == ["brainstorm", "spec", "plan", "brief", "checkpoint", "project"]
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


# --- section set ---------------------------------------------------------


def _brainstorm_with_decisions(tmp_path, monkeypatch):
    """An active 'Thing' whose brainstorm carries three decisions."""
    from specflo import brainstorm

    _project(tmp_path, monkeypatch)
    cfg = config.load_config(tmp_path)
    brainstorm.start_brainstorm(tmp_path, cfg, "thing", today="2026-09-06")
    for text in ("first", "second", "third"):
        brainstorm.add_decision(tmp_path, cfg, "thing", text, today="2026-09-06")
    return cfg, tmp_path / "docs" / "projects" / "thing" / "brainstorm.md"


def test_set_section_replaces_one_body_and_keeps_every_decision(tmp_path, monkeypatch):
    from specflo import markdown

    cfg, path = _brainstorm_with_decisions(tmp_path, monkeypatch)
    before = path.read_text()

    doc.set_section(tmp_path, cfg, "thing", "brainstorm", "Current understanding",
                    "The converged synthesis.\n", today="2026-09-07")

    after = path.read_text()
    assert "## Current understanding\n" in after
    assert markdown.section_body(after, "## Current understanding") == "\nThe converged synthesis.\n\n"
    assert markdown.section_body(after, "## Decisions") == markdown.section_body(before, "## Decisions")
    # everything from the next header on is byte-identical
    assert after[after.index("## Research"):] == before[before.index("## Research"):]
    assert "updated: 2026-09-07" in after
    assert "updated: 2026-09-06" not in after


def test_set_section_accepts_the_header_with_its_hashes(tmp_path, monkeypatch):
    cfg, path = _brainstorm_with_decisions(tmp_path, monkeypatch)

    doc.set_section(tmp_path, cfg, "thing", "brainstorm", "## Open questions", "none\n")

    assert "## Open questions\n\nnone\n" in path.read_text()


def test_set_section_targets_a_subsection_without_touching_its_sibling(tmp_path, monkeypatch):
    from specflo import markdown, spec

    _project(tmp_path, monkeypatch)
    cfg = config.load_config(tmp_path)
    spec.start_spec(tmp_path, cfg, "thing", today="2026-09-06")
    path = tmp_path / "docs" / "projects" / "thing" / "spec.md"
    before = path.read_text()

    doc.set_section(tmp_path, cfg, "thing", "spec", "In scope", "- the CLI\n")

    after = path.read_text()
    assert markdown.section_body(after, "### In scope") == "\n- the CLI\n\n"
    assert markdown.section_body(after, "### Out of scope") == markdown.section_body(before, "### Out of scope")


@pytest.mark.parametrize(
    "section, verb",
    [
        ("Decisions", "decision add"),
        ("Requirements", "requirement add"),
        ("Tasks", "task add"),
        ("Milestones", "milestone add"),
        ("Pools", "pool add"),
    ],
)
def test_set_section_refuses_managed_sections_naming_the_verb(tmp_path, monkeypatch, section, verb):
    cfg, path = _brainstorm_with_decisions(tmp_path, monkeypatch)
    before = path.read_text()

    with pytest.raises(SpecfloError) as excinfo:
        doc.set_section(tmp_path, cfg, "thing", "brainstorm", section, "x\n")

    assert verb in str(excinfo.value)
    assert path.read_text() == before


def test_set_section_refuses_an_unknown_section_listing_the_prose_ones(tmp_path, monkeypatch):
    cfg, _path = _brainstorm_with_decisions(tmp_path, monkeypatch)

    with pytest.raises(SpecfloError) as excinfo:
        doc.set_section(tmp_path, cfg, "thing", "brainstorm", "Synthesis", "x\n")

    message = str(excinfo.value)
    assert "Synthesis" in message
    assert "Current understanding" in message and "Open questions" in message
    assert "Decisions" not in message


@pytest.mark.parametrize("name", ["checkpoint", "project"])
def test_set_section_refuses_artifacts_without_prose_sections(tmp_path, monkeypatch, name):
    cfg, _path = _brainstorm_with_decisions(tmp_path, monkeypatch)

    with pytest.raises(SpecfloError) as excinfo:
        doc.set_section(tmp_path, cfg, "thing", name, "Anything", "x\n")

    assert name in str(excinfo.value)


def test_set_section_refuses_a_missing_artifact(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    cfg = config.load_config(tmp_path)

    with pytest.raises(SpecfloError):
        doc.set_section(tmp_path, cfg, "thing", "spec", "Objective", "x\n")


def test_section_set_from_a_file(tmp_path, monkeypatch):
    from specflo import markdown

    _cfg, path = _brainstorm_with_decisions(tmp_path, monkeypatch)
    body = tmp_path / "synthesis.md"
    body.write_text("From a file.\n")

    result = runner.invoke(
        app, ["section", "set", "brainstorm", "Current understanding", "--file", str(body)]
    )

    assert result.exit_code == 0, result.output
    assert markdown.section_body(path.read_text(), "## Current understanding") == "\nFrom a file.\n\n"
    assert "thing/brainstorm" in result.output
    assert str(path) not in result.output


def test_section_set_from_stdin(tmp_path, monkeypatch):
    from specflo import markdown

    _cfg, path = _brainstorm_with_decisions(tmp_path, monkeypatch)

    result = runner.invoke(
        app, ["section", "set", "brainstorm", "Research", "--stdin"], input="From stdin.\n"
    )

    assert result.exit_code == 0, result.output
    assert markdown.section_body(path.read_text(), "## Research") == "\nFrom stdin.\n\n"


def test_section_set_needs_exactly_one_source(tmp_path, monkeypatch):
    _cfg, path = _brainstorm_with_decisions(tmp_path, monkeypatch)
    before = path.read_text()
    body = tmp_path / "b.md"
    body.write_text("x\n")

    neither = runner.invoke(app, ["section", "set", "brainstorm", "Research"])
    both = runner.invoke(
        app, ["section", "set", "brainstorm", "Research", "--stdin", "--file", str(body)],
        input="y\n",
    )

    assert neither.exit_code != 0
    assert both.exit_code != 0
    assert path.read_text() == before


def test_section_set_refuses_a_managed_section_on_the_cli(tmp_path, monkeypatch):
    _cfg, path = _brainstorm_with_decisions(tmp_path, monkeypatch)
    before = path.read_text()

    result = runner.invoke(
        app, ["section", "set", "brainstorm", "Decisions", "--stdin"], input="x\n"
    )

    assert result.exit_code != 0
    assert "decision add" in result.output
    assert path.read_text() == before


def test_section_set_refreshes_the_checkpoint(tmp_path, monkeypatch):
    _cfg, path = _brainstorm_with_decisions(tmp_path, monkeypatch)
    checkpoint_md = path.parent / "checkpoint.md"
    checkpoint_md.unlink(missing_ok=True)

    result = runner.invoke(
        app, ["section", "set", "brainstorm", "Research", "--stdin"], input="x\n"
    )

    assert result.exit_code == 0, result.output
    assert checkpoint_md.is_file()


# --- review rounds by number ----------------------------------------------


def test_artifact_filename_accepts_a_review_round_by_number():
    assert doc.artifact_filename("review-1") == "review-1.md"
    assert doc.artifact_filename("review-12") == "review-12.md"
    for name in ("review", "review-", "review-x", "review-1.md", "Review-1", "review-1/../x"):
        with pytest.raises(SpecfloError, match="Unknown artifact"):
            doc.artifact_filename(name)


def test_doc_show_prints_a_review_round_by_its_number(tmp_path, monkeypatch):
    from specflo import review

    project_dir = _project(tmp_path, monkeypatch)
    cfg = config.load_config(tmp_path)
    review.start_round(tmp_path, cfg, "thing", today="2026-08-01")
    review.close_round(tmp_path, cfg, "thing", "changes-requested", today="2026-08-02")

    result = runner.invoke(app, ["doc", "show", "review-1"])

    assert result.exit_code == 0, result.output
    assert result.output == (project_dir / "review-1.md").read_text()
    missing = runner.invoke(app, ["doc", "show", "review-2"])
    assert missing.exit_code != 0 and "review-2" in missing.output


def test_doc_show_unknown_artifact_names_the_round_pattern_too(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["doc", "show", "notes"])

    assert result.exit_code != 0
    assert "review-<N>" in result.output


def test_set_section_refuses_an_entry_inside_a_managed_section_naming_the_verb(tmp_path, monkeypatch):
    cfg, path = _brainstorm_with_decisions(tmp_path, monkeypatch)
    before = path.read_text()
    entry = next(h for h in before.splitlines() if h.startswith("### D-01"))

    with pytest.raises(SpecfloError) as excinfo:
        doc.set_section(tmp_path, cfg, "thing", "brainstorm", entry, "x\n")

    assert "decision add" in str(excinfo.value)
    assert path.read_text() == before
    listed = doc.prose_sections(before)
    assert "Current understanding" in listed
    assert not any(title.startswith("D-0") for title in listed)


def test_section_set_refuses_a_missing_or_unreadable_body_file(tmp_path, monkeypatch):
    _cfg, path = _brainstorm_with_decisions(tmp_path, monkeypatch)
    before = path.read_text()
    binary = tmp_path / "body.bin"
    binary.write_bytes(b"\xff\xfe\x00 not text")

    missing = runner.invoke(app, ["section", "set", "brainstorm", "Research", "--file", "does-not-exist.md"])
    unreadable = runner.invoke(app, ["section", "set", "brainstorm", "Research", "--file", str(binary)])

    assert missing.exit_code == 1
    assert "No body file at does-not-exist.md." in missing.stderr
    assert "Traceback" not in missing.stderr and missing.stdout == ""
    assert unreadable.exit_code == 1
    assert f"Cannot read {binary} as text" in unreadable.stderr
    assert "Traceback" not in unreadable.stderr and unreadable.stdout == ""
    assert path.read_text() == before


# --- the brief view ----------------------------------------------------------------


def _cli_ok(args, stdin=None):
    from typer.testing import CliRunner
    from specflo.cli import app
    result = CliRunner().invoke(app, args, input=stdin)
    assert result.exit_code == 0, (args, result.output)
    return result


def test_doc_show_brief_at_quick_prints_the_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _cli_ok(["init"])
    _cli_ok(["new", "Thing", "--level", "quick"])
    _cli_ok(["section", "set", "brief", "Goal", "--stdin"], "Fix it.\n")
    text = (tmp_path / "docs" / "projects" / "thing" / "brief.md").read_text()

    assert _cli_ok(["doc", "show", "brief"]).output == text


def test_doc_show_brief_at_fast_renders_one_page_and_writes_nothing(tmp_path, monkeypatch):
    from test_level import _fast_project_at_execute
    monkeypatch.chdir(tmp_path)
    _fast_project_at_execute(tmp_path)
    project_dir = tmp_path / "docs" / "projects" / "thing"
    spec_md = project_dir / "spec.md"
    spec_md.write_text(spec_md.read_text().replace(
        "## Objective\n", "## Objective\nMake the thing work.\n", 1))
    before = {p.name: p.read_bytes() for p in project_dir.iterdir()}

    out = _cli_ok(["doc", "show", "brief"]).output

    for heading in ("## Goal", "## Decisions", "## Checks", "## Tasks"):
        assert heading in out
    assert "Make the thing work." in out
    assert "D-01" in out and "choice 1" in out
    assert "REQ-01" in out and "it runs" in out
    assert "T-01" in out and "build it" in out and "done" in out
    assert {p.name: p.read_bytes() for p in project_dir.iterdir()} == before


def test_doc_show_brief_at_full_is_refused(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from specflo.cli import app
    monkeypatch.chdir(tmp_path)
    _cli_ok(["init"])
    _cli_ok(["new", "Thing"])

    result = CliRunner().invoke(app, ["doc", "show", "brief"])

    assert result.exit_code != 0
    assert "quick" in result.output and "fast" in result.output

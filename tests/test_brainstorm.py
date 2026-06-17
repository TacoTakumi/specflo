import pytest

from specflo import brainstorm, config, projects
from specflo.errors import SpecfloError


@pytest.fixture
def root(tmp_path):
    config.init_config(tmp_path)
    return tmp_path


@pytest.fixture
def cfg(root):
    return config.load_config(root)


@pytest.fixture
def project(root, cfg):
    """Create a project and return its slug."""
    projects.create_project(root, cfg, "My Thing", created="2026-06-15")
    return "my-thing"


def test_start_creates_brainstorm_with_frontmatter(root, cfg, project):
    path, created = brainstorm.start_brainstorm(root, cfg, project, today="2026-06-16")
    assert created is True
    assert path == root / "docs" / "projects" / "my-thing" / "brainstorm.md"
    text = path.read_text()
    assert "project: my-thing" in text
    assert "phase: brainstorm" in text
    assert "status: draft" in text
    assert "created: 2026-06-16" in text
    assert "updated: 2026-06-16" in text
    assert "# Brainstorm: My Thing" in text
    assert "## Decisions" in text
    assert "## Out of scope / Deferred" in text
    assert "## Open questions" in text


def test_start_is_idempotent_and_does_not_clobber(root, cfg, project):
    path, created_first = brainstorm.start_brainstorm(root, cfg, project, today="2026-06-16")
    path.write_text(path.read_text() + "\nUSER MARKER\n")
    path_again, created_second = brainstorm.start_brainstorm(root, cfg, project, today="2026-06-17")
    assert created_first is True
    assert created_second is False
    assert path_again == path
    assert "USER MARKER" in path.read_text()  # not overwritten


def test_start_on_missing_project_raises(root, cfg):
    with pytest.raises(SpecfloError):
        brainstorm.start_brainstorm(root, cfg, "ghost")


def _bpath(root, cfg, project):
    return brainstorm.brainstorm_path(root, cfg, project)


def test_add_decision_assigns_sequential_ids(root, cfg, project):
    brainstorm.start_brainstorm(root, cfg, project, today="2026-06-16")
    d1 = brainstorm.add_decision(
        root, cfg, project, "Use SQLite", rationale="simplest", today="2026-06-16"
    )
    d2 = brainstorm.add_decision(
        root, cfg, project, "One file per project", today="2026-06-16"
    )
    assert d1.id == "D-01"
    assert d2.id == "D-02"
    text = _bpath(root, cfg, project).read_text()
    assert "### D-01 — Use SQLite" in text
    assert "- Rationale: simplest" in text
    assert "### D-02 — One file per project" in text
    assert "- Rationale: —" in text  # default when omitted


def test_add_decision_bumps_updated_only(root, cfg, project):
    brainstorm.start_brainstorm(root, cfg, project, today="2026-06-16")
    brainstorm.add_decision(root, cfg, project, "Something", today="2026-06-20")
    text = _bpath(root, cfg, project).read_text()
    assert "updated: 2026-06-20" in text
    assert "created: 2026-06-16" in text  # created is unchanged


def test_add_decision_without_start_raises(root, cfg, project):
    with pytest.raises(SpecfloError):
        brainstorm.add_decision(root, cfg, project, "Too early")


def test_decisions_stay_inside_their_section(root, cfg, project):
    brainstorm.start_brainstorm(root, cfg, project, today="2026-06-16")
    brainstorm.add_decision(root, cfg, project, "Inside", today="2026-06-16")
    text = _bpath(root, cfg, project).read_text()
    assert text.index("## Decisions") < text.index("### D-01") < text.index("## Out of scope")


def test_supersede_marks_old_and_links_new(root, cfg, project):
    brainstorm.start_brainstorm(root, cfg, project, today="2026-06-16")
    brainstorm.add_decision(root, cfg, project, "Use JSON", today="2026-06-16")
    d2 = brainstorm.add_decision(
        root, cfg, project, "Use YAML", rationale="frontmatter", supersedes="D-01",
        today="2026-06-16",
    )
    assert d2.id == "D-02"
    assert d2.supersedes == "D-01"
    text = _bpath(root, cfg, project).read_text()
    assert "### D-01 — Use JSON" in text          # kept in place
    assert "- Status: superseded by D-02" in text  # old one flipped
    assert "- Supersedes: D-01" in text            # new one links back


def test_supersede_unknown_decision_raises(root, cfg, project):
    brainstorm.start_brainstorm(root, cfg, project, today="2026-06-16")
    with pytest.raises(SpecfloError):
        brainstorm.add_decision(root, cfg, project, "x", supersedes="D-99", today="2026-06-16")


def test_ids_never_collide_after_supersede(root, cfg, project):
    brainstorm.start_brainstorm(root, cfg, project, today="2026-06-16")
    brainstorm.add_decision(root, cfg, project, "a", today="2026-06-16")                       # D-01
    brainstorm.add_decision(root, cfg, project, "b", supersedes="D-01", today="2026-06-16")    # D-02
    d3 = brainstorm.add_decision(root, cfg, project, "c", today="2026-06-16")                  # D-03
    assert d3.id == "D-03"


def _fill_out_of_scope(root, cfg, project):
    path = _bpath(root, cfg, project)
    path.write_text(
        path.read_text().replace(
            "## Out of scope / Deferred\n"
            "<!-- required, must be non-empty before validate passes -->",
            "## Out of scope / Deferred\nNo auth in v0.1.",
        )
    )
    return path


def test_validate_passes_a_complete_doc(root, cfg, project):
    brainstorm.start_brainstorm(root, cfg, project, today="2026-06-16")
    brainstorm.add_decision(root, cfg, project, "A real decision", today="2026-06-16")
    _fill_out_of_scope(root, cfg, project)
    assert brainstorm.validate_brainstorm(root, cfg, project) == []


def test_validate_flags_missing_file(root, cfg, project):
    issues = brainstorm.validate_brainstorm(root, cfg, project)
    assert any("not found" in i for i in issues)


def test_validate_flags_no_decisions(root, cfg, project):
    brainstorm.start_brainstorm(root, cfg, project, today="2026-06-16")
    _fill_out_of_scope(root, cfg, project)  # isolate the decisions check
    issues = brainstorm.validate_brainstorm(root, cfg, project)
    assert any("no decisions" in i for i in issues)


def test_validate_flags_empty_out_of_scope(root, cfg, project):
    brainstorm.start_brainstorm(root, cfg, project, today="2026-06-16")
    brainstorm.add_decision(root, cfg, project, "A decision", today="2026-06-16")
    issues = brainstorm.validate_brainstorm(root, cfg, project)
    assert any("Out of scope" in i for i in issues)


def test_validate_flags_placeholder_text(root, cfg, project):
    brainstorm.start_brainstorm(root, cfg, project, today="2026-06-16")
    brainstorm.add_decision(root, cfg, project, "A decision", today="2026-06-16")
    _fill_out_of_scope(root, cfg, project)
    path = _bpath(root, cfg, project)
    path.write_text(path.read_text().replace("A decision", "TODO decide later"))
    issues = brainstorm.validate_brainstorm(root, cfg, project)
    assert any("placeholder" in i for i in issues)


# Fix B — word-boundary placeholder tests


def test_validate_no_false_positive_for_embedded_todo(root, cfg, project):
    """AUTODOCK contains the substring 'TODO'; must NOT trip the placeholder check."""
    brainstorm.start_brainstorm(root, cfg, project, today="2026-06-16")
    brainstorm.add_decision(root, cfg, project, "Use AUTODOCK for docking", today="2026-06-16")
    _fill_out_of_scope(root, cfg, project)
    issues = brainstorm.validate_brainstorm(root, cfg, project)
    placeholder_issues = [i for i in issues if "placeholder" in i]
    assert placeholder_issues == [], f"False positive placeholder issue(s): {placeholder_issues}"


def test_validate_no_false_positive_for_embedded_tbd(root, cfg, project):
    """STBD contains the substring 'TBD'; must NOT trip the placeholder check."""
    brainstorm.start_brainstorm(root, cfg, project, today="2026-06-16")
    brainstorm.add_decision(root, cfg, project, "Use STBD protocol", today="2026-06-16")
    _fill_out_of_scope(root, cfg, project)
    issues = brainstorm.validate_brainstorm(root, cfg, project)
    placeholder_issues = [i for i in issues if "placeholder" in i]
    assert placeholder_issues == [], f"False positive placeholder issue(s): {placeholder_issues}"


# Fix A — fence-aware section parsing tests


def _doc_with_fenced_prose(root, cfg, project):
    """Return a brainstorm path whose 'Current understanding' section contains a fenced
    code block that has a fake '## Decisions' header and a fake '### D-99 — fake' line,
    followed by the real ## Decisions and ## Out of scope sections.
    """
    brainstorm.start_brainstorm(root, cfg, project, today="2026-06-16")
    path = _bpath(root, cfg, project)
    text = path.read_text()
    # Inject a fenced block into Current understanding that mirrors the doc structure
    fenced_prose = (
        "## Current understanding\n"
        "Here is an example of our artifact structure:\n"
        "```\n"
        "## Decisions\n"
        "### D-99 — fake decision\n"
        "- Status: active\n"
        "```\n"
    )
    text = text.replace("## Current understanding\n", fenced_prose)
    path.write_text(text)
    return path


def test_add_decision_skips_fenced_section_headers(root, cfg, project):
    """add_decision must insert into the REAL ## Decisions section, not a fenced one."""
    path = _doc_with_fenced_prose(root, cfg, project)
    d = brainstorm.add_decision(root, cfg, project, "Real decision", today="2026-06-16")
    # ID must be D-01 (D-99 in the fence must not count)
    assert d.id == "D-01"
    text = path.read_text()
    # The new entry must appear AFTER the real ## Decisions and BEFORE ## Out of scope
    idx_real_decisions = text.index("## Decisions\n<!-- append-only")
    idx_new_entry = text.index("### D-01 — Real decision")
    idx_out_of_scope = text.index("## Out of scope")
    assert idx_real_decisions < idx_new_entry < idx_out_of_scope, (
        "New decision landed outside the real Decisions section"
    )


def test_validate_reads_real_out_of_scope_not_fenced(root, cfg, project):
    """validate must read the REAL Out-of-scope section; a fenced empty one in prose
    must not cause a false 'empty' issue, and the real non-empty one must pass.
    """
    _doc_with_fenced_prose(root, cfg, project)
    brainstorm.add_decision(root, cfg, project, "Real decision", today="2026-06-16")
    # Fill the REAL Out of scope section (not in the fenced block)
    path = _bpath(root, cfg, project)
    text = path.read_text()
    text = text.replace(
        "## Out of scope / Deferred\n"
        "<!-- required, must be non-empty before validate passes -->",
        "## Out of scope / Deferred\nNo auth in v0.1.",
    )
    path.write_text(text)
    issues = brainstorm.validate_brainstorm(root, cfg, project)
    oos_issues = [i for i in issues if "Out of scope" in i]
    assert oos_issues == [], f"Unexpected Out-of-scope issue(s): {oos_issues}"


def test_complete_brainstorm_flips_status_and_bumps_updated(root, cfg, project):
    brainstorm.start_brainstorm(root, cfg, project, today="2026-06-16")

    brainstorm.complete_brainstorm(root, cfg, project, today="2026-06-20")

    text = _bpath(root, cfg, project).read_text()
    assert "status: complete" in text
    assert "status: draft" not in text
    assert "updated: 2026-06-20" in text


def test_complete_brainstorm_without_file_raises(root, cfg, project):
    with pytest.raises(SpecfloError):
        brainstorm.complete_brainstorm(root, cfg, project)


def test_complete_brainstorm_leaves_decision_status_untouched(root, cfg, project):
    brainstorm.start_brainstorm(root, cfg, project, today="2026-06-16")
    brainstorm.add_decision(root, cfg, project, "A real decision", today="2026-06-16")

    brainstorm.complete_brainstorm(root, cfg, project, today="2026-06-20")

    text = _bpath(root, cfg, project).read_text()
    assert "status: complete" in text  # frontmatter flipped
    assert "- Status: active" in text  # decision entry left alone

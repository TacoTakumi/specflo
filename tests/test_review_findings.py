"""Review findings: one ``- F-NN (severity) text`` line per finding.

``specflo review finding add`` records a finding in the open round's
Findings section. The ID is numbered across every round of the project, so a
later round can name an earlier finding by ID alone.
"""

import json
import multiprocessing

import pytest
from typer.testing import CliRunner

from reviewhelp import fix_active_open_items
from specflo import config, markdown, projects, review
from specflo.cli import app

runner = CliRunner()


def _project(tmp_path, monkeypatch):
    """An active 'Thing' at ``tmp_path``, with the cwd moved into it."""
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing", created="2026-08-22")
    projects.switch_project(tmp_path, cfg, "Thing")
    monkeypatch.chdir(tmp_path)
    return tmp_path / "docs" / "projects" / "thing"


def _add(severity, text):
    return runner.invoke(app, ["review", "finding", "add", "--severity", severity, "--text", text])


def _findings(path):
    return markdown.section_body(path.read_text(), "## Findings")


def _closed_round(project_dir, number, findings):
    path = project_dir / f"review-{number}.md"
    lines = "\n".join(findings)
    path.write_text(
        f"---\nround: {number}\nverdict: changes-requested\ndate: '2026-08-22'\n"
        f"sha: abc1234\nreason: ''\n---\n\n# Review round {number}\n\n"
        f"## Scope reviewed\n\n## Findings\n\n{lines}\n\n## Verdict\n"
    )
    return path


def test_add_appends_the_finding_line_and_prints_its_id(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    runner.invoke(app, ["review", "start"])

    result = _add("blocker", "The close drops the sha")

    assert result.exit_code == 0, result.output
    assert "F-01" in result.output
    assert _findings(project_dir / "review-1.md").strip() == "- F-01 (blocker) The close drops the sha"


def test_add_keeps_earlier_findings_in_order(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    runner.invoke(app, ["review", "start"])

    _add("blocker", "One")
    _add("should-fix", "Two")
    result = _add("nit", "Three")

    assert "F-03" in result.output
    assert _findings(project_dir / "review-1.md").strip().splitlines() == [
        "- F-01 (blocker) One",
        "- F-02 (should-fix) Two",
        "- F-03 (nit) Three",
    ]
    # The next heading is still where it was, one blank line below the list.
    assert "- F-03 (nit) Three\n\n## Verdict\n" in (project_dir / "review-1.md").read_text()


def test_add_json_carries_the_id_and_the_round(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    runner.invoke(app, ["review", "start"])

    result = runner.invoke(
        app, ["review", "finding", "add", "--severity", "nit", "--text", "Typo", "--json"]
    )

    data = json.loads(result.output)
    assert data["id"] == "F-01"
    assert data["severity"] == "nit"
    assert data["locator"] == "thing/review-1"
    assert data["path"] == str(project_dir / "review-1.md")


def test_add_with_no_open_round_refuses_naming_review_start(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    closed = _closed_round(project_dir, 1, ["- F-01 (blocker) One"])
    before = closed.read_text()

    result = _add("blocker", "Two")

    assert result.exit_code != 0
    assert "specflo review start" in result.output
    assert closed.read_text() == before
    assert not (project_dir / "review-2.md").exists()


def test_add_refuses_an_unknown_severity_listing_the_three(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    runner.invoke(app, ["review", "start"])
    path = project_dir / "review-1.md"
    before = path.read_text()

    result = _add("major", "Something")

    assert result.exit_code != 0
    for severity in ("blocker", "should-fix", "nit"):
        assert severity in result.output
    assert path.read_text() == before


def test_add_refuses_an_empty_or_multi_line_text(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    runner.invoke(app, ["review", "start"])
    path = project_dir / "review-1.md"
    before = path.read_text()

    for text in ("", "   ", "one\ntwo", "one\rtwo", "one\n"):
        result = _add("nit", text)
        assert result.exit_code != 0, repr(text)
        assert "non-empty" in result.output or "one line" in result.output, repr(text)
        assert path.read_text() == before, repr(text)


def test_ids_continue_across_rounds(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    _closed_round(
        project_dir, 1,
        ["- F-01 (blocker) One", "- F-02 (should-fix) Two", "- F-03 (nit) Three"],
    )
    fix_active_open_items()
    runner.invoke(app, ["review", "start"])

    result = _add("nit", "Four")

    assert result.exit_code == 0, result.output
    assert "F-04" in result.output
    assert _findings(project_dir / "review-2.md").strip() == "- F-04 (nit) Four"


def _add_in(args):
    root, text = args
    cfg = config.load_config(root)
    return review.add_finding(root, cfg, "thing", "nit", text)[0]


def test_concurrent_adds_get_distinct_ids(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    runner.invoke(app, ["review", "start"])
    jobs = [(tmp_path, f"Finding {i}") for i in range(6)]

    with multiprocessing.get_context("fork").Pool(len(jobs)) as pool:
        ids = pool.map(_add_in, jobs)

    assert sorted(ids) == [f"F-{n:02d}" for n in range(1, 7)]
    lines = _findings(project_dir / "review-1.md").strip().splitlines()
    assert len(lines) == 6
    for i in range(6):
        assert sum(line.endswith(f" Finding {i}") for line in lines) == 1


# --- review done derives the verdict from the findings -----------------------


def _open(project_dir, findings=None):
    """Start the next round; with ``findings``, write them as its Findings section."""
    fix_active_open_items()
    assert runner.invoke(app, ["review", "start"]).exit_code == 0
    path = review.open_round(project_dir.parents[2], config.load_config(project_dir.parents[2]), "thing")
    if findings is not None:
        path.write_text(
            markdown.replace_section_body(path.read_text(), "## Findings", "\n".join(findings))
        )
    return path


def _verdict(path):
    return review.frontmatter(path)["verdict"]


@pytest.mark.parametrize(
    "findings, verdict, counts",
    [
        (["- F-01 (blocker) The close drops the sha"], "changes-requested", "1 blocker, 0 should-fix, 0 nit"),
        (["- F-01 (should-fix) A message names the wrong command"], "changes-requested", "0 blocker, 1 should-fix, 0 nit"),
        (["- F-01 (nit) A name reads oddly", "- F-02 (nit) A typo"], "ready-to-merge", "0 blocker, 0 should-fix, 2 nit"),
        (["- none"], "ready-to-merge", "0 blocker, 0 should-fix, 0 nit"),
    ],
)
def test_done_derives_the_verdict_and_prints_the_counts(tmp_path, monkeypatch, findings, verdict, counts):
    project_dir = _project(tmp_path, monkeypatch)
    path = _open(project_dir, findings)

    result = runner.invoke(app, ["review", "done"])

    assert result.exit_code == 0, result.output
    assert _verdict(path) == verdict
    assert verdict in result.output
    assert counts in result.output


def test_done_json_carries_the_verdict_and_the_counts(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    _open(project_dir)
    _add("blocker", "One")
    _add("nit", "Two")

    data = json.loads(runner.invoke(app, ["review", "done", "--json"]).output)

    assert data["verdict"] == "changes-requested"
    assert data["findings"] == {"blocker": 1, "should-fix": 0, "nit": 1}
    assert data["locator"] == "thing/review-1"


def _refused(result, path, before, *names):
    assert result.exit_code != 0, result.output
    for name in names:
        assert name in result.output, (name, result.output)
    # The three ways on.
    assert "rewrite" in result.output
    assert "specflo review finding add" in result.output
    assert "specflo review waive" in result.output
    assert path.read_text() == before
    assert not _verdict(path)


@pytest.mark.parametrize(
    "findings, named",
    [
        (["- one nit, free-form"], "- one nit, free-form"),
        (["- F-01 (major) Not a severity"], "- F-01 (major) Not a severity"),
        (["- F-01 (nit) One", "  More prose under it"], "More prose under it"),
        (["- F-01 (nit) One", "- F-01 (blocker) Two"], "F-01"),
        (["- none", "- F-01 (nit) One"], "- none"),
        ([], "- none"),
    ],
)
def test_done_refuses_a_malformed_round_and_leaves_it_open(tmp_path, monkeypatch, findings, named):
    project_dir = _project(tmp_path, monkeypatch)
    path = _open(project_dir, findings)
    before = path.read_text()

    _refused(runner.invoke(app, ["review", "done"]), path, before, named)


def test_done_refuses_an_id_an_earlier_round_already_used(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    _closed_round(project_dir, 1, ["- F-01 (blocker) One", "- F-02 (nit) Two"])
    path = _open(project_dir, ["- F-02 (should-fix) Again"])
    before = path.read_text()

    _refused(runner.invoke(app, ["review", "done"]), path, before, "F-02")


def test_done_refuses_a_round_with_no_findings_section(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    path = project_dir / "review-1.md"
    path.write_text(
        "---\nround: 1\nverdict: ''\ndate: '2026-08-22'\nsha: ''\nreason: ''\n---\n\n# Review round 1\n"
    )
    before = path.read_text()

    _refused(runner.invoke(app, ["review", "done"]), path, before, "- none")


def test_done_accepts_an_explicit_verdict_only_when_it_is_the_derived_one(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    path = _open(project_dir, ["- F-01 (blocker) One", "- F-02 (nit) Two"])
    before = path.read_text()

    refused = runner.invoke(app, ["review", "done", "--verdict", "ready-to-merge"])

    assert refused.exit_code != 0
    assert "changes-requested" in refused.output and "F-01" in refused.output
    assert "F-02" not in refused.output            # a nit is not why
    assert path.read_text() == before

    accepted = runner.invoke(app, ["review", "done", "--verdict", "changes-requested"])

    assert accepted.exit_code == 0, accepted.output
    assert _verdict(path) == "changes-requested"


def test_done_waived_closes_without_reading_the_findings(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    path = _open(project_dir, ["- F-01 (blocker) One", "- free-form prose"])

    result = runner.invoke(app, ["review", "done", "--verdict", "waived", "--reason", "x"])

    assert result.exit_code == 0, result.output
    fields = review.frontmatter(path)
    assert (fields["verdict"], fields["reason"]) == ("waived", "x")


def test_done_derives_the_verdict_from_an_ingested_report(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    path = _open(project_dir)
    report = tmp_path / "report.md"
    report.write_text("# Round 1\n\n## Scope reviewed\n\n- the branch\n\n## Findings\n\n- F-01 (should-fix) One\n")

    result = runner.invoke(app, ["review", "done", "--file", str(report)])

    assert result.exit_code == 0, result.output
    assert _verdict(path) == "changes-requested"
    assert review.body_of(path) == report.read_text()


# --- nits go to one follow-up per round --------------------------------------


def _open_followups(root):
    from specflo import followup

    return followup.list_followups(root, config.load_config(root))


def test_a_round_with_nits_adds_one_followup_naming_them(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    _closed_round(project_dir, 1, ["- F-01 (blocker) One"])
    _open(project_dir, ["- F-02 (nit) A name", "- F-03 (should-fix) Two", "- F-04 (nit) A typo"])
    assert runner.invoke(app, ["review", "finding", "check", "F-01", "closed"]).exit_code == 0

    assert runner.invoke(app, ["review", "done"]).exit_code == 0

    entries = _open_followups(tmp_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.title == "Nits from review round 2"
    assert entry.do == "Decide which of F-02, F-04 to fix"
    assert entry.source == "review-2.md"
    assert entry.project == "thing"
    listed = runner.invoke(app, ["followup", "list"]).output
    assert "Nits from review round 2" in listed


@pytest.mark.parametrize(
    "findings, extra",
    [
        (["- none"], []),
        (["- F-01 (blocker) One"], []),
        (["- F-01 (nit) One"], ["--verdict", "waived", "--reason", "x"]),
    ],
)
def test_a_round_without_nits_or_a_waived_one_adds_no_followup(tmp_path, monkeypatch, findings, extra):
    project_dir = _project(tmp_path, monkeypatch)
    _open(project_dir, findings)

    assert runner.invoke(app, ["review", "done", *extra]).exit_code == 0

    assert _open_followups(tmp_path) == []

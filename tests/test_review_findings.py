"""Review findings: one ``- F-NN (severity) text`` line per finding.

``specflo review finding add`` records a finding in the open round's
Findings section. The ID is numbered across every round of the project, so a
later round can name an earlier finding by ID alone.
"""

import json
import multiprocessing

from typer.testing import CliRunner

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

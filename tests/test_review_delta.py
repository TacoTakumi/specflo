"""Delta rounds: every round after the first reviewed one reads only the diff.

``review start`` stamps the round with HEAD and the project's level when it
opens. After a reviewed round, the next round is a delta round: it prints the
range from the latest reviewed round's sha to HEAD, and the earlier blocker
and should-fix items it must check. ``--full`` asks for the whole branch and
keeps the items.
"""

import json
import subprocess

from typer.testing import CliRunner

from specflo import config, projects, review
from specflo.cli import app

runner = CliRunner()


def _project(tmp_path, monkeypatch, level="full"):
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing", created="2026-08-22", level=level)
    projects.switch_project(tmp_path, cfg, "Thing")
    monkeypatch.chdir(tmp_path)
    return tmp_path / "docs" / "projects" / "thing"


def _git(path, *args):
    return subprocess.run(
        ["git", *args], cwd=path, capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit(path, name):
    """Commit one new file and return the short sha."""
    (path / name).write_text(f"{name}\n")
    _git(path, "add", name)
    _git(path, "commit", "-qm", name)
    return _git(path, "rev-parse", "--short", "HEAD")


def _repo(path):
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "T")
    return _commit(path, "seed.txt")


def _round(project_dir, number, verdict, sha, findings, extra=""):
    path = project_dir / f"review-{number}.md"
    lines = "\n".join(findings)
    path.write_text(
        f"---\nround: {number}\nverdict: {verdict}\ndate: '2026-08-22'\n"
        f"sha: '{sha}'\n{extra}reason: ''\n---\n\n# Review round {number}\n\n"
        f"## Scope reviewed\n\n## Findings\n\n{lines}\n\n## Verdict\n"
    )
    return path


def _start(*args):
    result = runner.invoke(app, ["review", "start", *args])
    assert result.exit_code == 0, result.output
    return result


def _fields(path):
    return review.frontmatter(path)


# --- the stamps at open -------------------------------------------------------


def test_start_stamps_head_and_the_level(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch, level="fast")
    sha = _repo(tmp_path)

    _start()

    fields = _fields(project_dir / "review-1.md")
    assert fields["sha"] == sha
    assert fields["level"] == "fast"


def test_close_keeps_the_sha_stamped_at_open(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    opened_at = _repo(tmp_path)
    _start()
    _commit(tmp_path, "later.txt")
    path = project_dir / "review-1.md"
    path.write_text(path.read_text().replace("## Findings\n", "## Findings\n\n- none\n"))

    assert runner.invoke(app, ["review", "done"]).exit_code == 0

    assert _fields(path)["sha"] == opened_at


def test_close_fills_an_empty_sha_with_head(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    head = _repo(tmp_path)
    path = _round(project_dir, 1, "''", "", ["- none"])

    assert runner.invoke(app, ["review", "done"]).exit_code == 0

    assert _fields(path)["sha"] == head


# --- the scope ------------------------------------------------------------------


def test_the_first_round_is_a_whole_branch_round_with_no_range(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)

    result = _start()

    assert "Scope: whole branch" in result.output
    assert ".." not in result.output
    data = json.loads(runner.invoke(app, ["review", "start", "--json"]).output)
    assert (data["scope"], data["range"], data["items"]) == ("whole-branch", None, [])


def test_a_round_after_a_reviewed_one_is_a_delta_with_the_items_to_check(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    _round(project_dir, 1, "changes-requested", "abc1234",
           ["- F-01 (blocker) One", "- F-02 (should-fix) Two", "- F-03 (nit) Three"])

    result = _start()

    assert "abc1234..HEAD" in result.output
    assert "F-01" in result.output and "F-02" in result.output
    assert "F-03" not in result.output                  # a nit is never an item
    assert _fields(project_dir / "review-2.md")["base"] == "abc1234"


def test_a_waived_round_leaves_the_range_at_the_last_reviewed_round(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    _round(project_dir, 1, "changes-requested", "abc1234",
           ["- F-01 (blocker) One", "- F-02 (should-fix) Two"])
    _round(project_dir, 2, "waived", "def5678", ["- none"])

    result = _start()

    assert "abc1234..HEAD" in result.output
    assert "def5678" not in result.output
    assert "F-01" in result.output and "F-02" in result.output


def test_full_asks_for_the_whole_branch_and_keeps_the_items(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    _round(project_dir, 1, "changes-requested", "abc1234",
           ["- F-01 (blocker) One", "- F-02 (should-fix) Two"])

    result = _start("--full")

    assert "Scope: whole branch" in result.output
    assert "abc1234" not in result.output
    assert "F-01" in result.output and "F-02" in result.output
    assert _fields(project_dir / "review-2.md")["base"] == ""


def test_json_carries_the_scope_the_range_and_the_items(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    _round(project_dir, 1, "changes-requested", "abc1234",
           ["- F-01 (blocker) One", "- F-02 (should-fix) Two"])

    data = json.loads(runner.invoke(app, ["review", "start", "--json"]).output)

    assert data["scope"] == "delta"
    assert data["range"] == "abc1234..HEAD"
    assert data["items"] == ["F-01", "F-02"]
    assert data["locator"] == "thing/review-2"


def test_an_open_round_keeps_the_scope_it_opened_with(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    _round(project_dir, 1, "changes-requested", "abc1234", ["- F-01 (blocker) One"])
    _start("--full")

    again = _start()

    assert "(already open)" in again.output
    assert "Scope: whole branch" in again.output
    assert "F-01" in again.output


def test_the_scope_is_a_read_only_service_operation(tmp_path, monkeypatch):
    from specflo.service import LocalProjectService

    project_dir = _project(tmp_path, monkeypatch)
    _round(project_dir, 1, "changes-requested", "abc1234", ["- F-01 (blocker) One"])
    _start()
    path = project_dir / "review-2.md"
    before = path.read_text()
    svc = LocalProjectService(tmp_path, config.load_config(tmp_path))

    scope = svc.review_scope("thing")

    assert (scope["scope"], scope["range"], scope["items"]) == ("delta", "abc1234..HEAD", ["F-01"])
    assert path.read_text() == before

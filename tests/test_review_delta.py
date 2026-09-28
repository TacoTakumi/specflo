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

from reviewhelp import fix_active_open_items
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
    # A round opens only once each open item has a done fix task.
    fix_active_open_items()
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
    fix_active_open_items()

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


# --- checking earlier items ------------------------------------------------------


def _check(finding_id, state):
    return runner.invoke(app, ["review", "finding", "check", finding_id, state])


def _earlier(path):
    from specflo import markdown

    body = markdown.section_body(path.read_text(), "## Earlier findings")
    return None if body is None else body.strip().splitlines()


def _delta(project_dir):
    """Round 1 closed with blocker F-01, should-fix F-02 and nit F-03; round 2 open."""
    _round(project_dir, 1, "changes-requested", "abc1234",
           ["- F-01 (blocker) One", "- F-02 (should-fix) Two", "- F-03 (nit) Three"])
    _start()
    return project_dir / "review-2.md"


def test_check_writes_the_line_into_earlier_findings(tmp_path, monkeypatch):
    path = _delta(_project(tmp_path, monkeypatch))

    result = _check("F-01", "closed")

    assert result.exit_code == 0, result.output
    assert _earlier(path) == ["- F-01 closed"]
    # The section sits above Findings, which is still there and still empty.
    text = path.read_text()
    assert text.index("## Earlier findings") < text.index("## Findings")


def test_a_second_check_of_an_item_replaces_the_first(tmp_path, monkeypatch):
    path = _delta(_project(tmp_path, monkeypatch))

    _check("F-01", "open")
    _check("F-02", "closed")
    _check("F-01", "closed")

    assert _earlier(path) == ["- F-01 closed", "- F-02 closed"]


def test_check_refuses_what_is_not_an_item_and_leaves_the_file(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    path = _delta(project_dir)
    added = runner.invoke(app, ["review", "finding", "add", "--severity", "blocker", "--text", "New"])
    assert "F-04" in added.output
    before = path.read_text()

    for finding_id, state, why in (
        ("F-03", "closed", "nit"),                  # a nit is never checked
        ("F-04", "closed", "this round"),           # a finding of the round itself
        ("F-09", "closed", "F-09"),                 # nobody recorded it
        ("F-01", "fixed", "closed"),                # not a state
    ):
        result = _check(finding_id, state)
        assert result.exit_code != 0, (finding_id, state)
        assert why in result.output, (finding_id, result.output)
        assert path.read_text() == before, finding_id


def test_check_refuses_an_item_an_earlier_round_checked_closed(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    _round(project_dir, 1, "changes-requested", "abc1234",
           ["- F-01 (blocker) One", "- F-02 (should-fix) Two"])
    (project_dir / "review-2.md").write_text(
        "---\nround: 2\nverdict: changes-requested\ndate: '2026-08-22'\nsha: 'bcd2345'\n"
        "reason: ''\n---\n\n# Review round 2\n\n## Earlier findings\n\n- F-01 closed\n- F-02 open\n\n"
        "## Findings\n\n- F-03 (blocker) Three\n"
    )
    result = _start("--over-budget")
    assert "F-01" not in result.output.split("Items to check:")[1]
    path = project_dir / "review-3.md"
    before = path.read_text()

    refused = _check("F-01", "closed")

    assert refused.exit_code != 0
    assert "checked closed" in refused.output
    assert path.read_text() == before
    assert _check("F-02", "closed").exit_code == 0
    assert _check("F-03", "closed").exit_code == 0


def test_check_with_no_open_round_refuses(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    _round(project_dir, 1, "changes-requested", "abc1234", ["- F-01 (blocker) One"])

    result = _check("F-01", "closed")

    assert result.exit_code != 0
    assert "specflo review start" in result.output


# --- review done waits for every item ----------------------------------------------


def _none(path):
    path.write_text(path.read_text().replace("## Findings\n", "## Findings\n\n- none\n"))


def test_done_refuses_until_every_item_is_checked(tmp_path, monkeypatch):
    path = _delta(_project(tmp_path, monkeypatch))
    _none(path)
    _check("F-01", "closed")
    before = path.read_text()

    refused = runner.invoke(app, ["review", "done"])

    assert refused.exit_code != 0
    assert "F-02" in refused.output
    assert "specflo review finding check" in refused.output
    assert path.read_text() == before
    assert not _fields(path)["verdict"]

    _check("F-02", "closed")
    closed = runner.invoke(app, ["review", "done"])

    assert closed.exit_code == 0, closed.output
    assert _fields(path)["verdict"] == "ready-to-merge"


def test_a_full_round_checks_the_items_too(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    _round(project_dir, 1, "changes-requested", "abc1234", ["- F-01 (blocker) One"])
    _start("--full")
    path = project_dir / "review-2.md"
    _none(path)

    assert runner.invoke(app, ["review", "done"]).exit_code != 0
    _check("F-01", "closed")
    assert runner.invoke(app, ["review", "done"]).exit_code == 0


def test_an_item_checked_open_asks_for_changes_even_with_none(tmp_path, monkeypatch):
    path = _delta(_project(tmp_path, monkeypatch))
    _none(path)
    _check("F-01", "open")
    _check("F-02", "closed")

    result = runner.invoke(app, ["review", "done"])

    assert result.exit_code == 0, result.output
    assert _fields(path)["verdict"] == "changes-requested"
    assert "F-01" in result.output
    # The next round still has F-01 to check, and not F-02.
    nxt = _start("--over-budget")
    items = nxt.output.split("Items to check:")[1]
    assert "F-01" in items and "F-02" not in items


def test_done_refuses_a_malformed_earlier_findings_line(tmp_path, monkeypatch):
    path = _delta(_project(tmp_path, monkeypatch))
    _none(path)
    _check("F-01", "closed")
    _check("F-02", "closed")
    path.write_text(path.read_text().replace("- F-02 closed", "- F-02 was fixed"))
    before = path.read_text()

    result = runner.invoke(app, ["review", "done"])

    assert result.exit_code != 0
    assert "- F-02 was fixed" in result.output
    assert path.read_text() == before


# --- a round opened before the review restamps when the review begins ---------


def test_start_restamps_an_untouched_open_round_with_head(tmp_path, monkeypatch):
    # A ladder opens the full level's round when it climbs, long before the
    # review; the reviewer reads the commit HEAD names when it begins.
    project_dir = _project(tmp_path, monkeypatch)
    opened_at = _repo(tmp_path)
    _start()
    later = _commit(tmp_path, "work.txt")

    again = _start()

    assert "(already open)" in again.output
    assert _fields(project_dir / "review-1.md")["sha"] == later
    assert later != opened_at


def test_start_keeps_the_sha_of_a_round_already_under_review(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    opened_at = _repo(tmp_path)
    _start()
    runner.invoke(app, ["review", "finding", "add", "--severity", "nit", "--text", "Begun"])
    _commit(tmp_path, "work.txt")

    _start()

    assert _fields(project_dir / "review-1.md")["sha"] == opened_at


def test_start_leaves_an_untouched_round_whose_frontmatter_does_not_parse(
    tmp_path, monkeypatch
):
    # A hand-mangled round is its author's to fix: a restamp would write round,
    # base and level back empty.
    project_dir = _project(tmp_path, monkeypatch)
    _repo(tmp_path)
    _start()
    path = project_dir / "review-1.md"
    path.write_text(path.read_text().replace("round: 1", "round: [1", 1))
    before = path.read_text()
    _commit(tmp_path, "work.txt")

    again = _start()

    assert "(already open)" in again.output
    assert path.read_text() == before

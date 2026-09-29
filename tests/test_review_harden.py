"""Harden rounds: a fresh review of the whole scope, outside the gate's budget.

``review start --harden`` mints the next round of the same review-N.md series
with ``kind: harden`` in its frontmatter. A harden round always reviews its
whole scope, so it has no base, and the round budget neither refuses nor
counts it. A round file with no kind is a gate round, as every round file
written before harden rounds is.
"""

import json

from typer.testing import CliRunner

from reviewhelp import fix_active_open_items
from specflo import config, projects, review
from specflo.cli import app
from test_hosted_parity import (
    SLUG,
    _assert_the_daemon_ran_no_git,
    _fix,
    _hosted_steps,
    _local_steps,
    _recording_git,
)
from test_review_settle import _round_one, _snapshot

runner = CliRunner()


def _project(path, monkeypatch, level="full"):
    path.mkdir(parents=True, exist_ok=True)
    cfg = config.init_config(path)
    projects.create_project(path, cfg, "Thing", created="2026-09-28", level=level)
    projects.switch_project(path, cfg, "Thing")
    monkeypatch.chdir(path)
    return path / "docs" / "projects" / "thing"


def _round(project_dir, number, verdict, sha, findings, extra=""):
    path = project_dir / f"review-{number}.md"
    lines = "\n".join(findings)
    path.write_text(
        f"---\nround: {number}\nverdict: {verdict}\ndate: '2026-09-28'\n"
        f"sha: '{sha}'\nbase: ''\nlevel: full\n{extra}reason: ''\n---\n\n"
        f"# Review round {number}\n\n## Scope reviewed\n\n## Findings\n\n{lines}\n\n"
        "## Verdict\n"
    )
    return path


def _spent(path, monkeypatch, fixed=True):
    """A full-level project whose gate budget of 2 is spent: two rounds that
    asked for changes, each at a sha. ``fixed`` adds a done fix task for each
    open item. The project's directory."""
    project_dir = _project(path, monkeypatch)
    _round(project_dir, 1, "changes-requested", "abc1234", ["- F-01 (blocker) One"])
    _round(project_dir, 2, "changes-requested", "def5678", ["- F-02 (should-fix) Two"])
    if fixed:
        fix_active_open_items()
    return project_dir


def _budget(path):
    return review.budget(path, config.load_config(path), "thing")


def _start(*args):
    return runner.invoke(app, ["review", "start", *args])


# --- opening a harden round ---------------------------------------------------------


def test_harden_opens_the_next_round_past_a_spent_budget_with_kind_harden_and_no_base(
    tmp_path, monkeypatch
):
    project_dir = _spent(tmp_path, monkeypatch)
    assert _budget(tmp_path)["used"] == 2 and _budget(tmp_path)["spent"]
    assert _start().exit_code != 0                     # a gate round needs --over-budget
    assert not (project_dir / "review-3.md").exists()

    result = _start("--harden")

    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0] == "thing/review-3"
    fields = review.frontmatter(project_dir / "review-3.md")
    assert (fields["round"], fields["kind"], fields["base"], fields["verdict"]) == (
        3, "harden", "", ""
    )
    assert review.round_kind(fields) == review.HARDEN
    # The budget neither counts the harden round nor is reset by it.
    assert _budget(tmp_path)["used"] == 2
    assert _budget(tmp_path)["spent"]


def test_harden_reviews_the_whole_scope_and_checks_every_open_item(tmp_path, monkeypatch):
    _spent(tmp_path, monkeypatch)

    result = _start("--harden")

    assert result.exit_code == 0, result.output
    assert "Kind: harden" in result.output.splitlines()
    assert "Scope: whole branch" in result.output
    assert "def5678" not in result.output
    assert "Items to check: F-01, F-02" in result.output


def test_harden_json_names_the_kind(tmp_path, monkeypatch):
    _spent(tmp_path, monkeypatch)

    data = json.loads(_start("--harden", "--json").stdout)

    assert data == {
        "locator": "thing/review-3", "path": data["path"], "created": True, "kind": "harden",
        "scope": "whole-branch", "range": None, "items": ["F-01", "F-02"],
    }
    assert data["path"].endswith("review-3.md")


def test_harden_with_full_or_over_budget_gives_the_same_round(tmp_path, monkeypatch):
    minted = {}
    for flags in (("--harden",), ("--harden", "--full"), ("--harden", "--over-budget")):
        checkout = tmp_path / "-".join(flag.strip("-") for flag in flags)
        project_dir = _spent(checkout, monkeypatch)

        result = _start(*flags)

        assert result.exit_code == 0, (flags, result.output)
        minted[flags] = (result.output, (project_dir / "review-3.md").read_text())
        assert _budget(checkout)["used"] == 2
    assert len(set(minted.values())) == 1, minted


def test_harden_still_needs_a_done_fix_task_for_each_open_item(tmp_path, monkeypatch):
    project_dir = _spent(tmp_path, monkeypatch, fixed=False)
    before = _snapshot(tmp_path / "docs")

    for flags in (("--harden",), ("--harden", "--full"), ("--harden", "--over-budget")):
        result = _start(*flags)

        assert result.exit_code != 0, flags
        assert "No review round opens while an open item has no done fix task" in result.output
        assert "F-01" in result.output and "F-02" in result.output
    assert _snapshot(tmp_path / "docs") == before
    assert not (project_dir / "review-3.md").exists()


# --- an open round -------------------------------------------------------------------


def test_harden_hands_back_an_open_harden_round(tmp_path, monkeypatch):
    project_dir = _spent(tmp_path, monkeypatch)
    assert _start("--harden").exit_code == 0
    before = (project_dir / "review-3.md").read_text()

    again = _start("--harden")

    assert again.exit_code == 0, again.output
    assert again.output.splitlines()[:2] == ["thing/review-3 (already open)", "Kind: harden"]
    assert (project_dir / "review-3.md").read_text() == before
    assert not (project_dir / "review-4.md").exists()


def test_a_plain_start_hands_back_an_open_harden_round_as_a_harden_round(tmp_path, monkeypatch):
    project_dir = _spent(tmp_path, monkeypatch)
    assert _start("--harden").exit_code == 0

    again = _start()
    data = json.loads(_start("--json").stdout)

    assert again.exit_code == 0, again.output
    assert again.output.splitlines()[:2] == ["thing/review-3 (already open)", "Kind: harden"]
    assert (data["created"], data["kind"]) == (False, "harden")
    assert review.frontmatter(project_dir / "review-3.md")["kind"] == "harden"


def test_harden_with_a_gate_round_open_is_refused_and_changes_no_file(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    assert _start().exit_code == 0
    before = _snapshot(tmp_path / "docs")

    for flags in (("--harden",), ("--harden", "--full"), ("--harden", "--json")):
        result = _start(*flags)

        assert result.exit_code != 0, flags
        assert result.stdout == "", flags
        assert "review-1.md is an open gate round" in result.output
        assert "specflo review done" in result.output
        assert "specflo review waive --reason" in result.output
    assert _snapshot(tmp_path / "docs") == before
    assert review.round_kind(review.frontmatter(project_dir / "review-1.md")) == review.GATE


# --- a round with no kind -------------------------------------------------------------


def test_a_round_file_with_no_kind_reads_as_a_gate_round(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    path = _round(project_dir, 1, "changes-requested", "abc1234", ["- F-01 (blocker) One"])

    assert "kind" not in review.frontmatter(path)
    assert review.round_kind(review.frontmatter(path)) == review.GATE
    assert review.round_kind({}) == review.GATE
    assert _budget(tmp_path)["used"] == 1


def test_a_gate_round_is_minted_with_no_kind_and_counts_toward_the_budget(
    tmp_path, monkeypatch
):
    project_dir = _project(tmp_path, monkeypatch)

    result = _start()
    data = json.loads(_start("--json").stdout)

    assert result.exit_code == 0, result.output
    assert not any(line.startswith("Kind:") for line in result.output.splitlines())
    assert data["kind"] == "gate"
    text = (project_dir / "review-1.md").read_text()
    assert "kind" not in review.frontmatter(project_dir / "review-1.md")
    assert "\nkind:" not in text
    assert review.round_kind(review.frontmatter(project_dir / "review-1.md")) == review.GATE
    assert _budget(tmp_path)["used"] == 1


def test_a_hand_written_harden_kind_is_left_out_of_the_budget(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    _round(project_dir, 1, "changes-requested", "abc1234", ["- F-01 (blocker) One"])
    _round(project_dir, 2, "changes-requested", "def5678", ["- F-02 (should-fix) Two"],
           extra="kind: harden\n")

    state = _budget(tmp_path)

    assert (state["used"], state["spent"]) == (1, False)


# --- a hosted harden round -------------------------------------------------------------


def _harden_steps():
    """Two rounds that ask for changes spend the gate budget of 2, and each
    open item gets a done fix task; a plain start is then refused, a harden
    start opens round 3, and starting it again with --full hands it back."""
    return [
        *_round_one(),
        *_fix("Name the right command", "F-01", "T-02"),
        (["review", "start"], None),
        (["review", "finding", "check", "F-01", "open"], None),
        (["review", "finding", "add", "--severity", "blocker", "--at", "src/app.py:20",
          "--text", "The lock is dropped early"], None),
        (["review", "done"], None),
        *_fix("Keep the lock", "F-02", "T-03"),
        (["review", "start"], None),
        (["review", "start", "--harden"], None),
        (["review", "start", "--harden", "--full"], None),
        (["doc", "show", "review-3"], None),
    ]


def test_a_hosted_harden_start_writes_the_same_round_as_a_local_one(
    tmp_path, monkeypatch, live_daemon
):
    steps = _harden_steps()
    local, local_dir = _local_steps(tmp_path, monkeypatch, steps)
    git = _recording_git(monkeypatch)
    hosted, hosted_dir = _hosted_steps(tmp_path, monkeypatch, live_daemon, steps)

    assert len(local) == len(hosted) == len(steps) + 1
    for mine, theirs in zip(local[1:], hosted[1:], strict=True):
        assert mine == theirs, f"{' '.join(mine[0])}:\nlocal:\n{mine[2]}\nhosted:\n{theirs[2]}"
    starts = [(code, text) for args, code, text in hosted[1:] if args[:2] == ["review", "start"]]
    assert starts[-3][0] != 0                          # the gate budget is spent
    assert starts[-2] == (0, f"{SLUG}/review-3\nKind: harden\nScope: whole branch\n"
                             "Items to check: F-01, F-02\n")
    assert starts[-1][1].startswith(f"{SLUG}/review-3 (already open)\nKind: harden\n")
    for rounds in (local_dir, hosted_dir):
        fields = review.frontmatter(rounds / "review-3.md")
        assert (fields["kind"], fields["base"]) == ("harden", "")
        assert not (rounds / "review-4.md").exists()
    assert (local_dir / "review-3.md").read_text() == (hosted_dir / "review-3.md").read_text()
    # The JSON of a hosted start names the kind; it carries no path.
    data = json.loads(runner.invoke(app, ["review", "start", "--harden", "--json"]).stdout)
    assert (data["locator"], data["path"], data["created"], data["kind"]) == (
        f"{SLUG}/review-3", None, False, "harden"
    )
    _assert_the_daemon_ran_no_git(git, live_daemon["root"])

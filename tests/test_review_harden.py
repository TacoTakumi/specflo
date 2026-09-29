"""Harden rounds: a fresh review of the whole scope, outside the gate's budget.

``review start --harden`` mints the next round of the same review-N.md series
with ``kind: harden`` in its frontmatter. A harden round always reviews its
whole scope, so it has no base, and the round budget neither refuses nor
counts it. A round file with no kind is a gate round, as every round file
written before harden rounds is.
"""

import json

import pytest
from typer.testing import CliRunner

from reviewhelp import fix_active_open_items, write_none
from specflo import config, followup, projects, review
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


def _cli(*args):
    """Run a CLI command that must succeed; its result."""
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, (args, result.output)
    return result


def _at(monkeypatch, sha):
    """Make ``sha`` the HEAD the CLI reads from this checkout."""
    monkeypatch.setattr(review, "head_sha", lambda root: sha)


def _asked(path, monkeypatch, harden=True, sha="b0b0b0b"):
    """A full-level project whose round 1 asked for changes on blocker F-01
    at abc1234, with a done fix task for it, and round 2 open at ``sha``: a
    harden round, or a gate round. The project's directory."""
    project_dir = _project(path, monkeypatch)
    _round(project_dir, 1, "changes-requested", "abc1234", ["- F-01 (blocker) [src/app.py:1] One"])
    fix_active_open_items()
    _at(monkeypatch, sha)
    _cli("review", "start", *(["--harden"] if harden else []))
    return project_dir


def _should_fix_and_nit():
    """Record should-fix F-02 and nit F-03 in the open round."""
    _cli("review", "finding", "add", "--severity", "should-fix", "--at", "src/app.py:5",
         "--text", "The lock is dropped early")
    _cli("review", "finding", "add", "--severity", "nit", "--text", "A name is vague")


def _followups(path):
    return followup.list_followups(path, config.load_config(path), include_closed=True)


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


# --- closing a harden round -----------------------------------------------------------


def test_a_harden_round_closes_hardened_whatever_it_found_and_files_no_nits_followup(
    tmp_path, monkeypatch
):
    project_dir = _asked(tmp_path, monkeypatch)
    _cli("review", "finding", "check", "F-01", "closed")
    _should_fix_and_nit()

    result = _cli("review", "done")

    assert result.stdout == "thing/review-2 closed hardened (0 blocker, 1 should-fix, 1 nit)\n"
    fields = review.frontmatter(project_dir / "review-2.md")
    assert (fields["verdict"], fields["kind"], fields["sha"]) == ("hardened", "harden", "b0b0b0b")
    assert "- F-03 (nit) A name is vague" in (project_dir / "review-2.md").read_text()
    assert _followups(tmp_path) == []
    # The should-fix is an item the next round checks; the nit never is.
    fix_active_open_items()
    started = _cli("review", "start")
    assert "Items to check: F-02" in started.stdout.splitlines()


def test_a_gate_round_with_the_same_findings_still_asks_for_changes_and_files_its_nits(
    tmp_path, monkeypatch
):
    project_dir = _asked(tmp_path, monkeypatch, harden=False)
    _cli("review", "finding", "check", "F-01", "closed")
    _should_fix_and_nit()

    result = _cli("review", "done")

    assert result.stdout == (
        "thing/review-2 closed changes-requested (0 blocker, 1 should-fix, 1 nit)\n"
    )
    assert review.frontmatter(project_dir / "review-2.md")["verdict"] == "changes-requested"
    [entry] = _followups(tmp_path)
    assert (entry.title, entry.do, entry.source) == (
        "Nits from review round 2", "Decide which of F-03 to fix", "review-2.md"
    )


def test_a_gate_round_after_a_harden_round_reviews_the_delta_from_its_sha(
    tmp_path, monkeypatch
):
    project_dir = _asked(tmp_path, monkeypatch, sha="b0b0b0b")
    _cli("review", "finding", "check", "F-01", "open")
    _should_fix_and_nit()
    assert _cli("review", "done").stdout.startswith("thing/review-2 closed hardened")
    fix_active_open_items()
    _at(monkeypatch, "c0c0c0c")

    started = _cli("review", "start")

    fields = review.frontmatter(project_dir / "review-3.md")
    assert (fields["base"], fields["sha"], review.round_kind(fields)) == (
        "b0b0b0b", "c0c0c0c", review.GATE
    )
    assert started.stdout.splitlines()[1:] == [
        "Scope: b0b0b0b..HEAD", "Items to check: F-01, F-02"
    ]
    # The fixes were made after the harden round, so a pin test fails at its sha.
    assert "at `b0b0b0b`, the latest reviewed round's sha," in _cli("review", "prompt").stdout
    # The budget counts the two gate rounds and skips the harden round between them.
    assert _budget(tmp_path)["used"] == 2


def test_a_harden_round_that_checks_an_item_closed_drops_it_from_the_next_items(
    tmp_path, monkeypatch
):
    project_dir = _asked(tmp_path, monkeypatch)
    _cli("review", "finding", "check", "F-01", "closed")
    write_none(project_dir / "review-2.md")
    assert _cli("review", "done").stdout == (
        "thing/review-2 closed hardened (0 blocker, 0 should-fix, 0 nit)\n"
    )

    started = _cli("review", "start")

    assert started.stdout == "thing/review-3\nScope: b0b0b0b..HEAD\n"
    assert review.items_to_check(tmp_path, config.load_config(tmp_path), "thing", 3) == []
    refused = runner.invoke(app, ["review", "finding", "check", "F-01", "open"])
    assert refused.exit_code != 0
    assert "F-01 was already checked closed in review-2.md" in refused.output


def test_a_harden_round_is_the_first_reviewed_round_for_regression_marks(
    tmp_path, monkeypatch
):
    project_dir = _project(tmp_path, monkeypatch)
    cfg = config.load_config(tmp_path)
    _round(project_dir, 1, "hardened", "b0b0b0b", ["- F-01 (nit) One"], extra="kind: harden\n")
    review.start_round(tmp_path, cfg, "thing", sha="c0c0c0c")
    assert review.review_scope(tmp_path, cfg, "thing")["first_reviewed_sha"] == "b0b0b0b"
    assert review.frontmatter(project_dir / "review-2.md")["base"] == "b0b0b0b"
    review.add_finding(tmp_path, cfg, "thing", "blocker", "It broke", "src/app.py:3")

    closed = review.close_round(tmp_path, cfg, "thing", regressions=["F-02"])

    assert (closed.verdict, closed.regressions) == ("changes-requested", 1)
    assert "- F-02 (blocker, regression) [src/app.py:3] It broke" in (
        project_dir / "review-2.md"
    ).read_text()


# --- an explicit verdict for a harden round ---------------------------------------------


@pytest.mark.parametrize("verdict", ["ready-to-merge", "changes-requested"])
def test_a_harden_round_refuses_a_gate_verdict_and_changes_no_file(
    tmp_path, monkeypatch, verdict
):
    project_dir = _asked(tmp_path, monkeypatch)
    _cli("review", "finding", "check", "F-01", "closed")
    write_none(project_dir / "review-2.md")
    before = _snapshot(tmp_path / "docs")

    result = runner.invoke(app, ["review", "done", "--verdict", verdict])

    assert result.exit_code != 0
    assert (
        f"review-2.md is a harden round, so it closes hardened, not {verdict}."
        in result.output
    )
    assert _snapshot(tmp_path / "docs") == before


def test_a_harden_round_takes_verdict_hardened(tmp_path, monkeypatch):
    project_dir = _asked(tmp_path, monkeypatch)
    _cli("review", "finding", "check", "F-01", "closed")
    _should_fix_and_nit()

    result = _cli("review", "done", "--verdict", "hardened")

    assert result.stdout == "thing/review-2 closed hardened (0 blocker, 1 should-fix, 1 nit)\n"
    assert review.frontmatter(project_dir / "review-2.md")["verdict"] == "hardened"


def test_a_gate_round_refuses_verdict_hardened(tmp_path, monkeypatch):
    project_dir = _asked(tmp_path, monkeypatch, harden=False)
    _cli("review", "finding", "check", "F-01", "closed")
    write_none(project_dir / "review-2.md")
    before = _snapshot(tmp_path / "docs")

    result = runner.invoke(app, ["review", "done", "--verdict", "hardened"])

    assert result.exit_code != 0
    assert "The findings in review-2.md make it ready-to-merge, not hardened." in result.output
    assert _snapshot(tmp_path / "docs") == before


def test_a_harden_round_can_still_be_waived(tmp_path, monkeypatch):
    project_dir = _asked(tmp_path, monkeypatch)

    _cli("review", "waive", "--reason", "Out of time")

    fields = review.frontmatter(project_dir / "review-2.md")
    assert (fields["verdict"], fields["kind"], fields["reason"]) == (
        "waived", "harden", "Out of time"
    )


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


def test_a_hosted_harden_close_writes_the_same_round_as_a_local_one(
    tmp_path, monkeypatch, live_daemon
):
    steps = [
        *_harden_steps(),
        (["review", "finding", "check", "F-01", "closed"], None),
        (["review", "finding", "check", "F-02", "open"], None),
        (["review", "finding", "add", "--severity", "should-fix", "--at", "src/app.py:5",
          "--text", "The lock is taken twice"], None),
        (["review", "finding", "add", "--severity", "nit", "--text", "A name is vague"], None),
        (["review", "done"], None),
        (["doc", "show", "review-3"], None),
    ]
    local, local_dir = _local_steps(tmp_path, monkeypatch, steps)
    git = _recording_git(monkeypatch)
    hosted, hosted_dir = _hosted_steps(tmp_path, monkeypatch, live_daemon, steps)

    assert len(local) == len(hosted) == len(steps) + 1
    for mine, theirs in zip(local[1:], hosted[1:], strict=True):
        assert mine == theirs, f"{' '.join(mine[0])}:\nlocal:\n{mine[2]}\nhosted:\n{theirs[2]}"
    done = [(code, text) for args, code, text in hosted[1:] if args[:2] == ["review", "done"]]
    assert done[-1] == (0, f"{SLUG}/review-3 closed hardened"
                           " (0 blocker, 1 should-fix, 1 nit; still open: F-02)\n")
    for rounds in (local_dir, hosted_dir):
        fields = review.frontmatter(rounds / "review-3.md")
        assert (fields["verdict"], fields["kind"]) == ("hardened", "harden")
    assert (local_dir / "review-3.md").read_text() == (hosted_dir / "review-3.md").read_text()
    # The local round files no nits follow-up either.
    assert not (local_dir / followup.FOLLOWUP_FILENAME).exists()
    _assert_the_daemon_ran_no_git(git, live_daemon["root"])

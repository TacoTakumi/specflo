"""Harden rounds: a fresh review of the whole scope, outside the gate's budget.

``review start --harden`` mints the next round of the same review-N.md series
with ``kind: harden`` in its frontmatter. A harden round always reviews its
whole scope, so it has no base, and the round budget neither refuses nor
counts it. A round file with no kind is a gate round, as every round file
written before harden rounds is.

The completion gate reads the verdict of the latest gate round, never a
harden round's, and also needs each blocker and should-fix item the rounds
after it raised checked closed, deferred or rejected. With no gate round it
asks for one. ``validate execute``, the next-step hint and status agree.
"""

import json

import pytest
from typer.testing import CliRunner

from reviewhelp import fix_active_open_items, write_none
from specflo import config, followup, markdown, plan, projects, review, spec
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

    assert result.stdout == (
        "thing/review-2 closed hardened (0 blocker, 1 should-fix, 1 nit; 1 new find)\n"
    )
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
        "thing/review-2 closed hardened (0 blocker, 0 should-fix, 0 nit; 0 new finds)\n"
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


# --- the new finds of a harden round ------------------------------------------------------


def _write_findings(path, lines):
    """Write ``lines`` by hand as the Findings section of the round at ``path``."""
    path.write_text(
        markdown.replace_section_body(path.read_text(), review.FINDINGS_HEADER, "\n".join(lines))
    )


def _new_and_marked(project_dir):
    """Check F-01 closed in open harden round 2 and record, by hand, a
    should-fix marked as a regression, a new blocker and a nit."""
    _cli("review", "finding", "check", "F-01", "closed")
    _write_findings(project_dir / "review-2.md", [
        "- F-02 (should-fix, regression) [src/app.py:5] The fix broke the lock",
        "- F-03 (blocker) [src/app.py:9] The cache is never cleared",
        "- F-04 (nit) A name is vague",
    ])


def test_a_regression_or_a_nit_is_no_new_find_of_a_harden_round(tmp_path, monkeypatch):
    project_dir = _asked(tmp_path, monkeypatch)
    _new_and_marked(project_dir)

    result = _cli("review", "done")

    assert result.stdout == (
        "thing/review-2 closed hardened (1 blocker, 1 should-fix, 1 nit; 1 regression;"
        " 1 new find)\n"
    )
    closed = review.frontmatter(project_dir / "review-2.md")
    assert closed["verdict"] == "hardened"


def test_done_json_on_a_harden_round_carries_its_new_finds(tmp_path, monkeypatch):
    project_dir = _asked(tmp_path, monkeypatch)
    _new_and_marked(project_dir)

    data = json.loads(_cli("review", "done", "--json").stdout)

    assert (data["verdict"], data["regressions"], data["new_finds"]) == ("hardened", 1, 1)
    assert data["findings"] == {"blocker": 1, "should-fix": 1, "nit": 1}


def test_done_on_a_gate_round_names_no_new_finds(tmp_path, monkeypatch):
    project_dir = _asked(tmp_path, monkeypatch, harden=False)
    _new_and_marked(project_dir)

    data = json.loads(_cli("review", "done", "--json").stdout)

    assert data["verdict"] == "changes-requested"
    assert "new_finds" not in data


def test_a_waived_harden_round_names_no_new_finds(tmp_path, monkeypatch):
    project_dir = _asked(tmp_path, monkeypatch)
    _new_and_marked(project_dir)

    result = _cli("review", "done", "--verdict", "waived", "--reason", "Out of time")
    data = json.loads(_cli("status", "--json").stdout)

    assert result.stdout == "thing/review-2 closed waived\n"
    assert data["review"]["quiet_rounds"] == []


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

    assert result.stdout == (
        "thing/review-2 closed hardened (0 blocker, 1 should-fix, 1 nit; 1 new find)\n"
    )
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


# --- the completion gate after a harden round --------------------------------------------


def _planned(path, monkeypatch):
    """A full-level project at execute whose plan's one task is done, so the
    review gate is all ``validate execute`` asks, with HEAD at abc1234. The
    project's directory."""
    project_dir = _project(path, monkeypatch)
    cfg = config.load_config(path)
    spec.start_spec(path, cfg, "thing")
    spec.add_requirement(path, cfg, "thing", "Prints help", acceptance="exits 0")
    plan.start_plan(path, cfg, "thing")
    task = plan.add_task(path, cfg, "thing", "Build help", "help prints", "uv run pytest",
                         implements=["REQ-01"])
    plan.start_task(path, cfg, "thing", task.id)
    plan.done_task(path, cfg, "thing", task.id)
    assert plan.reconcile_issues(path, cfg, "thing") == []
    project_md = project_dir / "project.md"
    project_md.write_text(project_md.read_text().replace("phase: brainstorm", "phase: execute"))
    _at(monkeypatch, "abc1234")
    return project_dir


def _clean(project_dir, number, *flags, closed=()):
    """Open round ``number`` with ``flags``, check each of ``closed`` closed,
    record no finding and close it; what ``review done`` printed."""
    _cli("review", "start", *flags)
    for item in closed:
        _cli("review", "finding", "check", item, "closed")
    write_none(project_dir / f"review-{number}.md")
    return _cli("review", "done").stdout


def _validate():
    """``validate execute --json``'s exit code and issues."""
    result = runner.invoke(app, ["validate", "execute", "--json"])
    return result.exit_code, json.loads(result.stdout)["issues"]


def _state(path):
    return review.review_state(path, config.load_config(path), "thing")


def _next():
    """The Next line ``status`` shows."""
    return json.loads(_cli("status", "--json").stdout)["next_step"]


def _gate(file, verdict, passes, left_open):
    return {"file": file, "verdict": verdict, "passes": passes, "left_open": left_open}


# What the gate says while one item, the second field, is left open after
# the latest round, the first field.
_LEFT_OPEN = (
    "{1} is still open after the latest review round ({0}): fix it with a task that"
    " fixes it (`specflo task add --fixes {1}`), then run another round with"
    " `specflo review start` to check it closed, or settle it with"
    " `specflo review finding reject {1} --reason <why>` or"
    " `specflo review finding defer {1} --do <what>`."
)


def test_a_ready_gate_round_then_a_clean_harden_round_passes_the_gate(tmp_path, monkeypatch):
    project_dir = _planned(tmp_path, monkeypatch)
    assert _clean(project_dir, 1).startswith("thing/review-1 closed ready-to-merge")
    _at(monkeypatch, "b0b0b0b")

    assert _clean(project_dir, 2, "--harden").startswith("thing/review-2 closed hardened")

    assert _validate() == (0, [])
    state = _state(tmp_path)
    assert (state["verdict"], state["passing"], state["after_changes"]) == (
        "hardened", True, False
    )
    assert state["gate"] == _gate("review-1.md", "ready-to-merge", True, [])
    assert _next() == (
        "All tasks done and review-2.md is hardened - run `specflo advance` to complete"
        " the project."
    )
    date = review.frontmatter(project_dir / "review-2.md")["date"]
    assert (
        f"Reviews: 2 rounds (1 gate, 1 harden); latest round 2 hardened"
        f" (harden round, {date}, b0b0b0b); passes\n"
        in _cli("status").stdout
    )


@pytest.mark.parametrize("later", [(), ("--harden",)], ids=["gate", "harden"])
def test_a_should_fix_a_harden_round_raises_fails_the_gate_until_a_later_round_checks_it_closed(
    tmp_path, monkeypatch, later
):
    project_dir = _planned(tmp_path, monkeypatch)
    _clean(project_dir, 1)
    _at(monkeypatch, "b0b0b0b")
    _cli("review", "start", "--harden")
    _should_fix_and_nit()
    assert _cli("review", "done").stdout.startswith("thing/review-2 closed hardened")

    assert _validate() == (1, [_LEFT_OPEN.format("review-2.md", "F-01")])
    state = _state(tmp_path)
    assert state["passing"] is False
    assert state["gate"] == _gate("review-1.md", "ready-to-merge", True, ["F-01"])
    hint = _next()
    assert hint == (
        "All tasks done - review-2.md leaves F-01 open: fix it with a task that fixes it"
        " (`specflo task add --fixes F-01`), commit, then run `specflo review start` for a"
        " round that checks the fix."
    )
    assert "asks for changes" not in hint and "F-02" not in hint
    # A done fix task does not close the item: only a later round checks it closed.
    fix_active_open_items()
    assert _validate() == (1, [_LEFT_OPEN.format("review-2.md", "F-01")])
    _at(monkeypatch, "c0c0c0c")

    _clean(project_dir, 3, *later, closed=["F-01"])

    assert _validate() == (0, [])
    state = _state(tmp_path)
    assert (state["passing"], state["after_changes"]) == (True, True)
    # The fixes were made after the harden round, so the whole suite runs once more.
    hint = _next()
    assert "run the whole test suite once more" in hint and "`specflo advance`" in hint


def test_a_project_with_only_harden_rounds_fails_the_gate_asking_for_a_gate_round(
    tmp_path, monkeypatch
):
    project_dir = _planned(tmp_path, monkeypatch)
    _clean(project_dir, 1, "--harden")

    assert _validate() == (1, [
        "no gate round recorded, only harden rounds: open one with `specflo review start`,"
        " hand the reviewer `specflo review prompt`, and close it with `specflo review done`."
    ])
    state = _state(tmp_path)
    assert (state["verdict"], state["passing"]) == ("hardened", False)
    assert state["gate"] == _gate(None, "", False, [])
    assert _next() == (
        "All tasks done - no gate round is recorded, only harden rounds: run the whole test"
        " suite once, then open a gate round with `specflo review start`, hand a"
        " fresh-context reviewer the brief `specflo review prompt` prints, and close the"
        " round with `specflo review done`."
    )
    _at(monkeypatch, "c0c0c0c")

    _clean(project_dir, 2)

    assert _validate() == (0, [])
    assert _state(tmp_path)["passing"] is True


def test_after_a_spent_budget_the_gate_reads_the_latest_gate_round_not_the_harden_round(
    tmp_path, monkeypatch
):
    project_dir = _planned(tmp_path, monkeypatch)
    _round(project_dir, 1, "changes-requested", "abc1234", ["- F-01 (blocker) [src/app.py:1] One"])
    _round(project_dir, 2, "changes-requested", "def5678",
           ["- F-02 (should-fix) [src/app.py:2] Two"])
    fix_active_open_items()
    _at(monkeypatch, "b0b0b0b")

    _clean(project_dir, 3, "--harden", closed=["F-01", "F-02"])

    assert _validate() == (1, [
        "the latest gate round (review-2.md) is changes-requested and F-02 was checked closed"
        " by a later round, so its fix is proven, but only a new gate round clears the"
        " verdict. The level has used its review budget, so the next step is the user's"
        " choice: `specflo review start --over-budget` opens a gate round that checks nothing"
        " left and can pass, or `specflo review waive --reason <why>` waives the review."
    ])
    state = _state(tmp_path)
    assert (state["passing"], state["budget_spent"]) == (False, True)
    assert state["gate"] == {
        **_gate("review-2.md", "changes-requested", False, []), "checked_closed": ["F-02"]
    }
    hint = _next()
    assert hint.startswith(
        "All tasks done - review-2.md asks for changes and the level has used its review"
        " budget."
    )
    assert "review-3.md" not in hint


def test_a_waive_covers_what_it_left_open_but_not_what_a_later_harden_round_raises(
    tmp_path, monkeypatch
):
    project_dir = _planned(tmp_path, monkeypatch)
    _round(project_dir, 1, "changes-requested", "abc1234", ["- F-01 (blocker) [src/app.py:1] One"])
    _cli("review", "waive", "--reason", "Out of time")
    assert _validate() == (0, [])
    fix_active_open_items()
    _at(monkeypatch, "b0b0b0b")
    _cli("review", "start", "--harden")
    _cli("review", "finding", "check", "F-01", "open")
    _should_fix_and_nit()
    assert _cli("review", "done").stdout.startswith("thing/review-3 closed hardened")

    assert _validate() == (1, [_LEFT_OPEN.format("review-3.md", "F-02")])
    assert _state(tmp_path)["gate"] == _gate("review-2.md", "waived", True, ["F-02"])


def test_an_open_harden_round_blocks_the_gate_until_it_closes(tmp_path, monkeypatch):
    project_dir = _planned(tmp_path, monkeypatch)
    _clean(project_dir, 1)
    _cli("review", "start", "--harden")

    assert _validate() == (1, [
        "review round 2 is still open (review-2.md): close it with `specflo review done`."
    ])
    assert _state(tmp_path)["passing"] is False
    assert _next().startswith("All tasks done - finish the open review round review-2.md:")


def test_a_project_of_gate_rounds_carries_no_gate_state(tmp_path, monkeypatch):
    project_dir = _planned(tmp_path, monkeypatch)
    _clean(project_dir, 1)

    assert "gate" not in _state(tmp_path)
    assert "quiet_rounds" not in _state(tmp_path)


# --- a gate round whose items a later round checked closed ---------------------------------


def _checked_after(path, monkeypatch, checks, spent=False):
    """A full-level project at execute whose latest gate round asked for
    changes, each item with a done fix task, and whose harden round after it
    checked the items as ``checks``, F-NN to closed or open, says. Without
    ``spent``, gate round 1 asked for changes on F-01, F-02 and F-03. With it,
    the budget of 2 is spent: round 1 asked for changes on F-01 and round 2
    on F-02 and F-03. The project's directory."""
    project_dir = _planned(path, monkeypatch)
    lines = [f"- F-0{n} (should-fix) [src/app.py:{n}] Item {n}" for n in (1, 2, 3)]
    if spent:
        _round(project_dir, 1, "changes-requested", "abc1234", lines[:1])
        _round(project_dir, 2, "changes-requested", "def5678", lines[1:])
    else:
        _round(project_dir, 1, "changes-requested", "abc1234", lines)
    fix_active_open_items()
    _at(monkeypatch, "b0b0b0b")
    number = 3 if spent else 2
    _cli("review", "start", "--harden")
    for item, state in checks.items():
        _cli("review", "finding", "check", item, state)
    write_none(project_dir / f"review-{number}.md")
    assert _cli("review", "done").stdout.startswith(f"thing/review-{number} closed hardened")
    return project_dir


def _reject(item):
    return runner.invoke(app, ["review", "finding", "reject", item, "--reason", "Not a problem"])


# What the gate says once a later round checked closed an item the latest
# gate round, the first field, asks for changes on and no one settled; the
# second field names the items, the third their fixes.
_CLOSED = (
    "the latest gate round ({0}) is changes-requested and {1} checked closed by a later"
    " round, so {2} proven, but only a new gate round clears the verdict"
)
# The hint in the same place, the first field naming the gate round.
_CLOSED_HINT = (
    "All tasks done - {0} asks for changes and {1} checked closed by a later round, so {2}"
    " proven, but only a new gate round clears the verdict"
)
_CLEARS = ": `specflo review start` opens a gate round that checks nothing left and can pass."
_CLEARS_HINT = (
    ": run `specflo review start` for a gate round, which checks nothing left and can pass."
)


def test_checked_closed_items_with_the_rest_settled_need_only_a_new_gate_round(
    tmp_path, monkeypatch
):
    project_dir = _checked_after(tmp_path, monkeypatch, {
        "F-01": "closed", "F-02": "closed", "F-03": "open",
    })
    assert _reject("F-03").exit_code == 0

    code, issues = _validate()

    assert (code, issues) == (1, [
        _CLOSED.format("review-1.md", "F-01, F-02 were", "their fixes are") + _CLEARS
    ])
    # Nothing the gate offers for a checked-closed item is refused.
    for refused in ("reject F-01", "defer F-01", "reject F-NN", "--fixes"):
        assert refused not in issues[0]
    assert _reject("F-01").exit_code != 0
    assert _state(tmp_path)["gate"] == {
        **_gate("review-1.md", "changes-requested", False, []),
        "checked_closed": ["F-01", "F-02"],
    }
    hint = _next()
    assert hint == (
        _CLOSED_HINT.format("review-1.md", "F-01, F-02 were", "their fixes are") + _CLEARS_HINT
    )
    assert "--fixes" not in hint
    _at(monkeypatch, "c0c0c0c")

    _clean(project_dir, 3)

    assert _validate() == (0, [])


def test_checked_closed_items_with_none_settled_need_only_a_new_gate_round(
    tmp_path, monkeypatch
):
    _checked_after(tmp_path, monkeypatch, {"F-01": "closed", "F-02": "closed", "F-03": "closed"})

    code, issues = _validate()

    assert (code, issues) == (1, [
        _CLOSED.format("review-1.md", "F-01, F-02, F-03 were", "their fixes are") + _CLEARS
    ])
    assert _next() == (
        _CLOSED_HINT.format("review-1.md", "F-01, F-02, F-03 were", "their fixes are")
        + _CLEARS_HINT
    )


@pytest.mark.parametrize("settle", [False, True], ids=["none-settled", "one-settled"])
def test_checked_closed_and_still_open_items_of_a_gate_round_are_named_apart(
    tmp_path, monkeypatch, settle
):
    _checked_after(tmp_path, monkeypatch, {"F-01": "closed", "F-02": "open", "F-03": "open"})
    if settle:
        assert _reject("F-03").exit_code == 0

    code, issues = _validate()

    closed = _CLOSED.format("review-1.md", "F-01 was", "its fix is")
    closed_hint = _CLOSED_HINT.format("review-1.md", "F-01 was", "its fix is")
    if settle:
        assert (code, issues) == (1, [
            f"{closed}. F-02 is still open: fix it with a task that fixes it (`specflo task"
            " add --fixes F-02`), or settle it with `specflo review finding reject F-02"
            " --reason <why>` or `specflo review finding defer F-02 --do <what>`. Then"
            " `specflo review start` opens a gate round that checks what is left."
        ])
        assert _next() == (
            f"{closed_hint}. F-02 is still open: fix it with a task that fixes it (`specflo"
            " task add --fixes F-02`), work it to done and commit, then run `specflo review"
            " start` for a gate round, which checks the fix."
        )
    else:
        assert (code, issues) == (1, [
            f"{closed}. F-02, F-03 are still open: fix each with a task that fixes it"
            " (`specflo task add --fixes F-NN`), or settle each with `specflo review finding"
            " reject F-NN --reason <why>` or `specflo review finding defer F-NN --do <what>`."
            " Then `specflo review start` opens a gate round that checks what is left."
        ])
        assert _next() == (
            f"{closed_hint}. F-02, F-03 are still open: fix each with a task that fixes it"
            " (`specflo task add --fixes F-NN`), work each to done and commit, then run"
            " `specflo review start` for a gate round, which checks the fixes."
        )
    for surface in (issues[0], _next()):
        assert "F-01`" not in surface and "F-01 --" not in surface


@pytest.mark.parametrize("settle", [True, False], ids=["closed", "mixed"])
def test_checked_closed_items_after_a_spent_budget_name_over_budget_or_a_waive(
    tmp_path, monkeypatch, settle
):
    project_dir = _checked_after(
        tmp_path, monkeypatch, {"F-01": "closed", "F-02": "closed", "F-03": "open"}, spent=True
    )
    if settle:
        assert _reject("F-03").exit_code == 0
    state = _state(tmp_path)
    assert (state["budget_spent"], state["gate"]["checked_closed"]) == (True, ["F-02"])

    code, issues = _validate()

    closed = _CLOSED.format("review-2.md", "F-02 was", "its fix is")
    lead = (
        "All tasks done - review-2.md asks for changes and the level has used its review"
        " budget. F-02 was checked closed by a later round, so its fix is proven, but only a"
        " new gate round clears the verdict"
    )
    if settle:
        assert (code, issues) == (1, [
            f"{closed}. The level has used its review budget, so the next step is the user's"
            " choice: `specflo review start --over-budget` opens a gate round that checks"
            " nothing left and can pass, or `specflo review waive --reason <why>` waives the"
            " review."
        ])
        assert _next() == (
            f"{lead}. The next step is the user's choice: a gate round with `specflo review"
            " start --over-budget`, which checks nothing left and can pass, or waive the"
            " review with `specflo review waive --reason <why>`."
        )
    else:
        assert (code, issues) == (1, [
            f"{closed}. F-03 is still open: fix it with a task that fixes it (`specflo task"
            " add --fixes F-03`), or settle it with `specflo review finding reject F-03"
            " --reason <why>` or `specflo review finding defer F-03 --do <what>`. The level"
            " has used its review budget, so the next step is the user's choice: `specflo"
            " review start --over-budget` opens a gate round that checks what is left, or"
            " `specflo review waive --reason <why>` waives the review."
        ])
        assert _next() == (
            f"{lead}. F-03 is still open: fix it with a task that fixes it (`specflo task add"
            " --fixes F-03`), work it to done and commit. The next step is the user's choice:"
            " a gate round with `specflo review start --over-budget`, which checks the fix,"
            " or waive the review with `specflo review waive --reason <why>`."
        )
        return
    assert _start().exit_code != 0                     # a gate round needs --over-budget
    _at(monkeypatch, "c0c0c0c")

    _clean(project_dir, 4, "--over-budget")

    assert _validate() == (0, [])


# --- a stop after two quiet harden rounds -------------------------------------------------

# The hint once the latest two harden rounds, the first two fields, raised no
# new blocker or should-fix finding; the third field is how the whole suite
# is named, and the fourth what completion then needs.
_STOP = (
    "All tasks done - the last two harden rounds, {0} and {1}, raised no new blocker or"
    " should-fix finding, so hardening may stop here; that is the user's call. To stop,"
    " {2} once, then {3}. To go on, open another harden round with"
    " `specflo review start --harden`."
)
_ADVANCE = "run `specflo advance` to complete the project"
_GATE_NEEDS = (
    "go on to completion, which needs a gate round whose verdict passes and no open item"
    " (`specflo validate execute` names what is left)"
)
_SUITE = "run the whole test suite"


def test_two_quiet_harden_rounds_in_a_row_hint_a_stop_naming_the_whole_suite(
    tmp_path, monkeypatch
):
    project_dir = _planned(tmp_path, monkeypatch)
    _clean(project_dir, 1)
    assert _clean(project_dir, 2, "--harden").endswith("; 0 new finds)\n")
    # One quiet round is no run: the hint reads as it did.
    assert _next() == (
        "All tasks done and review-2.md is hardened - run `specflo advance` to complete"
        " the project."
    )

    assert _clean(project_dir, 3, "--harden") == (
        "thing/review-3 closed hardened (0 blocker, 0 should-fix, 0 nit; 0 new finds)\n"
    )

    assert _state(tmp_path)["quiet_rounds"] == ["review-2.md", "review-3.md"]
    assert _next() == _STOP.format("review-2.md", "review-3.md", _SUITE, _ADVANCE)
    _cli("config", "set", "test_command", "uv run pytest -q")
    assert _next() == _STOP.format(
        "review-2.md", "review-3.md", f"{_SUITE} (`uv run pytest -q`)", _ADVANCE
    )
    # The hint only suggests: the gate reads as it did.
    assert _validate() == (0, [])


def test_harden_rounds_whose_only_findings_are_nits_or_regressions_hint_a_stop(
    tmp_path, monkeypatch
):
    project_dir = _planned(tmp_path, monkeypatch)
    _clean(project_dir, 1)
    _cli("review", "start", "--harden")
    _write_findings(project_dir / "review-2.md", [
        "- F-01 (should-fix, regression) [src/app.py:5] The fix broke the lock",
        "- F-02 (nit) A name is vague",
    ])
    assert _cli("review", "done").stdout.endswith("; 1 regression; 0 new finds)\n")
    fix_active_open_items()
    _cli("review", "start", "--harden")
    _cli("review", "finding", "check", "F-01", "closed")
    _write_findings(project_dir / "review-3.md", [
        "- F-03 (blocker, regression) [src/app.py:7] The fix broke the cache",
        "- F-04 (nit) A comment is stale",
    ])

    assert _cli("review", "done").stdout == (
        "thing/review-3 closed hardened (1 blocker, 0 should-fix, 1 nit; 1 regression;"
        " 0 new finds)\n"
    )

    # F-03 is still an item to fix: the stop names what completion needs.
    assert _validate() == (1, [_LEFT_OPEN.format("review-3.md", "F-03")])
    assert _next() == _STOP.format("review-2.md", "review-3.md", _SUITE, _GATE_NEEDS)


def test_two_quiet_harden_rounds_with_a_new_find_between_hint_no_stop(tmp_path, monkeypatch):
    project_dir = _planned(tmp_path, monkeypatch)
    _clean(project_dir, 1)
    _clean(project_dir, 2, "--harden")
    _cli("review", "start", "--harden")
    _should_fix_and_nit()
    assert _cli("review", "done").stdout.endswith("; 1 new find)\n")
    fix_active_open_items()

    _clean(project_dir, 4, "--harden", closed=["F-01"])

    assert _state(tmp_path)["quiet_rounds"] == ["review-4.md"]
    hint = _next()
    assert hint == (
        "All tasks done and review-4.md is hardened after a round that asked for changes"
        " - run the whole test suite once more, then `specflo advance` to complete the"
        " project."
    )


def test_a_gate_round_between_two_quiet_harden_rounds_breaks_the_run(tmp_path, monkeypatch):
    project_dir = _planned(tmp_path, monkeypatch)
    _clean(project_dir, 1)
    _clean(project_dir, 2, "--harden")
    _clean(project_dir, 3)

    _clean(project_dir, 4, "--harden")

    assert _state(tmp_path)["quiet_rounds"] == ["review-4.md"]
    assert _next() == (
        "All tasks done and review-4.md is hardened - run `specflo advance` to complete"
        " the project."
    )


def test_a_waived_harden_round_breaks_the_run(tmp_path, monkeypatch):
    project_dir = _planned(tmp_path, monkeypatch)
    _clean(project_dir, 1)
    _clean(project_dir, 2, "--harden")
    _cli("review", "start", "--harden")
    _cli("review", "waive", "--reason", "Out of time")

    _clean(project_dir, 4, "--harden")

    assert _state(tmp_path)["quiet_rounds"] == ["review-4.md"]
    assert "--harden" not in _next()


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
                           " (0 blocker, 1 should-fix, 1 nit; 1 new find; still open: F-02)\n")
    for rounds in (local_dir, hosted_dir):
        fields = review.frontmatter(rounds / "review-3.md")
        assert (fields["verdict"], fields["kind"]) == ("hardened", "harden")
    assert (local_dir / "review-3.md").read_text() == (hosted_dir / "review-3.md").read_text()
    # The local round files no nits follow-up either.
    assert not (local_dir / followup.FOLLOWUP_FILENAME).exists()
    _assert_the_daemon_ran_no_git(git, live_daemon["root"])


def test_a_hosted_gate_after_a_harden_round_reads_as_a_local_one(
    tmp_path, monkeypatch, live_daemon
):
    """A gate round closes ready, a harden round raises should-fix F-03, and
    a later harden round checks it closed: the gate fails naming F-03, then
    passes, locally and hosted alike."""
    gate = [
        (["validate", "execute", "--json"], None),
        (["status"], None),
    ]
    steps = [
        *_round_one(),
        *_fix("Name the right command", "F-01", "T-02"),
        (["review", "start"], None),
        (["review", "finding", "check", "F-01", "closed"], None),
        (["review", "finding", "add", "--severity", "nit", "--text", "A name is vague"], None),
        (["review", "done"], None),
        (["review", "start", "--harden"], None),
        (["review", "finding", "add", "--severity", "should-fix", "--at", "src/app.py:5",
          "--text", "The lock is dropped early"], None),
        (["review", "done"], None),
        *gate,
        *_fix("Keep the lock", "F-03", "T-03"),
        (["review", "start", "--harden"], None),
        (["review", "finding", "check", "F-03", "closed"], None),
        (["review", "finding", "add", "--severity", "nit", "--text", "A comment is stale"], None),
        (["review", "done"], None),
        *gate,
    ]
    local, _ = _local_steps(tmp_path, monkeypatch, steps)
    git = _recording_git(monkeypatch)
    hosted, _ = _hosted_steps(tmp_path, monkeypatch, live_daemon, steps)

    assert len(local) == len(hosted) == len(steps) + 1
    for mine, theirs in zip(local[1:], hosted[1:], strict=True):
        assert mine == theirs, f"{' '.join(mine[0])}:\nlocal:\n{mine[2]}\nhosted:\n{theirs[2]}"
    checks = [
        (code, json.loads(text)["issues"]) for args, code, text in hosted[1:]
        if args[:2] == ["validate", "execute"]
    ]
    assert checks == [(1, [_LEFT_OPEN.format("review-3.md", "F-03")]), (0, [])]
    shown = [text for args, _, text in hosted[1:] if args == ["status"]]
    assert "latest round 3 hardened" in shown[0] and "passes" not in shown[0]
    assert "review-3.md leaves F-03 open" in shown[0]
    assert "latest round 4 hardened" in shown[1] and "; passes" in shown[1]
    assert "`specflo advance`" in shown[1]
    _assert_the_daemon_ran_no_git(git, live_daemon["root"])


def test_a_hosted_stop_after_two_quiet_harden_rounds_reads_as_a_local_one(
    tmp_path, monkeypatch, live_daemon
):
    """A gate round closes ready, then two harden rounds record only a nit
    each: the second close counts no new find, and status suggests a stop
    naming this checkout's test command, locally and hosted alike."""
    steps = [
        *_round_one(),
        *_fix("Name the right command", "F-01", "T-02"),
        (["review", "start"], None),
        (["review", "finding", "check", "F-01", "closed"], None),
        (["review", "finding", "add", "--severity", "nit", "--text", "A name is vague"], None),
        (["review", "done"], None),
        (["review", "start", "--harden"], None),
        (["review", "finding", "add", "--severity", "nit", "--text", "A comment is stale"], None),
        (["review", "done"], None),
        (["status"], None),
        (["review", "start", "--harden"], None),
        (["review", "finding", "add", "--severity", "nit", "--text", "A test name is long"], None),
        (["review", "done"], None),
        (["config", "set", "test_command", "uv run pytest -q"], None),
        (["status"], None),
    ]
    local, _ = _local_steps(tmp_path, monkeypatch, steps)
    git = _recording_git(monkeypatch)
    hosted, _ = _hosted_steps(tmp_path, monkeypatch, live_daemon, steps)

    assert len(local) == len(hosted) == len(steps) + 1
    for mine, theirs in zip(local[1:], hosted[1:], strict=True):
        assert mine == theirs, f"{' '.join(mine[0])}:\nlocal:\n{mine[2]}\nhosted:\n{theirs[2]}"
    done = [text for args, _, text in hosted[1:] if args[:2] == ["review", "done"]]
    assert done[-1] == (
        f"{SLUG}/review-4 closed hardened (0 blocker, 0 should-fix, 1 nit; 0 new finds)\n"
    )
    shown = [text for args, _, text in hosted[1:] if args == ["status"]]
    assert "--harden" not in shown[0]
    stop = " ".join(shown[1].split())
    assert "the last two harden rounds, review-3.md and review-4.md," in stop
    assert "run the whole test suite (`uv run pytest -q`) once" in stop
    _assert_the_daemon_ran_no_git(git, live_daemon["root"])


# --- the start line names the scope the harden round reviews ------------------


def test_a_harden_start_in_a_harden_project_names_the_briefs_scope(tmp_path, monkeypatch):
    from test_level_harden import _checkout, _fill

    _checkout(tmp_path / "local", monkeypatch)
    runner.invoke(app, ["new", "Thing", "--level", "harden"])
    _fill()

    result = runner.invoke(app, ["review", "start", "--harden"])

    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[1:] == ["Kind: harden", "Scope: the brief's Scope"]

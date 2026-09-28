"""The ladder run: quick, then fast, then full, each on its own stacked branch."""

import json
import subprocess

import pytest
import reviewhelp
from typer.testing import CliRunner

from specflo import auto, config, projects, review
from specflo.cli import app

runner = CliRunner()


def _ok(args, stdin=None):
    result = runner.invoke(app, args, input=stdin)
    assert result.exit_code == 0, (args, result.output)
    return result


def _pass_review():
    """Close the open round ready-to-merge, with '- none' under its Findings."""
    result = reviewhelp.review_done(runner, app, "ready-to-merge")
    assert result.exit_code == 0, result.output
    return result


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A git repository with one commit and specflo initialised, as the cwd."""
    monkeypatch.chdir(tmp_path)
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "Tester")
    (tmp_path / "app.txt").write_text("hello\n")
    _ok(["init"])
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def _branches(repo):
    return set(git(repo, "branch", "--format=%(refname:short)").splitlines())


def _state(repo):
    return auto.load_run_state(repo, config.load_config(repo), "thing")


# --- starting a ladder ---------------------------------------------------------------


def test_ladder_start_cuts_the_quick_branch_and_records_the_base(repo):
    _ok(["new", "Thing", "--level", "quick"])
    base = git(repo, "rev-parse", "HEAD")

    result = runner.invoke(app, ["auto", "--ladder", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["stop"] is False
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "specflo/thing/quick"
    assert git(repo, "rev-parse", "specflo/thing/quick") == base
    ladder_md = (repo / "docs" / "projects" / "thing" / "ladder.md").read_text()
    assert "`main`" in ladder_md and base in ladder_md
    assert _state(repo)["ladder"]["base_commit"] == base


def test_a_later_pass_without_the_flag_continues_the_ladder(repo):
    _ok(["new", "Thing", "--level", "quick"])
    _ok(["auto", "--ladder", "--json"])

    result = json.loads(_ok(["auto", "--json"]).output)

    assert result["stop"] is False
    assert auto.LADDER_MARKER in result["payload"]


def test_ladder_start_refuses_a_fast_project(repo):
    _ok(["new", "Thing", "--level", "fast"])
    before = _branches(repo)

    result = runner.invoke(app, ["auto", "--ladder"])

    assert result.exit_code != 0
    assert "quick" in result.output
    assert _branches(repo) == before


def test_ladder_start_refuses_a_dirty_tree(repo):
    _ok(["new", "Thing", "--level", "quick"])
    (repo / "app.txt").write_text("changed\n")
    before = _branches(repo)

    result = runner.invoke(app, ["auto", "--ladder"])

    assert result.exit_code != 0
    assert "app.txt" in result.output
    assert _branches(repo) == before


def test_ladder_start_refuses_an_existing_ladder_branch(repo):
    _ok(["new", "Thing", "--level", "quick"])
    git(repo, "branch", "specflo/thing/quick")
    before = {b: git(repo, "rev-parse", b) for b in _branches(repo)}

    result = runner.invoke(app, ["auto", "--ladder"])

    assert result.exit_code != 0
    assert "specflo/thing/quick" in result.output
    assert {b: git(repo, "rev-parse", b) for b in _branches(repo)} == before


def test_ladder_start_changes_no_ref_outside_its_own(repo):
    git(repo, "branch", "keep")
    _ok(["new", "Thing", "--level", "quick"])
    before = {b: git(repo, "rev-parse", b) for b in _branches(repo)}

    _ok(["auto", "--ladder", "--json"])

    after = {b: git(repo, "rev-parse", b) for b in _branches(repo)}
    assert {b: after[b] for b in before} == before
    assert set(after) - set(before) == {"specflo/thing/quick"}


# --- climbing: a completed level moves the ladder up ---------------------------------


def _work(repo, name, text="work\n"):
    (repo / name).write_text(text)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", f"work on {name}")


def _finish_quick(repo):
    _ok(["section", "set", "brief", "Goal", "--stdin"], "Fix the greeting.\n")
    _ok(["section", "set", "brief", "Done when", "--stdin"], "- app.txt says hi\n")
    _work(repo, "app.txt", "hi\n")
    _ok(["section", "set", "brief", "Proof", "--stdin"], "cat app.txt -> hi\n")
    _ok(["advance"])


def _ladder_at_quick(repo):
    _ok(["new", "Thing", "--level", "quick"])
    _ok(["auto", "--ladder", "--json"])


def test_a_completed_quick_level_moves_the_ladder_to_fast(repo):
    _ladder_at_quick(repo)
    _finish_quick(repo)
    quick_end = git(repo, "rev-parse", "HEAD")

    result = json.loads(_ok(["auto", "--json"]).output)

    assert result["stop"] is False
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "specflo/thing/fast"
    assert git(repo, "rev-parse", "specflo/thing/quick") == quick_end
    project = projects.load_project(repo, config.load_config(repo), "thing")
    assert (project.level, project.phase, project.status) == ("fast", "brainstorm", "active")
    ladder_md = (repo / "docs" / "projects" / "thing" / "ladder.md").read_text()
    assert "| quick |" in ladder_md


def _finish_fast(repo):
    """Take a seeded fast project through its phases, work and a review."""
    _ok(["decision", "add", "--text", "keep it small", "--rationale", "weighed a, b"])
    _ok(["section", "set", "brainstorm", "Out of scope / Deferred", "--stdin"], "none\n")
    _ok(["advance"])
    _ok(["section", "set", "spec", "In scope", "--stdin"], "- the greeting.\n")
    _ok(["section", "set", "spec", "Out of scope", "--stdin"], "- the rest.\n")
    _ok(["advance"])
    _ok(["advance"])
    _work(repo, "fast.txt")
    _ok(["review", "start"])
    _pass_review()
    _ok(["advance"])


def test_a_completed_fast_level_moves_the_ladder_to_full(repo):
    _ladder_at_quick(repo)
    _finish_quick(repo)
    _ok(["auto", "--json"])
    _finish_fast(repo)
    fast_end = git(repo, "rev-parse", "HEAD")

    result = json.loads(_ok(["auto", "--json"]).output)

    assert result["stop"] is False
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "specflo/thing/full"
    assert git(repo, "rev-parse", "specflo/thing/fast") == fast_end
    project = projects.load_project(repo, config.load_config(repo), "thing")
    assert (project.level, project.phase) == ("full", "brainstorm")
    assert "| fast |" in (repo / "docs" / "projects" / "thing" / "ladder.md").read_text()


# --- caps inside a ladder: cut down, do not stop ---------------------------------------


def test_a_quick_ladder_level_over_its_cap_is_told_to_cut_down(repo):
    _ladder_at_quick(repo)
    _ok(["section", "set", "brief", "Done when", "--stdin"], "- one\n- two\n")

    result = json.loads(_ok(["auto", "--json"]).output)

    assert result["stop"] is False
    assert "one check" in result["payload"] and "Deferred" in result["payload"]


def test_a_fast_ladder_level_over_its_cap_is_told_to_cut_down(repo):
    _ladder_at_quick(repo)
    _finish_quick(repo)
    _ok(["auto", "--json"])
    for n in range(7):
        _ok(["task", "add", "--text", f"more {n}", "--acceptance", "a", "--verify", "true",
             "--from", "REQ-01"])

    result = json.loads(_ok(["auto", "--json"]).output)

    assert result["stop"] is False
    assert "7" in result["payload"] and "Out of scope / Deferred" in result["payload"]
    assert "only warns" in result["payload"]
    # No verb lowers the task count, so the cap cannot block the level.
    assert "at most 7" not in runner.invoke(app, ["validate", "plan"]).output


# --- the ladder.md rows ---------------------------------------------------------------


def _row(repo, level):
    text = (repo / "docs" / "projects" / "thing" / "ladder.md").read_text()
    line = next(l for l in text.splitlines() if l.startswith(f"| {level} |"))
    cells = [c.strip() for c in line.strip("|").split("|")]
    keys = ["level", "branch", "commits", "files", "added", "removed", "tasks",
            "tests", "review", "deferred", "time"]
    return dict(zip(keys, cells))


def _quick_level_with_history(repo):
    _ladder_at_quick(repo)
    base = _state(repo)["ladder"]["base_commit"]
    _ok(["section", "set", "brief", "Goal", "--stdin"], "Grow the file.\n")
    _ok(["section", "set", "brief", "Done when", "--stdin"], "- the file has ten lines\n")
    _ok(["section", "set", "brief", "Deferred", "--stdin"], "- one more\n- and another\n")
    _work(repo, "app.txt", "".join(f"line {n}\n" for n in range(5)))
    _work(repo, "app.txt", "".join(f"line {n}\n" for n in range(10)))
    _ok(["section", "set", "brief", "Proof", "--stdin"], "wc -l app.txt -> 10\n")
    _ok(["advance"])
    _ok(["auto", "--json"])
    return base


def test_the_quick_row_matches_git_and_the_brief(repo):
    base = _quick_level_with_history(repo)
    row = _row(repo, "quick")

    assert row["commits"] == git(repo, "rev-list", "--count", f"{base}..specflo/thing/quick") == "2"
    numstat = git(repo, "diff", "--numstat", base, "specflo/thing/quick").splitlines()
    assert row["files"] == str(len(numstat)) == "1"
    plus, minus, _ = numstat[0].split("\t")
    assert (row["added"], row["removed"]) == (plus, minus) == ("10", "1")
    assert (row["tasks"], row["review"], row["tests"]) == ("n/a", "none", "not run")
    assert row["deferred"] == "2"
    assert row["time"].isdigit()


@pytest.mark.parametrize("command, expected", [("true", "pass"), ("false", "fail")])
def test_the_row_records_the_test_command_result(repo, command, expected):
    _ok(["config", "set", "test_command", command])
    _quick_level_with_history(repo)

    assert _row(repo, "quick")["tests"] == expected
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "specflo/thing/fast"


# --- the end of the ladder, and the guardrails across it --------------------------------


def _ladder_at_full(repo):
    _ladder_at_quick(repo)
    _finish_quick(repo)
    _ok(["auto", "--json"])
    _finish_fast(repo)
    _ok(["auto", "--json"])


def test_the_ladder_ends_when_full_completes(repo):
    _ladder_at_full(repo)
    _ok(["advance"])
    _ok(["advance"])
    _ok(["advance"])
    _work(repo, "full.txt")
    _pass_review()
    _ok(["advance"])

    result = json.loads(_ok(["auto", "--json"]).output)

    assert result["stop"] is True
    assert result["reason"] == auto.STOP_PROJECT_COMPLETE
    for level in ("quick", "fast", "full"):
        assert f"specflo/thing/{level}" in result["payload"]
    assert "ladder.md" in result["payload"]
    assert "| full |" in (repo / "docs" / "projects" / "thing" / "ladder.md").read_text()


def test_guard_auto_off_stops_the_ladder_and_cuts_no_branch(repo):
    _ladder_at_quick(repo)
    _finish_quick(repo)
    _ok(["auto", "--off"])
    before = _branches(repo)

    result = json.loads(_ok(["auto", "--json"]).output)

    assert (result["stop"], result["reason"]) == (True, auto.STOP_KILL_SWITCH)
    assert _branches(repo) == before


def test_guard_the_pass_cap_counts_the_whole_ladder(repo):
    _ladder_at_quick(repo)
    _finish_quick(repo)

    result = json.loads(_ok(["auto", "--json", "--max-passes", "2"]).output)

    assert (result["stop"], result["reason"]) == (True, auto.STOP_PASS_CAP)


# --- a completed ladder level is a live run, not a finished project ------------------------


def test_a_completed_ladder_level_reads_as_a_run_under_way_that_continues(repo):
    _ladder_at_quick(repo)
    _ok(["section", "set", "brief", "Goal", "--stdin"], "Fix the greeting.\n")
    _ok(["section", "set", "brief", "Done when", "--stdin"], "- app.txt says hi\n")
    _work(repo, "app.txt", "hi\n")
    _ok(["section", "set", "brief", "Proof", "--stdin"], "cat app.txt -> hi\n")

    advanced = _ok(["advance"]).output

    status = json.loads(_ok(["status", "--json"]).output)
    assert status["auto_run"]["under_way"] is True
    checkpoint_md = (repo / "docs" / "projects" / "thing" / "checkpoint.md").read_text()
    for text in (advanced, status["next_step"], checkpoint_md):
        assert "specflo auto" in text
        assert "Start the next piece of work" not in text


# --- a climb that cannot happen stops with a reason, once ---------------------------------


def test_an_existing_next_branch_blocks_the_climb_with_a_reason_and_one_row(repo):
    _ladder_at_quick(repo)
    git(repo, "branch", "specflo/thing/fast")
    _finish_quick(repo)

    first = json.loads(_ok(["auto", "--json"]).output)
    second = json.loads(_ok(["auto", "--json"]).output)

    assert (first["stop"], first["reason"]) == (True, auto.STOP_LADDER_BLOCKED)
    assert "specflo/thing/fast" in first["payload"]
    assert second["reason"] == auto.STOP_LADDER_BLOCKED
    ladder_md = (repo / "docs" / "projects" / "thing" / "ladder.md").read_text()
    assert ladder_md.count("| quick |") == 0
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "specflo/thing/quick"


def test_a_level_changed_by_hand_blocks_the_climb_with_a_reason(repo):
    _ladder_at_quick(repo)
    _ok(["section", "set", "brief", "Goal", "--stdin"], "Fix the greeting.\n")
    _ok(["section", "set", "brief", "Done when", "--stdin"], "- app.txt says hi\n")
    _ok(["section", "set", "brief", "Proof", "--stdin"], "cat app.txt -> hi\n")
    _ok(["auto", "--off"])
    _ok(["level", "fast"])
    _ok(["auto", "--on"])
    _finish_fast(repo)

    result = json.loads(_ok(["auto", "--json"]).output)

    assert (result["stop"], result["reason"]) == (True, auto.STOP_LADDER_BLOCKED)
    assert "changed outside the ladder" in result["payload"]


def test_a_second_ladder_start_says_a_ladder_is_running(repo):
    _ladder_at_quick(repo)

    result = runner.invoke(app, ["auto", "--ladder"])

    assert result.exit_code != 0
    assert "already has a ladder" in result.output


def test_a_ladder_starts_with_the_projects_dir_outside_the_repo(repo, tmp_path_factory):
    outside = tmp_path_factory.mktemp("elsewhere") / "projects"
    _ok(["config", "set", "projects_dir", str(outside), "--force"])
    _ok(["new", "Thing", "--level", "quick"])

    result = runner.invoke(app, ["auto", "--ladder", "--json"])

    assert result.exit_code == 0, result.output
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "specflo/thing/quick"


# --- what an agent reads at a level boundary -------------------------------------------


def test_the_end_of_a_ladder_level_does_not_read_as_the_end_of_the_run(repo):
    from specflo import hook
    _ladder_at_quick(repo)
    payload = json.loads(_ok(["auto", "--json"]).output)["payload"]
    assert "ends only that level" in payload
    _ok(["section", "set", "brief", "Goal", "--stdin"], "Fix the greeting.\n")
    _ok(["section", "set", "brief", "Done when", "--stdin"], "- app.txt says hi\n")
    _work(repo, "app.txt", "hi\n")
    _ok(["section", "set", "brief", "Proof", "--stdin"], "cat app.txt -> hi\n")

    advanced = _ok(["advance"]).output

    assert "this project is complete" not in advanced
    assert "specflo auto" in advanced
    assert "specflo auto" in hook.reseed_text(repo)


def test_a_fast_ladder_level_is_told_to_advance_past_the_approval(repo):
    _ladder_at_quick(repo)
    _finish_quick(repo)
    _ok(["auto", "--json"])
    _ok(["decision", "add", "--text", "keep it small", "--rationale", "weighed a, b"])
    _ok(["section", "set", "brainstorm", "Out of scope / Deferred", "--stdin"], "none\n")
    _ok(["advance"])
    _ok(["section", "set", "spec", "In scope", "--stdin"], "- the greeting.\n")
    _ok(["section", "set", "spec", "Out of scope", "--stdin"], "- the rest.\n")
    _ok(["advance"])

    payload = json.loads(_ok(["auto", "--json"]).output)["payload"]

    assert "one approval before execute too: advance when the plan validates" in payload


def test_a_blocked_ladder_is_not_a_run_under_way(repo):
    _ladder_at_quick(repo)
    git(repo, "branch", "specflo/thing/fast")
    _finish_quick(repo)
    _ok(["auto", "--json"])

    assert json.loads(_ok(["status", "--json"]).output)["auto_run"]["under_way"] is False


def test_a_full_level_reached_by_hand_blocks_the_ladder_end_with_a_reason(repo):
    _ladder_at_quick(repo)
    _ok(["section", "set", "brief", "Goal", "--stdin"], "Fix the greeting.\n")
    _ok(["section", "set", "brief", "Done when", "--stdin"], "- app.txt says hi\n")
    _ok(["section", "set", "brief", "Proof", "--stdin"], "cat app.txt -> hi\n")
    _ok(["auto", "--off"])
    _ok(["level", "full"])
    _ok(["auto", "--on"])
    _finish_fast(repo)

    result = json.loads(_ok(["auto", "--json"]).output)

    assert (result["stop"], result["reason"]) == (True, auto.STOP_LADDER_BLOCKED)
    assert "changed outside the ladder" in result["payload"]
    assert json.loads(_ok(["status", "--json"]).output)["auto_run"]["under_way"] is False


def test_guard_auto_off_at_fast_level_stops_the_ladder(repo):
    _ladder_at_quick(repo)
    _finish_quick(repo)
    _ok(["auto", "--json"])
    _ok(["auto", "--off"])
    before = _branches(repo)

    result = json.loads(_ok(["auto", "--json"]).output)

    assert (result["stop"], result["reason"]) == (True, auto.STOP_KILL_SWITCH)
    assert _branches(repo) == before



# --- the full level in a ladder, as an agent reads it --------------------------------------


def _full_level_done(repo):
    _ladder_at_full(repo)
    _ok(["advance"])
    _ok(["advance"])
    _ok(["advance"])
    _work(repo, "full.txt")
    _pass_review()
    return _ok(["advance"]).output


def test_full_level_in_a_ladder_has_its_own_review_and_work(repo):
    _ladder_at_full(repo)

    payload = json.loads(_ok(["auto", "--json"]).output)["payload"]

    assert "no user to interview" in payload and "D-01" in payload
    assert "Out of scope / Deferred" in payload
    assert "take it back with `specflo review start`" in payload
    assert (repo / "docs" / "projects" / "thing" / "review-2.md").is_file()
    _ok(["advance"])
    _ok(["advance"])
    _ok(["advance"])
    blocked = runner.invoke(app, ["advance"])
    assert blocked.exit_code != 0 and "still open" in blocked.output


def test_the_end_of_the_full_level_points_to_the_closing_pass(repo):
    from specflo import hook
    advanced = _full_level_done(repo)

    assert "this project is complete" not in advanced
    assert "close the ladder" in advanced
    status = json.loads(_ok(["status", "--json"]).output)
    assert status["auto_run"]["under_way"] is True
    assert "close the ladder" in status["next_step"]
    assert "close the ladder" in hook.reseed_text(repo)
    assert "| full |" not in (repo / "docs" / "projects" / "thing" / "ladder.md").read_text()

    result = json.loads(_ok(["auto", "--json"]).output)

    assert result["reason"] == auto.STOP_PROJECT_COMPLETE
    assert "| full |" in (repo / "docs" / "projects" / "thing" / "ladder.md").read_text()
    assert json.loads(_ok(["status", "--json"]).output)["auto_run"]["under_way"] is False


def test_a_fast_auto_payload_no_longer_says_to_wait_for_approval(repo):
    _ladder_at_quick(repo)
    _finish_quick(repo)
    _ok(["auto", "--json"])
    _ok(["decision", "add", "--text", "keep it small", "--rationale", "weighed a, b"])
    _ok(["section", "set", "brainstorm", "Out of scope / Deferred", "--stdin"], "none\n")
    _ok(["advance"])
    _ok(["section", "set", "spec", "In scope", "--stdin"], "- the greeting.\n")
    _ok(["section", "set", "spec", "Out of scope", "--stdin"], "- the rest.\n")
    _ok(["advance"])

    payload = json.loads(_ok(["auto", "--json"]).output)["payload"]

    assert "only after they approve" not in payload
    assert "covers fast level's one approval" in payload


def test_a_level_row_times_to_the_advance_and_counts_commits_to_the_branch_tip(repo):
    _ladder_at_quick(repo)
    _finish_quick(repo)
    ended = _state(repo)["ladder"]["levels"]["quick"]["ended"]
    _work(repo, "late.txt")

    _ok(["auto", "--json"])

    quick = _state(repo)["ladder"]["levels"]["quick"]
    assert quick["ended"] == ended
    assert quick["end_commit"] == git(repo, "rev-parse", "specflo/thing/quick")
    commits = git(repo, "rev-list", "--count",
                  f"{quick['start_commit']}..specflo/thing/quick")
    assert _row(repo, "quick")["commits"] == commits


def test_after_a_guardrail_stop_at_a_climb_the_texts_still_point_to_auto(repo):
    _ladder_at_quick(repo)
    _finish_quick(repo)
    _ok(["auto", "--off"])
    _ok(["auto", "--json"])

    status = json.loads(_ok(["status", "--json"]).output)

    assert "specflo auto" in status["next_step"]
    assert status["auto_run"]["under_way"] is False


def test_full_level_texts_name_the_full_work_at_brainstorm_spec_and_plan(repo):
    _ladder_at_full(repo)
    checkpoint_md = repo / "docs" / "projects" / "thing" / "checkpoint.md"

    for phase in ("brainstorm", "spec", "plan"):
        status = json.loads(_ok(["status", "--json"]).output)
        assert status["phase"] == phase
        assert status["next_step"].startswith("Ladder at full level"), phase
        assert "Ladder at full level" in checkpoint_md.read_text(), phase
        _ok(["advance"])


def test_the_climb_refreshes_the_checkpoint(repo):
    _ladder_at_quick(repo)
    _finish_quick(repo)

    _ok(["auto", "--json"])

    checkpoint_md = (repo / "docs" / "projects" / "thing" / "checkpoint.md").read_text()
    assert "_phase: brainstorm" in checkpoint_md
    assert "This ladder level is complete" not in checkpoint_md


def test_guide_at_a_ladder_pause_points_to_auto(repo):
    _ladder_at_quick(repo)
    _finish_quick(repo)

    out = _ok(["guide", "--json"]).output

    assert "specflo auto" in json.loads(out)["next_step"]


# --- review round 5 -----------------------------------------------------------------


def _fast_ladder_over_the_task_cap(repo):
    _ladder_at_quick(repo)
    _finish_quick(repo)
    _ok(["auto", "--json"])
    for n in range(7):
        _ok(["task", "add", "--text", f"more {n}", "--acceptance", "a", "--verify", "true",
             "--from", "REQ-01"])


def test_inside_a_ladder_the_outgrew_text_never_says_to_move_up(repo):
    _fast_ladder_over_the_task_cap(repo)

    checkpoint = _ok(["checkpoint"]).output
    payload = json.loads(_ok(["auto", "--json"]).output)["payload"]

    assert "Over fast level's cap" in checkpoint and "only warns" in checkpoint
    assert "specflo level" not in checkpoint and "specflo level" not in payload


def test_specflo_level_is_refused_while_a_ladder_is_live(repo):
    _ladder_at_quick(repo)

    refused = runner.invoke(app, ["level", "fast"])

    assert refused.exit_code != 0 and "ladder run is live" in refused.output
    assert projects.load_project(repo, config.load_config(repo), "thing").level == "quick"


def test_no_ladder_starts_while_the_kill_switch_is_set(repo):
    _ok(["new", "Thing", "--level", "quick"])
    _ok(["auto", "--off"])

    refused = runner.invoke(app, ["auto", "--ladder", "--json"])

    assert refused.exit_code != 0 and "kill switch" in refused.output
    assert git(repo, "branch", "--list", "specflo/thing/quick") == ""
    assert not (repo / "docs" / "projects" / "thing" / "ladder.md").exists()


def test_guide_asks_an_attended_fast_project_to_stop_for_approval(repo):
    _ok(["new", "Thing", "--level", "quick"])
    _finish_quick(repo)
    _ok(["level", "fast"])
    _ok(["decision", "add", "--text", "keep it small", "--rationale", "weighed a, b"])
    _ok(["section", "set", "brainstorm", "Out of scope / Deferred", "--stdin"], "none\n")
    _ok(["advance"])
    _ok(["section", "set", "spec", "In scope", "--stdin"], "- the greeting.\n")
    _ok(["section", "set", "spec", "Out of scope", "--stdin"], "- the rest.\n")
    _ok(["advance"])

    guide_next = json.loads(_ok(["guide", "--json"]).output)["next_step"]

    assert "only after they approve" in guide_next


def test_a_deferred_list_that_says_none_counts_no_items():
    from specflo import ladder

    doc = "## Deferred\n- none\n\n## Next\n"
    assert ladder._list_count(doc, "## Deferred") == 0
    assert ladder._list_count("## Deferred\n- later: colours\n- None.\n", "## Deferred") == 1


# --- the review budget inside a ladder: waive and climb -----------------------------------


def _round_asking_for_changes(*checks, severity="blocker", text="Still wrong"):
    """Open a round, check each earlier item as given, add one finding, close it."""
    _ok(["review", "start", "--over-budget"])
    for item, state in checks:
        _ok(["review", "finding", "check", item, state])
    _ok(["review", "finding", "add", "--severity", severity, "--text", text])
    _ok(["review", "done"])


def _fast_level_at_its_budget(repo):
    """A ladder at fast level whose two review rounds both asked for changes."""
    _ladder_at_quick(repo)
    _finish_quick(repo)
    _ok(["auto", "--json"])
    _ok(["decision", "add", "--text", "keep it small", "--rationale", "weighed a, b"])
    _ok(["section", "set", "brainstorm", "Out of scope / Deferred", "--stdin"], "none\n")
    _ok(["advance"])
    _ok(["section", "set", "spec", "In scope", "--stdin"], "- the greeting.\n")
    _ok(["section", "set", "spec", "Out of scope", "--stdin"], "- the rest.\n")
    _ok(["advance"])
    _ok(["advance"])
    _work(repo, "fast.txt")
    _round_asking_for_changes()                                   # F-01
    _round_asking_for_changes(("F-01", "open"), severity="should-fix")   # F-02


def _review_verdict(repo, number):
    path = repo / "docs" / "projects" / "thing" / f"review-{number}.md"
    return review.frontmatter(path)


def test_a_ladder_waives_a_fast_level_at_its_budget_and_climbs(repo):
    _fast_level_at_its_budget(repo)

    waived = json.loads(_ok(["auto", "--json"]).output)

    assert (waived["stop"], waived["reason"]) == (False, None)
    fields = _review_verdict(repo, 3)
    assert (fields["verdict"], fields["reason"]) == ("waived", "review budget reached in a ladder run")
    assert "specflo advance" in waived["payload"]
    assert "F-01" in waived["payload"] and "F-02" in waived["payload"]

    _ok(["advance"])
    climbed = json.loads(_ok(["auto", "--json"]).output)

    assert climbed["reason"] != auto.STOP_REVIEW_BUDGET
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "specflo/thing/full"
    row = _row(repo, "fast")
    assert row["review"].startswith("waived (budget)")
    assert "F-01" in row["review"] and "F-02" in row["review"]


def test_the_open_items_carry_into_the_full_levels_first_round(repo):
    _fast_level_at_its_budget(repo)
    _ok(["auto", "--json"])
    _ok(["advance"])
    _ok(["auto", "--json"])

    scope = json.loads(_ok(["review", "start", "--json"]).output)

    assert scope["scope"] == "delta"
    assert scope["items"] == ["F-01", "F-02"]


def test_a_ladder_at_full_level_waives_at_its_budget_and_ends(repo):
    _ladder_at_full(repo)
    _ok(["advance"])
    _ok(["advance"])
    _ok(["advance"])
    _work(repo, "full.txt")
    # The round the climb opened, then one more: both ask for changes.
    _ok(["review", "finding", "add", "--severity", "blocker", "--text", "Wrong"])
    _ok(["review", "done"])
    _round_asking_for_changes(("F-01", "open"))
    reasons = []

    waived = json.loads(_ok(["auto", "--json"]).output)
    reasons.append(waived["reason"])
    _ok(["advance"])
    closing = json.loads(_ok(["auto", "--json"]).output)
    reasons.append(closing["reason"])

    assert auto.STOP_REVIEW_BUDGET not in reasons
    assert closing["reason"] == auto.STOP_PROJECT_COMPLETE
    row = _row(repo, "full")
    assert row["review"].startswith("waived (budget)")
    assert "F-01" in row["review"] and "F-02" in row["review"]

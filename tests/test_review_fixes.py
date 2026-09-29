"""Review start waits for fix tasks: a round opens once each open item is fixed by a done task.

After a round asks for changes, each open item - a blocker or should-fix
finding no reviewed round has checked closed - needs a task that fixes it
(``task add --fixes F-NN``) and is done before ``review start`` opens the next
round, with or without --full or --over-budget. The refusal names each item
and the command, and changes no file. A waive is never refused. A nit never
needs a fix task, and a superseded task fixes nothing. A hosted project is
refused the same way.
"""

import json

import pytest
from typer.testing import CliRunner

from specflo import config, plan, projects, review, spec
from specflo.cli import app
from specflo.errors import SpecfloError
from specflo.service.local import LocalProjectService
from reviewhelp import write_none
from test_hosted_parity import _hosted_steps, _local_steps, _pipeline

runner = CliRunner()

# Every form of `review start` the refusal covers.
_STARTS = [["review", "start"], ["review", "start", "--full"], ["review", "start", "--over-budget"]]


def _steps():
    """The pipeline through T-01 done, then a round that asks for changes: should-fix F-01."""
    pipeline = _pipeline()
    done = next(i for i, (args, _) in enumerate(pipeline) if args[:2] == ["task", "done"])
    return [
        *pipeline[: done + 1],
        (["review", "start"], None),
        (["review", "finding", "add", "--severity", "should-fix", "--at", "src/app.py:9-12",
          "--text", "A message names the wrong command"], None),
        (["review", "done"], None),
    ]


def _assert_all_ok(results):
    """Each step's last run exits 0: the pipeline validates the brainstorm once
    before it is complete, and again after."""
    codes = {tuple(args): (code, text) for args, code, text in results[1:]}
    assert all(code == 0 for code, _ in codes.values()), codes


def _ok(args):
    result = runner.invoke(app, args)
    assert result.exit_code == 0, (args, result.output)
    return result


def _add_fix(text: str, *fixes: str) -> str:
    """`task add` fixing ``fixes``; the new task's ID."""
    args = ["task", "add", "--text", text, "--acceptance", "fixed", "--verify", "uv run pytest"]
    for fix in fixes:
        args += ["--fixes", fix]
    return _ok(args).output.split()[1]


def _files(project_dir) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in project_dir.iterdir() if path.is_file()}


def _refused_starts(project_dir) -> list[tuple[int, str]]:
    """Each form of review start's ``(exit code, output)``; no file changes after each."""
    outputs = []
    for args in _STARTS:
        before = _files(project_dir)
        result = runner.invoke(app, args)
        assert _files(project_dir) == before, args
        outputs.append((result.exit_code, result.output))
    return outputs


def _opened() -> dict:
    """`review start --json`, less the locator and path, which name where the project lives."""
    opened = json.loads(_ok(["review", "start", "--json"]).output)
    return {key: opened[key] for key in ("created", "scope", "range", "items")}


def _assert_refused(outputs, *named):
    for code, output in outputs:
        assert code == 1, output
        for text in named:
            assert text in output, output


def test_review_start_waits_for_a_done_fix_task(tmp_path, monkeypatch):
    results, project_dir = _local_steps(tmp_path, monkeypatch, _steps())
    _assert_all_ok(results)

    _assert_refused(
        _refused_starts(project_dir),
        "F-01 (no fix task)", "specflo task add --fixes F-01",
        "specflo review waive --reason <why>",
    )

    fix = _add_fix("Name the right command", "F-01")
    _assert_refused(_refused_starts(project_dir), "F-01 (T-02 is not done)",
                    "specflo task add --fixes F-01")
    _ok(["task", "start", fix])
    _assert_refused(_refused_starts(project_dir), "F-01 (T-02 is not done)")

    _ok(["task", "done", fix])
    opened = json.loads(_ok(["review", "start", "--json"]).output)
    assert (opened["created"], opened["items"]) == (True, ["F-01"])
    assert (project_dir / "review-2.md").is_file()


def test_review_waive_is_not_refused(tmp_path, monkeypatch):
    results, project_dir = _local_steps(tmp_path, monkeypatch, _steps())
    _assert_all_ok(results)
    _assert_refused(_refused_starts(project_dir)[:1], "F-01")

    waived = _ok(["review", "waive", "--reason", "Checked by hand"])

    assert "review-2 closed waived" in waived.output
    assert review.frontmatter(project_dir / "review-2.md")["verdict"] == "waived"
    # A waive reviewed nothing: the item is still open, and still unfixed.
    _assert_refused(_refused_starts(project_dir)[:1], "F-01 (no fix task)")


def test_the_fix_refusal_comes_before_the_budget_refusal(tmp_path, monkeypatch):
    """Round 2 spends the budget of two rounds and leaves F-02 open: F-02 is
    named first, and the budget refusal comes only once F-02 is fixed."""
    results, project_dir = _local_steps(tmp_path, monkeypatch, _steps())
    _assert_all_ok(results)
    first = _add_fix("Name the right command", "F-01")
    _ok(["task", "start", first])
    _ok(["task", "done", first])
    _ok(["review", "start"])
    _ok(["review", "finding", "check", "F-01", "closed"])
    _ok(["review", "finding", "add", "--severity", "should-fix", "--at", "src/app.py:3",
         "--text", "The close drops the sha"])
    _ok(["review", "done"])

    refused = runner.invoke(app, ["review", "start"])
    assert refused.exit_code == 1
    assert "F-02 (no fix task)" in refused.output
    assert "--over-budget" not in refused.output

    second = _add_fix("Keep the sha", "F-02")
    _ok(["task", "start", second])
    _ok(["task", "done", second])
    refused = runner.invoke(app, ["review", "start"])
    assert refused.exit_code == 1
    assert "review budget" in refused.output and "--over-budget" in refused.output
    assert "fix task" not in refused.output
    opened = json.loads(_ok(["review", "start", "--over-budget", "--json"]).output)
    assert (opened["created"], opened["items"]) == (True, ["F-02"])


def test_review_start_is_refused_the_same_way_on_a_hosted_project(
    tmp_path, monkeypatch, live_daemon
):
    steps = _steps()
    local, local_dir = _local_steps(tmp_path, monkeypatch, steps)
    local_refusals = _refused_starts(local_dir)
    local_fix = _add_fix("Name the right command", "F-01")
    local_pending = _refused_starts(local_dir)
    _ok(["task", "start", local_fix])
    _ok(["task", "done", local_fix])
    local_opened = _opened()

    hosted, hosted_dir = _hosted_steps(tmp_path, monkeypatch, live_daemon, steps)
    _assert_all_ok(hosted)
    hosted_refusals = _refused_starts(hosted_dir)
    hosted_fix = _add_fix("Name the right command", "F-01")
    hosted_pending = _refused_starts(hosted_dir)
    _ok(["task", "start", hosted_fix])
    _ok(["task", "done", hosted_fix])
    hosted_opened = _opened()

    _assert_refused(hosted_refusals, "F-01 (no fix task)", "specflo task add --fixes F-01")
    assert hosted_refusals == local_refusals
    assert hosted_pending == local_pending
    assert hosted_opened == local_opened == {
        "created": True, "scope": "whole-branch", "range": None, "items": ["F-01"],
    }
    assert (hosted_dir / "review-2.md").is_file()


# --- which items and which tasks count ----------------------------------------


@pytest.fixture
def reviewed(tmp_path):
    """A project with a plan and a closed round 1 that asked for changes:
    blocker F-01 and nit F-02. (root, cfg, slug, service)."""
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing")
    spec.start_spec(tmp_path, cfg, "thing")
    spec.add_requirement(tmp_path, cfg, "thing", "Prints help", acceptance="exits 0")
    plan.start_plan(tmp_path, cfg, "thing")
    plan.add_task(tmp_path, cfg, "thing", "Build help", "help prints", "uv run pytest",
                  implements=["REQ-01"])
    review.start_round(tmp_path, cfg, "thing", sha="")
    review.add_finding(tmp_path, cfg, "thing", "blocker", "The close drops the sha", "src/app.py:3")
    review.add_finding(tmp_path, cfg, "thing", "nit", "A name reads oddly")
    review.close_round(tmp_path, cfg, "thing", nits_followup=False)
    return tmp_path, cfg, "thing", LocalProjectService(tmp_path, cfg)


def _done_fix(root, cfg, slug, *fixes, text="Keep the sha") -> str:
    task = plan.add_task(root, cfg, slug, text, "kept", "uv run pytest",
                         implements=[], fixes=list(fixes))
    plan.start_task(root, cfg, slug, task.id)
    plan.done_task(root, cfg, slug, task.id)
    return task.id


def test_a_nit_needs_no_fix_task(reviewed):
    root, cfg, slug, service = reviewed
    assert review.unfixed_items(root, cfg, slug) == {"F-01": []}
    _done_fix(root, cfg, slug, "F-01")
    assert review.unfixed_items(root, cfg, slug) == {}
    path, created = service.start_round(slug, sha="")
    assert (path.name, created) == ("review-2.md", True)


def test_a_superseded_task_fixes_nothing(reviewed):
    root, cfg, slug, service = reviewed
    fix = _done_fix(root, cfg, slug, "F-01")
    plan.add_task(root, cfg, slug, "Build help again", "help prints", "uv run pytest",
                  implements=["REQ-01"], supersedes=fix)
    # Superseding resets the old task to pending; mark it done by hand, so only
    # its status keeps it from counting.
    path = plan.plan_path(root, cfg, slug)
    document = path.read_text()
    start = document.index(f"### {fix} ")
    path.write_text(document[:start] + document[start:].replace(
        "- Progress: pending", "- Progress: done", 1))
    assert next(t for t in plan.list_tasks(root, cfg, slug, include_superseded=True)
                if t.id == fix).progress == "done"

    assert review.unfixed_items(root, cfg, slug) == {"F-01": []}
    with pytest.raises(SpecfloError, match=r"F-01 \(no fix task\)"):
        service.start_round(slug, sha="")


def test_a_project_with_no_plan_is_refused_naming_the_item(tmp_path):
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing")
    review.start_round(tmp_path, cfg, "thing", sha="")
    review.add_finding(tmp_path, cfg, "thing", "should-fix", "A message names the wrong command",
                       "src/app.py:9-12")
    review.close_round(tmp_path, cfg, "thing", nits_followup=False)
    with pytest.raises(SpecfloError, match=r"F-01 \(no fix task\)"):
        LocalProjectService(tmp_path, cfg).start_round("thing", sha="")
    assert [path.name for _, path in review.round_files(tmp_path, cfg, "thing")] == ["review-1.md"]


def test_an_item_checked_closed_needs_no_fix_task(reviewed):
    root, cfg, slug, service = reviewed
    _done_fix(root, cfg, slug, "F-01")
    service.start_round(slug, sha="")
    review.check_finding(root, cfg, slug, "F-01", "closed")
    review.add_finding(root, cfg, slug, "should-fix", "A message names the wrong command",
                       "src/app.py:9-12")
    review.close_round(root, cfg, slug, nits_followup=False)
    assert review.unfixed_items(root, cfg, slug) == {"F-03": []}
    _done_fix(root, cfg, slug, "F-03", text="Name the right command")
    path, created = service.start_round(slug, sha="", over_budget=True)
    assert (path.name, created) == ("review-3.md", True)


def test_an_open_round_is_handed_back_without_the_check(reviewed):
    root, cfg, slug, service = reviewed
    _done_fix(root, cfg, slug, "F-01")
    opened, _ = service.start_round(slug, sha="")
    plan.reopen_task(root, cfg, slug, "T-02")
    path, created = service.start_round(slug, sha="")
    assert (path, created) == (opened, False)


def test_the_ladder_and_the_waive_mint_a_round_without_the_check(reviewed):
    root, cfg, slug, service = reviewed
    path, created = review.start_round(root, cfg, slug, sha="")
    assert (path.name, created) == ("review-2.md", True)
    review.close_round(root, cfg, slug, "waived", reason="Checked by hand")
    assert service.waive_round(slug, "Checked by hand again", sha="").name == "review-3.md"


# --- an item a later round checks open again ----------------------------------


def _check_open_and_close(root, cfg, slug, service, **start):
    """Open the next round, check F-01 open and close it: changes-requested."""
    path, _ = service.start_round(slug, sha="", **start)
    review.check_finding(root, cfg, slug, "F-01", "open")
    write_none(path)
    review.close_round(root, cfg, slug, nits_followup=False)


def test_an_item_checked_open_again_needs_one_more_done_fix_task(reviewed):
    root, cfg, slug, service = reviewed
    _done_fix(root, cfg, slug, "F-01")
    _check_open_and_close(root, cfg, slug, service)

    # The fix done before round 2 did not hold, so it no longer counts.
    assert list(review.unfixed_items(root, cfg, slug)) == ["F-01"]
    assert review.reopened_items(root, cfg, slug) == {"F-01": "review-2.md"}
    with pytest.raises(SpecfloError) as refused:
        service.start_round(slug, sha="", over_budget=True)
    assert str(refused.value).startswith(
        "No review round opens while an open item waits for a fix: F-01 (review-2.md checked"
        " it open after its fix; it needs a new fix task). Each needs a task that fixes it,"
        " added with `specflo task add --fixes F-01`"
    )
    assert "no fix task" not in str(refused.value)
    assert [path.name for _, path in review.round_files(root, cfg, slug)] == [
        "review-1.md", "review-2.md"]

    _done_fix(root, cfg, slug, "F-01", text="Keep the sha on every path")
    assert review.unfixed_items(root, cfg, slug) == {}
    path, created = service.start_round(slug, sha="", over_budget=True)
    assert (path.name, created) == ("review-3.md", True)


def test_each_open_check_needs_its_own_done_fix_task(reviewed):
    root, cfg, slug, service = reviewed
    _done_fix(root, cfg, slug, "F-01")
    _check_open_and_close(root, cfg, slug, service)
    _done_fix(root, cfg, slug, "F-01", text="Keep the sha on every path")
    _check_open_and_close(root, cfg, slug, service, over_budget=True)

    assert list(review.unfixed_items(root, cfg, slug)) == ["F-01"]
    _done_fix(root, cfg, slug, "F-01", text="Keep the sha on the last path")
    assert review.unfixed_items(root, cfg, slug) == {}


def test_a_waived_round_checking_an_item_open_asks_no_more_fix_tasks(reviewed):
    root, cfg, slug, service = reviewed
    _done_fix(root, cfg, slug, "F-01")
    path, _ = service.start_round(slug, sha="")
    review.check_finding(root, cfg, slug, "F-01", "open")
    write_none(path)
    review.close_round(root, cfg, slug, "waived", reason="Checked by hand")

    # A waive reviewed nothing, so its check is not a finding that the fix failed.
    assert review.unfixed_items(root, cfg, slug) == {}


def test_a_task_superseding_a_failed_fix_counts_once_done(reviewed):
    root, cfg, slug, service = reviewed
    failed = _done_fix(root, cfg, slug, "F-01")
    _check_open_and_close(root, cfg, slug, service)

    task = plan.add_task(root, cfg, slug, "Keep the sha on every path", "kept", "uv run pytest",
                         implements=[], fixes=["F-01"], supersedes=failed)
    plan.start_task(root, cfg, slug, task.id)
    plan.done_task(root, cfg, slug, task.id)

    assert review.unfixed_items(root, cfg, slug) == {}
    path, created = service.start_round(slug, sha="", over_budget=True)
    assert (path.name, created) == ("review-3.md", True)


def test_a_round_that_checks_an_item_open_before_any_fix_marks_no_fix_failed(reviewed):
    root, cfg, slug, service = reviewed
    # The ladder's climb opens a round without the fix check.
    path, _ = review.start_round(root, cfg, slug, sha="")
    review.check_finding(root, cfg, slug, "F-01", "open")
    write_none(path)
    review.close_round(root, cfg, slug, nits_followup=False)

    assert "failed_fixes" not in review.frontmatter(path)
    assert review.reopened_items(root, cfg, slug) == {}
    with pytest.raises(SpecfloError, match=r"F-01 \(no fix task\)"):
        service.start_round(slug, sha="", over_budget=True)
    _done_fix(root, cfg, slug, "F-01")
    path, created = service.start_round(slug, sha="", over_budget=True)
    assert (path.name, created) == ("review-3.md", True)


@pytest.mark.parametrize("checks", ["- F-1 open", "- F-01 open\n- F-01 open"])
def test_a_check_is_read_by_its_number_and_once(reviewed, checks):
    root, cfg, slug, service = reviewed
    failed = _done_fix(root, cfg, slug, "F-01")
    path, _ = service.start_round(slug, sha="")
    doc = path.read_text().replace(
        "## Findings", f"{review.EARLIER_HEADER}\n\n{checks}\n\n## Findings", 1)
    path.write_text(doc)
    write_none(path)
    review.close_round(root, cfg, slug, nits_followup=False)

    assert review.frontmatter(path)["failed_fixes"] == {"F-01": [failed]}
    assert review.reopened_items(root, cfg, slug) == {"F-01": "review-2.md"}
    _done_fix(root, cfg, slug, "F-01", text="Keep the sha on every path")
    assert review.unfixed_items(root, cfg, slug) == {}


def test_a_round_that_checks_nothing_open_records_no_failed_fix(reviewed):
    root, cfg, slug, service = reviewed
    _done_fix(root, cfg, slug, "F-01")
    path, _ = service.start_round(slug, sha="")
    review.check_finding(root, cfg, slug, "F-01", "closed")
    write_none(path)
    review.close_round(root, cfg, slug, nits_followup=False)

    assert "failed_fixes" not in review.frontmatter(path)
    assert "failed_fixes" not in path.read_text()


def test_a_fix_done_while_a_round_is_open_is_not_failed_by_its_close(reviewed):
    root, cfg, slug, service = reviewed
    # The ladder's climb opens a round without the fix check; the fix lands
    # while the round is open, so the round never reviewed it.
    path, _ = review.start_round(root, cfg, slug, sha="")
    review.check_finding(root, cfg, slug, "F-01", "open")
    write_none(path)
    _done_fix(root, cfg, slug, "F-01")
    review.close_round(root, cfg, slug, nits_followup=False)

    assert "failed_fixes" not in review.frontmatter(path)
    path, created = service.start_round(slug, sha="", over_budget=True)
    assert (path.name, created) == ("review-3.md", True)


def test_a_superseding_fix_done_while_a_round_is_open_is_not_failed(reviewed):
    root, cfg, slug, service = reviewed
    failed = _done_fix(root, cfg, slug, "F-01")
    path, _ = service.start_round(slug, sha="")
    review.check_finding(root, cfg, slug, "F-01", "open")
    write_none(path)
    task = plan.add_task(root, cfg, slug, "Keep the sha on every path", "kept", "uv run pytest",
                         implements=[], fixes=["F-01"], supersedes=failed)
    plan.start_task(root, cfg, slug, task.id)
    plan.done_task(root, cfg, slug, task.id)
    review.close_round(root, cfg, slug, nits_followup=False)

    assert review.frontmatter(path)["failed_fixes"] == {"F-01": [failed]}
    assert review.unfixed_items(root, cfg, slug) == {}


def test_a_round_handed_back_untouched_takes_the_fixes_done_by_then(reviewed):
    root, cfg, slug, service = reviewed
    path, _ = review.start_round(root, cfg, slug, sha="")
    fix = _done_fix(root, cfg, slug, "F-01")
    # Nobody wrote into the round, so handing it back re-reads what it reviews.
    assert service.start_round(slug, sha="") == (path, False)
    review.check_finding(root, cfg, slug, "F-01", "open")
    write_none(path)
    review.close_round(root, cfg, slug, nits_followup=False)

    assert review.frontmatter(path)["failed_fixes"] == {"F-01": [fix]}
    assert review.reopened_items(root, cfg, slug) == {"F-01": "review-2.md"}


def test_a_round_opened_with_no_saved_fixes_fails_nothing(reviewed):
    root, cfg, slug, service = reviewed
    _done_fix(root, cfg, slug, "F-01")
    path, _ = service.start_round(slug, sha="")
    # A round opened before rounds saved their fixes carries no list.
    fields = review.frontmatter(path)
    fields.pop("fixes_at_open", None)
    path.write_text(review._render(fields, review.body_of(path)))
    review.check_finding(root, cfg, slug, "F-01", "open")
    write_none(path)
    review.close_round(root, cfg, slug, nits_followup=False)

    assert "failed_fixes" not in review.frontmatter(path)

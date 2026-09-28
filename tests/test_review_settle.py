"""Settling a finding without a fix: `review finding reject F-NN --reason <why>`.

A reject writes '- F-NN rejected: <why>' under a Settled section appended to
the round that recorded the finding; every byte before it stays. Only an open
item is settled: a blocker or should-fix finding of a closed round that no
reviewed round has checked closed. A nit, an ID no closed round records, an
item checked closed, one already settled, a finding of the open round, an
item the open round has checked, and an empty or multi-line reason are
refused, and a refusal changes no file. A settled finding leaves the ledger:
no later round checks it, review start needs no fix task for it, and task add
--fixes naming it is refused. A hosted project works the same way, and the
daemon runs no git.
"""

import json

import pytest
from typer.testing import CliRunner

from specflo import config, plan, projects, review, spec
from specflo.cli import app
from specflo.errors import SpecfloError
from specflo.service.local import LocalProjectService
from reviewhelp import write_none
from test_hosted_parity import (
    _hosted_steps, _local_steps, _pipeline, _recording_git, _without_follow_up_lines,
    SLUG as PARITY_SLUG,
)

runner = CliRunner()

SLUG = "thing"
REASON = "The message names the command this caller runs"


def _snapshot(directory) -> dict[str, bytes]:
    """Every file under ``directory``, by its name, as bytes."""
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in sorted(directory.rglob("*")) if path.is_file()
    }


def _done_fix(root, cfg, slug, *fixes) -> str:
    """A done task fixing ``fixes``; its ID."""
    task = plan.add_task(root, cfg, slug, f"Fix {', '.join(fixes)}", "fixed", "uv run pytest",
                         implements=[], fixes=list(fixes))
    plan.start_task(root, cfg, slug, task.id)
    plan.done_task(root, cfg, slug, task.id)
    return task.id


def _project(root):
    """A project with a started plan: (cfg, project dir)."""
    cfg = config.init_config(root)
    projects.create_project(root, cfg, "Thing")
    spec.start_spec(root, cfg, SLUG)
    spec.add_requirement(root, cfg, SLUG, "Prints help", acceptance="exits 0")
    plan.start_plan(root, cfg, SLUG)
    plan.add_task(root, cfg, SLUG, "Build help", "help prints", "uv run pytest",
                  implements=["REQ-01"])
    return cfg, root / "docs" / "projects" / SLUG


@pytest.fixture
def single(tmp_path):
    """A closed round 1 that asked for changes for one item, should-fix F-01:
    (root, cfg, service, project dir)."""
    cfg, directory = _project(tmp_path)
    review.start_round(tmp_path, cfg, SLUG, sha="")
    review.add_finding(tmp_path, cfg, SLUG, "should-fix", "A message names the wrong command",
                       "src/app.py:9-12")
    review.close_round(tmp_path, cfg, SLUG, nits_followup=False)
    return tmp_path, cfg, LocalProjectService(tmp_path, cfg), directory


@pytest.fixture
def ledger(tmp_path):
    """Three rounds. Round 1 asked for changes (blocker F-01, should-fix F-02,
    nit F-03); round 2 checked F-01 closed and F-02 open, and asked for
    changes (blocker F-04); round 3 is open and has found should-fix F-05.
    Every open item has a done fix task. (root, cfg, service, project dir)."""
    cfg, directory = _project(tmp_path)
    service = LocalProjectService(tmp_path, cfg)
    review.start_round(tmp_path, cfg, SLUG, sha="")
    review.add_finding(tmp_path, cfg, SLUG, "blocker", "The close drops the sha", "src/app.py:3")
    review.add_finding(tmp_path, cfg, SLUG, "should-fix", "A message names the wrong command",
                       "src/app.py:9-12")
    review.add_finding(tmp_path, cfg, SLUG, "nit", "A name reads oddly")
    review.close_round(tmp_path, cfg, SLUG, nits_followup=False)
    _done_fix(tmp_path, cfg, SLUG, "F-01", "F-02")
    service.start_round(SLUG, sha="")
    review.check_finding(tmp_path, cfg, SLUG, "F-01", "closed")
    review.check_finding(tmp_path, cfg, SLUG, "F-02", "open")
    review.add_finding(tmp_path, cfg, SLUG, "blocker", "The lock is dropped early", "src/app.py:20")
    review.close_round(tmp_path, cfg, SLUG, nits_followup=False)
    _done_fix(tmp_path, cfg, SLUG, "F-04")
    service.start_round(SLUG, sha="", over_budget=True)
    review.add_finding(tmp_path, cfg, SLUG, "should-fix", "A refusal names no ID", "src/app.py:30")
    return tmp_path, cfg, service, directory


# --- the Settled section --------------------------------------------------------------


def test_reject_writes_the_reason_under_settled_in_the_round_that_recorded_it(single):
    root, cfg, service, directory = single
    path = directory / "review-1.md"
    before = path.read_text()
    assert before.endswith("## Verdict\n")

    assert service.reject_finding(SLUG, "F-01", REASON) == ("F-01", path)

    assert path.read_text() == before + f"\n## Settled\n\n- F-01 rejected: {REASON}\n"


def test_a_second_settled_finding_joins_the_same_section(ledger):
    root, cfg, service, directory = ledger
    first, second = directory / "review-1.md", directory / "review-2.md"
    before = first.read_text()

    assert service.reject_finding(SLUG, "F-04", "The lock is held by the caller") == (
        "F-04", second
    )
    # The finding is settled in the round that recorded it, never another.
    assert first.read_text() == before
    assert second.read_text().endswith(
        "## Verdict\n\n## Settled\n\n- F-04 rejected: The lock is held by the caller\n"
    )
    assert service.reject_finding(SLUG, "F-2", f"  {REASON}  ") == ("F-02", first)
    assert first.read_text() == before + f"\n## Settled\n\n- F-02 rejected: {REASON}\n"

    # A third finding of round 2, added by hand, joins round 2's section.
    text = second.read_text()
    second.write_text(text.replace(
        "- F-04 (blocker)", "- F-06 (should-fix) [src/app.py:40] One more\n- F-04 (blocker)"
    ))
    service.reject_finding(SLUG, "F-06", "Not a problem")
    assert second.read_text().endswith(
        "## Settled\n\n- F-04 rejected: The lock is held by the caller\n"
        "- F-06 rejected: Not a problem\n"
    )


def test_a_settled_line_outside_the_settled_section_settles_nothing(single):
    root, cfg, service, directory = single
    path = directory / "review-1.md"
    path.write_text(path.read_text().replace(
        "## Scope reviewed\n", f"## Scope reviewed\n\n- F-01 rejected: {REASON}\n"
    ))
    assert review.items_to_check(root, cfg, SLUG, 2) == ["F-01"]
    assert review.unfixed_items(root, cfg, SLUG) == {"F-01": []}


# --- a settled finding leaves the ledger ------------------------------------------------


def test_a_rejected_only_item_leaves_nothing_to_check_or_fix(single):
    root, cfg, service, directory = single
    service.reject_finding(SLUG, "F-01", REASON)

    assert review.unfixed_items(root, cfg, SLUG) == {}
    assert review.items_to_check(root, cfg, SLUG, 2) == []
    with pytest.raises(SpecfloError) as refused:
        review.check_fixes(root, cfg, SLUG, ["F-01"])
    assert str(refused.value) == (
        "F-01 was rejected in review-1.md. Open items a task can fix: none."
    )

    # No fix task, and the round opens with no items.
    path, created = service.start_round(SLUG, sha="")
    assert (path.name, created) == ("review-2.md", True)
    assert service.review_scope(SLUG)["items"] == []
    assert "No earlier items to check this round." in service.review_prompt(SLUG)
    before = _snapshot(directory)
    with pytest.raises(SpecfloError) as refused:
        service.check_finding(SLUG, "F-01", "closed")
    assert str(refused.value) == (
        "F-01 was rejected in review-1.md. Items this round checks: none."
    )
    assert _snapshot(directory) == before

    write_none(path)
    closed = service.close_round(SLUG)
    assert (closed.verdict, closed.still_open) == ("ready-to-merge", [])
    assert review.review_state(root, cfg, SLUG)["open_items"] == []


def test_a_rejected_item_leaves_the_open_round_it_was_not_checked_in(ledger):
    root, cfg, service, directory = ledger
    assert service.review_scope(SLUG)["items"] == ["F-02", "F-04"]

    service.reject_finding(SLUG, "F-04", "The lock is held by the caller")

    assert service.review_scope(SLUG)["items"] == ["F-02"]
    with pytest.raises(SpecfloError, match=r"^F-04 was rejected in review-2\.md\. Items this"):
        service.check_finding(SLUG, "F-04", "open")
    with pytest.raises(SpecfloError, match=r"^F-04 was rejected in review-2\.md\. Open items"):
        review.check_fixes(root, cfg, SLUG, ["F-04"])
    # The round closes once the item left is checked; F-04 needs no check.
    service.check_finding(SLUG, "F-02", "closed")
    closed = service.close_round(SLUG)
    assert (closed.verdict, closed.still_open) == ("changes-requested", [])
    assert review.items_to_check(root, cfg, SLUG, 4) == ["F-05"]


# --- refusals ---------------------------------------------------------------------------


# Each refused reject on the ledger once F-04 is rejected, and its whole refusal.
_REFUSED = [
    ("F-03", REASON, "F-03 is a nit, and a nit is never rejected. Open items: F-02."),
    ("F-09", REASON, "No closed review round records a finding F-09. Open items: F-02."),
    ("F-01", REASON, "F-01 was already checked closed in review-2.md. Open items: F-02."),
    ("F-04", REASON, "F-04 was already rejected in review-2.md. Open items: F-02."),
    ("F-05", REASON,
     "F-05 is a finding of the open round review-3.md; close the round with"
     " `specflo review done` first. Open items: F-02."),
    ("X-1", REASON, "'X-1' is not a finding ID; one looks like F-01. Open items: F-02."),
    ("F-02", "", "A rejection needs a non-empty --reason, so the round that recorded the"
                 " finding says why it is not a problem."),
    ("F-02", "   ", "A rejection needs a non-empty --reason, so the round that recorded the"
                    " finding says why it is not a problem."),
    ("F-02", "One\nTwo", "A rejection's --reason is one line: remove the line break."),
]


@pytest.mark.parametrize("finding_id, reason, refusal", _REFUSED)
def test_a_reject_of_anything_but_an_open_item_is_refused_and_changes_no_file(
    ledger, finding_id, reason, refusal
):
    root, cfg, service, directory = ledger
    service.reject_finding(SLUG, "F-04", "The lock is held by the caller")
    before = _snapshot(directory)

    with pytest.raises(SpecfloError) as refused:
        service.reject_finding(SLUG, finding_id, reason)

    assert str(refused.value) == refusal
    assert _snapshot(directory) == before


def test_a_reject_of_an_item_the_open_round_checked_is_refused(ledger):
    root, cfg, service, directory = ledger
    service.check_finding(SLUG, "F-02", "open")
    before = _snapshot(directory)

    with pytest.raises(SpecfloError) as refused:
        service.reject_finding(SLUG, "F-02", REASON)

    assert str(refused.value) == (
        "F-02 is checked in the open round review-3.md; close the round with"
        " `specflo review done` first. Open items: F-02, F-04."
    )
    assert _snapshot(directory) == before


def test_a_reject_with_no_round_is_refused(tmp_path):
    cfg, directory = _project(tmp_path)
    before = _snapshot(directory)
    with pytest.raises(SpecfloError) as refused:
        LocalProjectService(tmp_path, cfg).reject_finding(SLUG, "F-01", REASON)
    assert str(refused.value) == (
        "No closed review round records a finding F-01. Open items: none."
    )
    assert _snapshot(directory) == before


# --- the CLI, local and hosted ----------------------------------------------------------


def _steps():
    """The pipeline through T-01 done, a round that asked for changes for its
    only item, should-fix F-01, the reject, and the round after it."""
    pipeline = _pipeline()
    done = next(i for i, (args, _) in enumerate(pipeline) if args[:2] == ["task", "done"])
    return [
        *pipeline[: done + 1],
        (["review", "start"], None),
        (["review", "finding", "add", "--severity", "should-fix", "--at", "src/app.py:9-12",
          "--text", "A message names the wrong command"], None),
        (["review", "done"], None),
        (["review", "finding", "reject", "F-01", "--reason", REASON], None),
        (["doc", "show", "review-1"], None),
        (["status"], None),
        (["review", "start"], None),
        (["review", "prompt"], None),
    ]


# Each refused command once round 2 is open, and what its refusal says.
_CLI_REFUSED = [
    (["review", "finding", "reject", "F-01", "--reason", "Again"],
     "F-01 was already rejected in review-1.md. Open items: none."),
    (["review", "finding", "reject", "F-01", "--reason", ""], "A rejection needs a non-empty --reason"),
    (["review", "finding", "check", "F-01", "closed"],
     "F-01 was rejected in review-1.md. Items this round checks: none."),
    (["task", "add", "--text", "Fix it", "--acceptance", "fixed", "--verify", "uv run pytest",
      "--fixes", "F-01"],
     "F-01 was rejected in review-1.md. Open items a task can fix: none."),
]


def _outputs(results) -> dict[tuple, tuple[int, str]]:
    """Each step's ``(exit code, stdout)``, by its arguments."""
    return {tuple(args): (code, text) for args, code, text in results[1:]}


def _cli_refusals(directory) -> list[tuple[int, str]]:
    """Each refused command's ``(exit code, output)``; no file changes on any."""
    outputs = []
    for args, _ in _CLI_REFUSED:
        before = _snapshot(directory)
        result = runner.invoke(app, args)
        assert _snapshot(directory) == before, args
        outputs.append((result.exit_code, result.output))
    return outputs


def test_review_finding_reject_settles_the_item_for_the_next_round(tmp_path, monkeypatch):
    results, directory = _local_steps(tmp_path, monkeypatch, _steps())
    out = _outputs(results)
    assert all(code == 0 for code, _ in out.values()), out

    assert out[("review", "finding", "reject", "F-01", "--reason", REASON)][1] == (
        f"Rejected F-01 in {PARITY_SLUG}/review-1.\n"
    )
    shown = out[("doc", "show", "review-1")][1]
    assert shown.endswith(f"## Verdict\n\n## Settled\n\n- F-01 rejected: {REASON}\n")
    # No fix task: the next round opens, and it checks nothing.
    assert out[("review", "start")][1] == f"{PARITY_SLUG}/review-2\nScope: whole branch\n"
    assert "No earlier items to check this round." in out[("review", "prompt")][1]
    assert "- F-01" not in out[("review", "prompt")][1]
    assert "F-01" not in out[("status",)][1]

    for (args, refusal), (code, output) in zip(_CLI_REFUSED, _cli_refusals(directory)):
        assert code == 1, (args, output)
        assert refusal in output, (args, output)


def test_review_finding_reject_json_names_the_round(single, monkeypatch):
    root, cfg, service, directory = single
    projects.switch_project(root, cfg, SLUG)
    monkeypatch.chdir(root)
    result = runner.invoke(
        app, ["review", "finding", "reject", "F-01", "--reason", REASON, "--json"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "id": "F-01", "reason": REASON, "locator": f"{SLUG}/review-1",
        "path": str(directory / "review-1.md"),
    }


def test_review_finding_reject_on_a_hosted_project_is_the_same_and_runs_no_git_on_the_daemon(
    tmp_path, monkeypatch, live_daemon
):
    steps = _steps()
    local, local_dir = _local_steps(tmp_path, monkeypatch, steps)
    local_refusals = _cli_refusals(local_dir)
    git = _recording_git(monkeypatch)
    hosted, hosted_dir = _hosted_steps(tmp_path, monkeypatch, live_daemon, steps)
    hosted_refusals = _cli_refusals(hosted_dir)

    assert len(local) == len(hosted) == len(steps) + 1
    for mine, theirs in zip(local[1:], hosted[1:]):
        args = mine[0]
        if args[:2] == ["review", "prompt"]:
            # Where nits and follow-ups go is the one line a hosted brief words
            # differently.
            mine = (args, mine[1], _without_follow_up_lines(mine[2]))
            theirs = (args, theirs[1], _without_follow_up_lines(theirs[2]))
        assert mine[1:] == theirs[1:], f"{' '.join(args)}:\nlocal:\n{mine[2]}\nhosted:\n{theirs[2]}"
    assert all(code == 0 for code, _ in _outputs(hosted).values()), hosted
    assert [code for code, _ in hosted_refusals] == [1] * len(_CLI_REFUSED)
    assert hosted_refusals == local_refusals
    for name in ("review-1.md", "review-2.md"):
        assert (hosted_dir / name).read_text() == (local_dir / name).read_text()
    assert (hosted_dir / "review-1.md").read_text().endswith(
        f"\n## Settled\n\n- F-01 rejected: {REASON}\n"
    )

    # The daemon holds no git repository and ran no git; the reject is audited.
    assert not (live_daemon["root"] / ".git").exists()
    assert [argv for on_client, argv in git if not on_client] == []
    audited = [
        json.loads(line)
        for line in (live_daemon["root"] / "audit.jsonl").read_text().splitlines()
    ]
    assert any(
        record["operation"] == "reject_finding" and record["project"] == PARITY_SLUG
        for record in audited
    ), audited

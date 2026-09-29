"""Settling a finding without a fix: `review finding reject F-NN --reason <why>`
and `review finding defer F-NN --do <what>`.

A reject writes '- F-NN rejected: <why>' under a Settled section appended to
the round that recorded the finding; every byte before it stays. A defer
files a follow-up whose title is the finding's text, whose Do line is what a
later project should do and whose From line names the round and the finding,
and writes '- F-NN deferred FU-NN' there instead. Only an open item is
settled: a blocker or should-fix finding of a closed round that no reviewed
round has checked closed. A nit, an ID no closed round records, an item
checked closed, one already settled, a finding of the open round, an item
the open round has checked, and an empty or multi-line reason or Do line are
refused, and a refusal changes no file and files no follow-up. A settled
finding leaves the ledger: no later round checks it, review start needs no
fix task for it, and task add --fixes naming it is refused. A latest round
that asked for changes passes the completion gate once every blocking item
it raised or kept open is settled; the gate names each item that still
blocks. A hosted project rejects the same way, and the daemon runs no git; a
hosted project's defer is refused, naming the follow-up that will route its
follow-ups.
"""

import json

import pytest
from typer.testing import CliRunner

from specflo import config, followup, plan, projects, review, spec
from specflo.cli import app
from specflo.errors import SpecfloError
from specflo.service.local import LocalProjectService
from specflo.service.remote import RemoteProjectService
from reviewhelp import write_none
from test_hosted_parity import (
    _hosted_steps, _local_steps, _pipeline, _recording_git, _without_follow_up_lines,
    SLUG as PARITY_SLUG,
)

runner = CliRunner()

SLUG = "thing"
REASON = "The message names the command this caller runs"
DO = "Name the command the caller runs in every refusal"

# Each way to settle a finding without a fix, by the word its Settled line uses.
_SETTLE = {
    "rejected": lambda service, finding_id: service.reject_finding(SLUG, finding_id, REASON),
    "deferred": lambda service, finding_id: service.defer_finding(SLUG, finding_id, DO),
}


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
    Every open item has a done fix task, and F-02 a second one since round 2
    checked it open. (root, cfg, service, project dir)."""
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
    _done_fix(tmp_path, cfg, SLUG, "F-02", "F-04")
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


def test_parse_settled_reads_a_rejected_and_a_deferred_line():
    doc = (
        "## Settled\n\n- F-01 rejected: Not a problem\n- F-02 deferred FU-07\n"
        "- F-03 deferred\n- F-04 deferred: FU-08\n- F-05 deferred FU-09 later\n"
        "- F-06 rejected FU-10\n"
    )
    assert review.parse_settled(doc) == {
        1: ("rejected", "Not a problem"), 2: ("deferred", "FU-07"),
    }


# --- defer: a follow-up for a later project ---------------------------------------------


def test_defer_files_a_follow_up_and_writes_it_under_settled(single):
    root, cfg, service, directory = single
    path = directory / "review-1.md"
    before = path.read_text()
    assert not (directory / "followup.md").exists()

    assert service.defer_finding(SLUG, "F-1", f"  {DO}  ") == ("F-01", path, "FU-01")

    assert path.read_text() == before + "\n## Settled\n\n- F-01 deferred FU-01\n"
    # The title is the finding's text, so the follow-up list reads well; the
    # Do line is what a later project does; the From line names the round
    # and the finding.
    assert (
        "### FU-01 - A message names the wrong command\n"
        f"- Do: {DO}\n"
        "- From: review-1.md F-01\n"
        "- Status: open\n"
    ) in (directory / "followup.md").read_text()
    assert followup.list_followups(root, cfg) == [followup.FollowUp(
        id="FU-01", project=SLUG, title="A message names the wrong command", do=DO,
        source="review-1.md F-01", status="open",
    )]


def test_a_deferred_and_a_rejected_finding_join_the_same_section(ledger):
    root, cfg, service, directory = ledger
    first, second = directory / "review-1.md", directory / "review-2.md"
    # Follow-up numbers run across every project.
    projects.create_project(root, cfg, "Other")
    followup.add_followup(root, cfg, "other", "Earlier work", "Do it")
    before = first.read_text()

    service.reject_finding(SLUG, "F-04", "The lock is held by the caller")
    text = second.read_text()
    second.write_text(text.replace(
        "- F-04 (blocker)", "- F-06 (should-fix) [src/app.py:40] One more\n- F-04 (blocker)"
    ))
    assert service.defer_finding(SLUG, "F-06", "Split the check") == ("F-06", second, "FU-02")
    assert first.read_text() == before
    assert second.read_text().endswith(
        "## Settled\n\n- F-04 rejected: The lock is held by the caller\n"
        "- F-06 deferred FU-02\n"
    )

    # F-02 was checked open in round 2, but round 1 recorded it.
    assert service.defer_finding(SLUG, "F-02", DO) == ("F-02", first, "FU-03")
    assert first.read_text() == before + "\n## Settled\n\n- F-02 deferred FU-03\n"
    assert [(e.id, e.project, e.title, e.source) for e in followup.list_followups(root, cfg)] == [
        ("FU-01", "other", "Earlier work", None),
        ("FU-02", SLUG, "One more", "review-2.md F-06"),
        ("FU-03", SLUG, "A message names the wrong command", "review-1.md F-02"),
    ]
    assert service.review_scope(SLUG)["items"] == []


# --- a settled finding leaves the ledger ------------------------------------------------


@pytest.mark.parametrize("kind", _SETTLE)
def test_a_settled_only_item_leaves_nothing_to_check_or_fix(single, kind):
    root, cfg, service, directory = single
    _SETTLE[kind](service, "F-01")

    assert review.unfixed_items(root, cfg, SLUG) == {}
    assert review.items_to_check(root, cfg, SLUG, 2) == []
    with pytest.raises(SpecfloError) as refused:
        review.check_fixes(root, cfg, SLUG, ["F-01"])
    assert str(refused.value) == (
        f"F-01 was {kind} in review-1.md. Open items a task can fix: none."
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
        f"F-01 was {kind} in review-1.md. Items this round checks: none."
    )
    assert _snapshot(directory) == before

    write_none(path)
    closed = service.close_round(SLUG)
    assert (closed.verdict, closed.still_open) == ("ready-to-merge", [])
    assert review.review_state(root, cfg, SLUG)["open_items"] == []


@pytest.mark.parametrize("kind", _SETTLE)
def test_a_settled_item_leaves_the_open_round_it_was_not_checked_in(ledger, kind):
    root, cfg, service, directory = ledger
    assert service.review_scope(SLUG)["items"] == ["F-02", "F-04"]

    _SETTLE[kind](service, "F-04")

    assert service.review_scope(SLUG)["items"] == ["F-02"]
    with pytest.raises(SpecfloError, match=rf"^F-04 was {kind} in review-2\.md\. Items this"):
        service.check_finding(SLUG, "F-04", "open")
    with pytest.raises(SpecfloError, match=rf"^F-04 was {kind} in review-2\.md\. Open items"):
        review.check_fixes(root, cfg, SLUG, ["F-04"])
    # The round closes once the item left is checked; F-04 needs no check.
    service.check_finding(SLUG, "F-02", "closed")
    closed = service.close_round(SLUG)
    assert (closed.verdict, closed.still_open) == ("changes-requested", [])
    assert review.items_to_check(root, cfg, SLUG, 4) == ["F-05"]


# --- the completion gate ----------------------------------------------------------------


# What the gate says while a round that asked for changes has settled none of
# its blocking items: the words it has always said.
_ASKS_FOR_CHANGES = (
    "the latest review round ({}) is changes-requested: address the findings,"
    " then run another round with `specflo review start`."
)


# What it says once some are settled and one item, the second field, is not.
_STILL_BLOCKS = (
    "the latest review round ({0}) is changes-requested and {1} still blocks it:"
    " fix it, then run another round with `specflo review start`, or settle it with"
    " `specflo review finding reject {1} --reason <why>` or"
    " `specflo review finding defer {1} --do <what>`."
)


def _all_tasks_done(root, cfg) -> None:
    """Finish the plan's first task, so the review gate is all validate execute asks."""
    plan.start_task(root, cfg, SLUG, "T-01")
    plan.done_task(root, cfg, SLUG, "T-01")
    assert plan.reconcile_issues(root, cfg, SLUG) == []


def _validate_execute() -> tuple[int, dict]:
    """`validate execute --json`'s exit code and payload."""
    result = runner.invoke(app, ["validate", "execute", "--json"])
    return result.exit_code, json.loads(result.stdout)


@pytest.mark.parametrize("kind", _SETTLE)
def test_a_round_whose_only_blocking_item_is_settled_passes_the_gate(single, monkeypatch, kind):
    root, cfg, service, directory = single
    _all_tasks_done(root, cfg)
    projects.switch_project(root, cfg, SLUG)
    monkeypatch.chdir(root)
    code, payload = _validate_execute()
    assert (code, payload["ready"]) == (1, False)
    assert payload["issues"] == [_ASKS_FOR_CHANGES.format("review-1.md")]

    _SETTLE[kind](service, "F-01")

    code, payload = _validate_execute()
    assert (code, payload["ready"], payload["issues"]) == (0, True, [])
    assert service.validate_artifact(SLUG, "execute") == []
    # The round keeps its verdict: settling changes no frontmatter.
    assert review.frontmatter(directory / "review-1.md")["verdict"] == "changes-requested"


@pytest.mark.parametrize("kind", _SETTLE)
def test_a_blocking_item_left_open_fails_the_gate_naming_it(tmp_path, kind):
    cfg, directory = _project(tmp_path)
    service = LocalProjectService(tmp_path, cfg)
    review.start_round(tmp_path, cfg, SLUG, sha="")
    review.add_finding(tmp_path, cfg, SLUG, "should-fix", "A message names the wrong command",
                       "src/app.py:9-12")
    review.add_finding(tmp_path, cfg, SLUG, "should-fix", "A refusal names no ID", "src/app.py:30")
    review.add_finding(tmp_path, cfg, SLUG, "nit", "A name reads oddly")
    review.close_round(tmp_path, cfg, SLUG, nits_followup=False)
    _all_tasks_done(tmp_path, cfg)

    _SETTLE[kind](service, "F-01")

    assert service.validate_artifact(SLUG, "execute") == [
        _STILL_BLOCKS.format("review-1.md", "F-02")
    ]
    assert review.review_state(tmp_path, cfg, SLUG)["passing"] is False
    # The nit never blocks: settling the last should-fix passes the gate.
    _SETTLE[kind](service, "F-02")
    assert service.validate_artifact(SLUG, "execute") == []
    assert review.review_state(tmp_path, cfg, SLUG)["passing"] is True


@pytest.mark.parametrize("kind", _SETTLE)
def test_status_shows_a_round_whose_items_are_all_settled_as_passing(single, monkeypatch, kind):
    root, cfg, service, directory = single
    _all_tasks_done(root, cfg)
    project_md = directory / "project.md"
    project_md.write_text(project_md.read_text().replace("phase: brainstorm", "phase: execute"))
    projects.switch_project(root, cfg, SLUG)
    monkeypatch.chdir(root)
    before = json.loads(runner.invoke(app, ["status", "--json"]).stdout)
    assert (before["phase"], before["review"]["passing"]) == ("execute", False)
    assert "`specflo review start`" in before["next_step"]

    _SETTLE[kind](service, "F-01")

    shown = runner.invoke(app, ["status", "--json"])
    assert shown.exit_code == 0, shown.output
    state = json.loads(shown.stdout)
    assert (state["review"]["verdict"], state["review"]["passing"]) == ("changes-requested", True)
    assert state["review"]["open_items"] == []
    # The next step is the advance, never another round.
    assert state["next_step"] == (
        "All tasks done and every item review-1.md asks for changes on is settled - run"
        " the whole test suite once more, then `specflo advance` to complete the project."
    )
    text = runner.invoke(app, ["status"])
    assert text.exit_code == 0, text.output
    date = review.frontmatter(directory / "review-1.md")["date"]
    assert (
        f"Reviews: 1 round; latest round 1 changes-requested ({date}); passes: every item"
        " settled\n"
    ) in text.output
    assert f"Next:    {state['next_step']}\n" in text.output
    assert "review start" not in text.output


def test_an_item_the_round_kept_open_blocks_until_it_is_settled(ledger):
    """Round 3 checks F-02 open and F-04 closed, and raises should-fix F-05,
    blocker F-06 and a nit. A done fix task does not unblock F-05: only a
    later round checks a fix closed."""
    root, cfg, service, directory = ledger
    service.check_finding(SLUG, "F-02", "open")
    service.check_finding(SLUG, "F-04", "closed")
    review.add_finding(root, cfg, SLUG, "blocker", "The lock leaks", "src/app.py:50")
    review.add_finding(root, cfg, SLUG, "nit", "A comment reads oddly")
    assert service.close_round(SLUG).verdict == "changes-requested"
    _done_fix(root, cfg, SLUG, "F-05")
    _all_tasks_done(root, cfg)
    assert service.validate_artifact(SLUG, "execute") == [_ASKS_FOR_CHANGES.format("review-3.md")]

    service.reject_finding(SLUG, "F-06", REASON)
    assert service.validate_artifact(SLUG, "execute") == [
        "the latest review round (review-3.md) is changes-requested and F-02, F-05 still"
        " block it: fix them, then run another round with `specflo review start`, or settle"
        " each with `specflo review finding reject F-NN --reason <why>` or"
        " `specflo review finding defer F-NN --do <what>`."
    ]
    service.defer_finding(SLUG, "F-02", DO)
    assert service.validate_artifact(SLUG, "execute") == [
        _STILL_BLOCKS.format("review-3.md", "F-05")
    ]
    service.reject_finding(SLUG, "F-05", REASON)
    assert service.validate_artifact(SLUG, "execute") == []


def test_a_changes_requested_round_that_records_no_blocking_item_still_fails_the_gate(single):
    """An old round asked for changes in free-form prose: no item to settle."""
    root, cfg, service, directory = single
    _all_tasks_done(root, cfg)
    path = directory / "review-1.md"
    path.write_text(path.read_text().replace(
        "- F-01 (should-fix) [src/app.py:9-12] A message names the wrong command",
        "- A message names the wrong command",
    ))
    assert service.validate_artifact(SLUG, "execute") == [_ASKS_FOR_CHANGES.format("review-1.md")]


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


_DEFER_NEEDS_DO = (
    "A deferral needs a non-empty --do, so the follow-up says what a later project should do."
)

# Each refused defer on the ledger once F-04 is deferred, and its whole refusal.
_DEFER_REFUSED = [
    ("F-03", DO, "F-03 is a nit, and a nit is never deferred. Open items: F-02."),
    ("F-09", DO, "No closed review round records a finding F-09. Open items: F-02."),
    ("F-01", DO, "F-01 was already checked closed in review-2.md. Open items: F-02."),
    ("F-04", DO, "F-04 was already deferred in review-2.md. Open items: F-02."),
    ("F-05", DO,
     "F-05 is a finding of the open round review-3.md; close the round with"
     " `specflo review done` first. Open items: F-02."),
    ("X-1", DO, "'X-1' is not a finding ID; one looks like F-01. Open items: F-02."),
    ("F-02", "", _DEFER_NEEDS_DO),
    ("F-02", "   ", _DEFER_NEEDS_DO),
    ("F-02", "One\nTwo", "A deferral's --do is one line: remove the line break."),
]


@pytest.mark.parametrize("finding_id, do, refusal", _DEFER_REFUSED)
def test_a_defer_of_anything_but_an_open_item_is_refused_and_files_no_follow_up(
    ledger, finding_id, do, refusal
):
    root, cfg, service, directory = ledger
    assert service.defer_finding(SLUG, "F-04", DO)[2] == "FU-01"
    before = _snapshot(root / "docs")

    with pytest.raises(SpecfloError) as refused:
        service.defer_finding(SLUG, finding_id, do)

    assert str(refused.value) == refusal
    assert _snapshot(root / "docs") == before


def test_a_defer_of_an_item_the_open_round_checked_is_refused(ledger):
    root, cfg, service, directory = ledger
    service.check_finding(SLUG, "F-02", "open")
    before = _snapshot(root / "docs")
    with pytest.raises(SpecfloError) as refused:
        service.defer_finding(SLUG, "F-02", DO)
    assert str(refused.value) == (
        "F-02 is checked in the open round review-3.md; close the round with"
        " `specflo review done` first. Open items: F-02, F-04."
    )
    assert _snapshot(root / "docs") == before


def test_a_defer_with_no_round_is_refused(tmp_path):
    cfg, directory = _project(tmp_path)
    before = _snapshot(tmp_path / "docs")
    with pytest.raises(SpecfloError) as refused:
        LocalProjectService(tmp_path, cfg).defer_finding(SLUG, "F-01", DO)
    assert str(refused.value) == (
        "No closed review round records a finding F-01. Open items: none."
    )
    assert _snapshot(tmp_path / "docs") == before


def test_a_finding_settles_once_whichever_way(ledger):
    root, cfg, service, directory = ledger
    service.reject_finding(SLUG, "F-04", REASON)
    service.defer_finding(SLUG, "F-02", DO)
    before = _snapshot(root / "docs")

    with pytest.raises(SpecfloError) as refused:
        service.defer_finding(SLUG, "F-04", DO)
    assert str(refused.value) == "F-04 was already rejected in review-2.md. Open items: none."
    with pytest.raises(SpecfloError) as refused:
        service.reject_finding(SLUG, "F-02", REASON)
    assert str(refused.value) == "F-02 was already deferred in review-1.md. Open items: none."
    assert _snapshot(root / "docs") == before


def test_a_follow_up_that_cannot_be_filed_refuses_the_defer_and_writes_no_line(single):
    root, cfg, service, directory = single
    (directory / "followup.md").write_text("# Follow-ups: thing\n\nHand-written.\n")
    before = _snapshot(root / "docs")

    with pytest.raises(SpecfloError) as refused:
        service.defer_finding(SLUG, "F-01", DO)

    assert str(refused.value) == "Malformed thing/followup: no '## Follow-ups' section."
    assert _snapshot(root / "docs") == before
    assert review.items_to_check(root, cfg, SLUG, 2) == ["F-01"]


# Why a daemon-held project's defer is refused: its follow-ups are not routed yet.
_HOSTED_DEFER = (
    "Deferring a finding files a follow-up, and follow-ups for a hosted project"
    " are not routed yet (FU-90). Fix the finding with a task that names it in"
    " --fixes, or reject it with `specflo review finding reject F-NN --reason <why>`."
)


@pytest.mark.parametrize("finding_id, do", [("F-01", DO), ("F-01", ""), ("F-09", DO)])
def test_a_hosted_service_refuses_every_defer_naming_the_follow_up_that_routes_it(
    single, finding_id, do
):
    root, cfg, service, directory = single
    before = _snapshot(root / "docs")

    with pytest.raises(SpecfloError) as refused:
        LocalProjectService(root, cfg, hosted=True).defer_finding(SLUG, finding_id, do)

    assert str(refused.value) == _HOSTED_DEFER
    assert _snapshot(root / "docs") == before


# --- the CLI, local and hosted ----------------------------------------------------------


_REJECT = ["review", "finding", "reject", "F-01", "--reason", REASON]
_DEFER = ["review", "finding", "defer", "F-01", "--do", DO]


def _round_one():
    """The pipeline through T-01 done and a round that asked for changes for
    its only item, should-fix F-01."""
    pipeline = _pipeline()
    done = next(i for i, (args, _) in enumerate(pipeline) if args[:2] == ["task", "done"])
    return [
        *pipeline[: done + 1],
        (["review", "start"], None),
        (["review", "finding", "add", "--severity", "should-fix", "--at", "src/app.py:9-12",
          "--text", "A message names the wrong command"], None),
        (["review", "done"], None),
    ]


def _steps(settle=_REJECT, shown=("review-1",)):
    """Round one, ``settle`` and the documents ``shown``, and the round after it."""
    return [
        *_round_one(),
        (settle, None),
        *[(["doc", "show", name], None) for name in shown],
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


def _cli_refusals(directory, refused=_CLI_REFUSED) -> list[tuple[int, str]]:
    """Each refused command's ``(exit code, output)``; no file changes on any."""
    outputs = []
    for args, _ in refused:
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
    assert "- F-01" not in out[("review", "prompt")][1].splitlines()  # no item to check
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


# Each refused command once a deferred F-01's next round is open, and what its
# refusal says.
_CLI_DEFER_REFUSED = [
    (["review", "finding", "defer", "F-01", "--do", "Again"],
     "F-01 was already deferred in review-1.md. Open items: none."),
    (["review", "finding", "defer", "F-01", "--do", ""], _DEFER_NEEDS_DO),
    (["review", "finding", "reject", "F-01", "--reason", "Not a problem"],
     "F-01 was already deferred in review-1.md. Open items: none."),
    (["review", "finding", "check", "F-01", "closed"],
     "F-01 was deferred in review-1.md. Items this round checks: none."),
    (["task", "add", "--text", "Fix it", "--acceptance", "fixed", "--verify", "uv run pytest",
      "--fixes", "F-01"],
     "F-01 was deferred in review-1.md. Open items a task can fix: none."),
]


def test_review_finding_defer_files_a_follow_up_and_settles_the_item(tmp_path, monkeypatch):
    results, directory = _local_steps(
        tmp_path, monkeypatch, _steps(_DEFER, ("review-1", "followup"))
    )
    out = _outputs(results)
    assert all(code == 0 for code, _ in out.values()), out

    assert out[tuple(_DEFER)][1] == (
        f"Deferred F-01 in {PARITY_SLUG}/review-1 to FU-01 in {PARITY_SLUG}/followup.\n"
    )
    assert out[("doc", "show", "review-1")][1].endswith(
        "## Verdict\n\n## Settled\n\n- F-01 deferred FU-01\n"
    )
    assert (
        "### FU-01 - A message names the wrong command\n"
        f"- Do: {DO}\n"
        "- From: review-1.md F-01\n"
        "- Status: open\n"
    ) in out[("doc", "show", "followup")][1]
    # No fix task: the next round opens, and it checks nothing.
    assert out[("review", "start")][1] == f"{PARITY_SLUG}/review-2\nScope: whole branch\n"
    assert "No earlier items to check this round." in out[("review", "prompt")][1]
    assert "- F-01" not in out[("review", "prompt")][1].splitlines()  # no item to check
    listed = runner.invoke(app, ["followup", "list"])
    assert "FU-01" in listed.output and "A message names the wrong command" in listed.output

    refusals = _cli_refusals(directory, _CLI_DEFER_REFUSED)
    for (args, refusal), (code, output) in zip(_CLI_DEFER_REFUSED, refusals):
        assert code == 1, (args, output)
        assert refusal in output, (args, output)


def test_review_finding_defer_json_names_the_round_and_the_follow_up(single, monkeypatch):
    root, cfg, service, directory = single
    projects.switch_project(root, cfg, SLUG)
    monkeypatch.chdir(root)
    result = runner.invoke(app, ["review", "finding", "defer", "F-01", "--do", DO, "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "id": "F-01", "followup": "FU-01", "do": DO, "locator": f"{SLUG}/review-1",
        "path": str(directory / "review-1.md"),
    }


def test_review_finding_defer_on_a_hosted_project_is_refused_naming_the_follow_up_that_routes_it(
    tmp_path, monkeypatch, live_daemon
):
    hosted, hosted_dir = _hosted_steps(tmp_path, monkeypatch, live_daemon, _round_one())
    assert all(code == 0 for code, _ in _outputs(hosted).values()), hosted
    checkout = tmp_path / "hosted"
    audit = live_daemon["root"] / "audit.jsonl"
    before = _snapshot(hosted_dir)
    audited = audit.read_text()

    for do in (DO, ""):
        result = runner.invoke(app, ["review", "finding", "defer", "F-01", "--do", do])
        assert result.exit_code == 1, result.output
        assert result.output == (
            f"error: Project {PARITY_SLUG!r} is hosted on remote 'home'; deferring a"
            " finding files a follow-up, and follow-ups for a hosted project are not"
            " routed yet (FU-90). Fix the finding with a task that names it in --fixes,"
            " or reject it with `specflo review finding reject F-NN --reason <why>`.\n"
        )
    # The CLI refuses before any request goes out: the daemon saw none.
    assert audit.read_text() == audited
    assert _snapshot(hosted_dir) == before
    assert list(checkout.rglob("followup.md")) == []
    assert list(live_daemon["root"].rglob("followup.md")) == []

    # A direct call is refused on the daemon, with no document changed.
    remote = RemoteProjectService(live_daemon["url"], live_daemon["token"])
    with pytest.raises(SpecfloError) as refused:
        remote.defer_finding(PARITY_SLUG, "F-01", DO)
    assert str(refused.value).endswith(_HOSTED_DEFER), str(refused.value)
    assert _snapshot(hosted_dir) == before
    assert list(live_daemon["root"].rglob("followup.md")) == []
    assert audit.read_text() == audited

    # The item stays open: a reject still settles it.
    rejected = runner.invoke(app, ["review", "finding", "reject", "F-01", "--reason", REASON])
    assert rejected.exit_code == 0, rejected.output
    assert (hosted_dir / "review-1.md").read_text().endswith(
        f"\n## Settled\n\n- F-01 rejected: {REASON}\n"
    )

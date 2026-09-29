"""The reviewer brief: one set of rules for every review round.

``specflo review prompt`` prints what the reviewer of the open round needs:
the scope, what each severity means, what is not a finding, how to record,
that the CLI sets the verdict, and which tests to run: the configured
test_command each round when one is set, else only the tests in scope. A
round with items to check lists the tasks that fix each one and the rules for
checking an item closed: the pin test fails at the latest reviewed round's sha
and passes on HEAD, and the defect is gone on every path that reaches it. A
round after one that settled something lists it under Already settled: each
nit, each deferred finding with its follow-up, each rejected finding with its
reason and each follow-up a reviewer recorded from an earlier round, with the
rule to raise one again only with new evidence that it is worse than recorded.
"""

import pytest
from typer.testing import CliRunner

from reviewhelp import fix_active_open_items
from specflo import config, followup, plan, projects, review, spec
from specflo.cli import app
from test_hosted_parity import _hosted_steps, _local_steps, _pipeline

runner = CliRunner()


def _project(tmp_path, monkeypatch):
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing", created="2026-08-22")
    projects.switch_project(tmp_path, cfg, "Thing")
    monkeypatch.chdir(tmp_path)
    return tmp_path / "docs" / "projects" / "thing"


def _prompt():
    result = runner.invoke(app, ["review", "prompt"])
    assert result.exit_code == 0, result.output
    return result.output


# What every brief carries, whatever the round's scope.
_ELEMENTS = (
    # the severities, with the rule for agent-facing wording
    "blocker", "should-fix", "nit", "agent-facing",
    # what is not a finding
    "did not introduce", "specflo followup add",
    # how to record
    "specflo review finding add", "specflo review finding check",
    "## Scope reviewed", "- none",
    # the verdict is the CLI's
    "specflo review done", "verdict",
    # targeted tests
    "only the tests",
)


def test_a_whole_branch_round_gets_the_whole_brief(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    runner.invoke(app, ["review", "start"])

    text = _prompt()

    assert "whole branch" in text
    assert "review-1.md" in text
    for element in _ELEMENTS:
        assert element in text, element


def test_a_delta_round_gets_its_range_and_every_item_to_check(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    (project_dir / "review-1.md").write_text(
        "---\nround: 1\nverdict: changes-requested\ndate: '2026-08-22'\nsha: 'abc1234'\n"
        "reason: ''\n---\n\n# Review round 1\n\n## Findings\n\n"
        "- F-01 (blocker) One\n- F-02 (should-fix) Two\n- F-03 (nit) Three\n"
    )
    fix_active_open_items()
    runner.invoke(app, ["review", "start"])

    text = _prompt()

    assert "abc1234..HEAD" in text
    assert "outside" in text                     # the range bounds what is a finding
    for item in ("F-01", "F-02"):
        assert item in text
    # The nit is no item to check: it is listed only as settled.
    assert "F-03" not in text.split("\n## Already settled\n", 1)[0]
    for element in _ELEMENTS:
        assert element in text, element


# The rules for checking an item closed, which only a round with items carries.
_FIX_RULES = (
    # the pin test proves the fix: it fails before it and passes after
    "fails on the source at", "passes on HEAD",
    # the defect is gone on every path, and a missed path is no new finding
    "every path", "keeps the item open", "not a new finding",
)

_ROUND_1 = (
    "---\nround: 1\nverdict: changes-requested\ndate: '2026-08-22'\nsha: 'abc1234'\n"
    "reason: ''\n---\n\n# Review round 1\n\n## Findings\n\n"
    "- F-01 (blocker) One\n- F-02 (should-fix) Two\n- F-03 (nit) Three\n"
)
_FIX_TITLE = "Keep the sha when the round closes"
_FIX_VERIFY = "uv run pytest tests/test_close.py -k sha"


def _item_entry(text, item):
    """The brief's entry for ``item``: its line and the indented lines under it."""
    lines = text.splitlines()
    start = lines.index(f"- {item}")
    entry = [lines[start]]
    for line in lines[start + 1:]:
        if not line.startswith("  "):
            break
        entry.append(line)
    return "\n".join(entry)


def _line_with(text, phrase):
    """The one line of ``text`` that holds ``phrase``."""
    (line,) = [line for line in text.splitlines() if phrase in line]
    return line


@pytest.mark.parametrize("start", [["review", "start"], ["review", "start", "--full"]])
def test_a_round_with_items_lists_their_fix_tasks_and_the_rules_to_close_them(
    tmp_path, monkeypatch, start
):
    project_dir = _project(tmp_path, monkeypatch)
    (project_dir / "review-1.md").write_text(_ROUND_1)
    # The branch's own work is T-01..T-04; T-05 fixes F-01.
    cfg = config.load_config(tmp_path)
    spec.start_spec(tmp_path, cfg, "thing")
    spec.add_requirement(tmp_path, cfg, "thing", "Closes rounds", acceptance="they close")
    plan.start_plan(tmp_path, cfg, "thing")
    for n in range(1, 5):
        plan.add_task(tmp_path, cfg, "thing", f"Work {n}", "done", "uv run pytest",
                      implements=["REQ-01"])
    fix = plan.add_task(tmp_path, cfg, "thing", _FIX_TITLE, "the sha is kept", _FIX_VERIFY,
                        implements=[], fixes=["F-01"])
    assert fix.id == "T-05"
    plan.start_task(tmp_path, cfg, "thing", fix.id)
    plan.done_task(tmp_path, cfg, "thing", fix.id)
    (other,) = fix_active_open_items()
    assert runner.invoke(app, start).exit_code == 0

    text = _prompt()

    first = _item_entry(text, "F-01")
    assert "T-05" in first and _FIX_TITLE in first and _FIX_VERIFY in first
    assert other not in first
    assert other in _item_entry(text, "F-02")
    for task_id in ("T-01", "T-02", "T-03", "T-04"):
        assert task_id not in text, task_id
    # The sha the pin test must fail at is the latest reviewed round's, in a
    # full round too, whose range starts nowhere.
    assert "abc1234" in _line_with(text, "fails on the source at")
    for rule in _FIX_RULES:
        assert rule in text, rule


@pytest.mark.parametrize("earlier", [None, "ready"])
def test_a_round_with_no_items_carries_no_fix_proof_rules(tmp_path, monkeypatch, earlier):
    project_dir = _project(tmp_path, monkeypatch)
    if earlier:
        (project_dir / "review-1.md").write_text(
            "---\nround: 1\nverdict: ready-to-merge\ndate: '2026-08-22'\nsha: 'abc1234'\n"
            "reason: ''\n---\n\n# Review round 1\n\n## Findings\n\n- F-01 (nit) One\n"
        )
    assert runner.invoke(app, ["review", "start"]).exit_code == 0

    text = _prompt()

    for rule in _FIX_RULES:
        assert rule not in text, rule
    assert "Verify" not in text
    assert "abc1234" not in text.replace("abc1234..HEAD", "")


# The Tests part as it reads with no test_command set; it must not change.
_TARGETED_TESTS = (
    "Run only the tests for the files in scope. The whole suite ran before the"
    " first round."
)
_TEST_COMMAND = "run-the-suite-sentinel"


def _tests_part(text):
    """The brief's Tests part: everything under ``## Tests``."""
    assert "\n## Tests\n" in text, text
    return text.split("\n## Tests\n", 1)[1]


def test_a_set_test_command_is_the_suite_the_reviewer_runs(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["config", "set", "test_command", _TEST_COMMAND]).exit_code == 0
    runner.invoke(app, ["review", "start"])

    tests = _tests_part(_prompt())

    assert f"`{_TEST_COMMAND}`" in tests
    assert "each round" in tests
    assert "Run only the tests for the files in scope" not in tests


def test_without_a_test_command_the_brief_keeps_the_targeted_tests(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    runner.invoke(app, ["review", "start"])

    tests = _tests_part(_prompt())

    assert " ".join(tests.split()) == _TARGETED_TESTS


def test_no_open_round_refuses_naming_review_start(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["review", "prompt"])

    assert result.exit_code != 0
    assert "specflo review start" in result.output


def test_the_prompt_is_read_only_on_the_daemon():
    from specflo.daemon import routes

    assert "review_prompt" in routes.READ_OPERATIONS


# --- what earlier rounds settled ------------------------------------------------------

_SETTLED_HEADER = "\n## Already settled\n"
_REVIEWER_TITLE = "Tidy the help text"
_REJECTED_WHY = "The caller names the ID itself"


def _settled_part(text):
    """The brief's Already settled section: everything under its heading up to
    the next one."""
    assert _SETTLED_HEADER in text, text
    return text.split(_SETTLED_HEADER, 1)[1].split("\n## ", 1)[0]


def _settled_rounds(tmp_path, monkeypatch):
    """Two closed rounds and the follow-ups filed from them; no round open.

    Round 1 asked for changes: blocker F-01, should-fix F-02, nit F-03,
    should-fix F-04, deferred to FU-120 once the round closed, and blocker
    F-05, rejected. Its reviewer recorded FU-118 from it, and its close filed
    FU-119 for its nits. Round 2 checked F-01 and F-02 closed and asked for
    changes for should-fix F-06, which is open. Another project's FU-117
    comes from a review-1.md of its own. Every open item has a done fix task.
    """
    project_dir = _project(tmp_path, monkeypatch)
    cfg = config.load_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Other", created="2026-08-22")
    projects.switch_project(tmp_path, cfg, "Thing")
    (project_dir.parent / "other" / "followup.md").write_text(
        "# Follow-ups: other\n\n## Follow-ups\n\n"
        "### FU-117 - Another project's note\n- Do: Look again\n"
        "- From: review-1.md\n- Status: open\n"
    )
    review.start_round(tmp_path, cfg, "thing", sha="abc1234")
    for severity, text, at in (
        ("blocker", "The close drops the sha", "src/app.py:3"),
        ("should-fix", "The lock is dropped early", "src/app.py:5"),
        ("nit", "A name reads oddly", None),
        ("should-fix", "A message names the wrong command", "src/app.py:9-12"),
        ("blocker", "The refusal loses the ID", "src/app.py:20"),
    ):
        review.add_finding(tmp_path, cfg, "thing", severity, text, at)
    reviewer = followup.add_followup(
        tmp_path, cfg, "thing", _REVIEWER_TITLE, "Reword the help", source="review-1.md"
    )
    review.close_round(tmp_path, cfg, "thing")
    deferred = review.defer_finding(tmp_path, cfg, "thing", "F-04", "Name the command")[2]
    review.reject_finding(tmp_path, cfg, "thing", "F-05", _REJECTED_WHY)
    nits = [e for e in followup.list_followups(tmp_path, cfg) if e.title.startswith("Nits")]
    assert (reviewer.id, [e.id for e in nits], deferred) == ("FU-118", ["FU-119"], "FU-120")
    fix_active_open_items()
    review.start_round(tmp_path, cfg, "thing", sha="def5678")
    review.check_finding(tmp_path, cfg, "thing", "F-01", "closed")
    review.check_finding(tmp_path, cfg, "thing", "F-02", "closed")
    review.add_finding(tmp_path, cfg, "thing", "should-fix", "A refusal names no ID",
                       "src/app.py:30")
    review.close_round(tmp_path, cfg, "thing")
    fix_active_open_items()
    return project_dir


@pytest.mark.parametrize("start", [
    ["review", "start", "--over-budget"], ["review", "start", "--full", "--over-budget"],
])
def test_a_later_round_lists_what_earlier_rounds_settled(tmp_path, monkeypatch, start):
    _settled_rounds(tmp_path, monkeypatch)
    started = runner.invoke(app, start)
    assert started.exit_code == 0, started.output

    text = _prompt()

    settled = _settled_part(text)
    assert "- F-03 (nit) A name reads oddly" in settled.splitlines()
    assert "FU-120" in _line_with(settled, "F-04")
    assert _REJECTED_WHY in _line_with(settled, "F-05")
    assert _REVIEWER_TITLE in _line_with(settled, "FU-118")
    assert "new evidence" in settled and "worse than recorded" in settled
    # Not an item checked closed, not an open item, not the round's nits
    # follow-up, not another project's follow-up.
    for absent in ("F-01", "F-02", "F-06", "FU-119", "FU-117"):
        assert absent not in settled, absent
    # A deferral's follow-up is listed through its finding, not a second time.
    assert settled.count("FU-120") == 1
    # The open item is still one to check, and no settled finding is.
    lines = text.splitlines()
    assert "- F-06" in lines
    for finding_id in ("F-03", "F-04", "F-05"):
        assert f"- {finding_id}" not in lines, finding_id


@pytest.mark.parametrize("earlier", [None, "closed"])
def test_a_round_with_nothing_settled_has_no_settled_section(tmp_path, monkeypatch, earlier):
    project_dir = _project(tmp_path, monkeypatch)
    if earlier:
        # Round 1's only item, checked closed by round 2: nothing is settled.
        (project_dir / "review-1.md").write_text(_ROUND_1.replace("- F-03 (nit) Three\n", ""))
        (project_dir / "review-2.md").write_text(
            "---\nround: 2\nverdict: ready-to-merge\ndate: '2026-08-23'\nsha: 'def5678'\n"
            "reason: ''\n---\n\n# Review round 2\n\n## Earlier findings\n\n- F-01 closed\n"
            "- F-02 closed\n\n## Findings\n\n- none\n"
        )
    assert runner.invoke(app, ["review", "start", "--over-budget"]).exit_code == 0

    text = _prompt()

    assert "Already settled" not in text
    assert "new evidence" not in text


def test_a_hosted_brief_lists_the_same_settled_nits_and_rejects(
    tmp_path, monkeypatch, live_daemon
):
    """A daemon files no follow-up, so a hosted round's nits stay in its file:
    the settled list holds them and the rejected finding all the same."""
    pipeline = _pipeline()
    done = next(i for i, (args, _) in enumerate(pipeline) if args[:2] == ["task", "done"])
    steps = [
        *pipeline[: done + 1],
        (["review", "start"], None),
        (["review", "finding", "add", "--severity", "should-fix", "--at", "src/app.py:9-12",
          "--text", "A message names the wrong command"], None),
        (["review", "finding", "add", "--severity", "nit", "--text", "A name reads oddly"], None),
        (["review", "done"], None),
        (["review", "finding", "reject", "F-01", "--reason", _REJECTED_WHY], None),
        (["review", "start"], None),
        (["review", "prompt"], None),
    ]
    briefs = []
    for run in (lambda: _local_steps(tmp_path, monkeypatch, steps),
                lambda: _hosted_steps(tmp_path, monkeypatch, live_daemon, steps)):
        results = run()[0]
        # Each review step passes; the pipeline's first validate is meant to fail.
        review_steps = results[done + 2:]
        assert all(code == 0 for _, code, _ in review_steps), review_steps
        briefs.append(_settled_part(results[-1][2]))
    local, hosted = briefs

    assert "- F-02 (nit) A name reads oddly" in hosted.splitlines()
    assert _REJECTED_WHY in _line_with(hosted, "F-01")
    assert hosted == local

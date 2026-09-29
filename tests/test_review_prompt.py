"""The reviewer brief: one set of rules for every gate round, and a harden round's own.

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

A harden round's brief keeps the scope, the items, the rules for checking an
item closed and what earlier rounds settled, and says: there is no verdict,
the round closes hardened; a blocker or should-fix needs evidence named in its
text; wording in agent-facing text and docs is never a finding unless it tells
an agent or user to do the wrong thing, which makes it a should-fix; its nits
stay in the round; and only the tests that reproduce a finding run, the whole
suite once when hardening stops. A gate round's brief says none of it.

A harden round in a harden project reviews the Scope its brief names, quoted
into the brief with its Focus, not the branch: a problem already present in
that scope is a finding, and one outside it goes to a follow-up.
"""

import pytest
from typer.testing import CliRunner

from reviewhelp import fix_active_open_items
from specflo import config, followup, plan, projects, review, spec
from specflo.cli import app
from test_hosted_parity import _hosted_steps, _local_steps, _pipeline
from test_level_harden import _checkout, _fill, _hosted_checkout, _ok

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


# --- a harden round's own brief -------------------------------------------------------

# The gate briefs word for word: a first round, and a delta round with an item,
# its fix task and what earlier rounds settled. A harden round's brief leaves
# them as they are.
_GATE_FIRST = (
    "# Reviewer brief: thing, review round 1 (review-1.md)\n"
    "\n"
    "Review the work and record what you find through the specflo CLI.\n"
    "\n"
    "## Scope\n"
    "\n"
    "This round reviews the whole branch: every change the branch makes.\n"
    "\n"
    "## Severity\n"
    "\n"
    "- blocker: wrong behaviour, a broken requirement, or data loss.\n"
    "- should-fix: a real problem to fix before merge, smaller than a blocker.\n"
    "- nit: style, naming, wording or docs polish. Wording in agent-facing text (skills,"
    " prompts, messages an agent reads) is a nit unless it tells the agent to do the"
    " wrong thing.\n"
    "\n"
    "A blocker or should-fix finding asks for changes. A nit never blocks: it goes to a"
    " follow-up when the round closes.\n"
    "\n"
    "## What is not a finding\n"
    "\n"
    "A problem the branch did not introduce is not a finding. Record it with `specflo"
    " followup add \"<title>\" --do \"<what to do>\" --from \"review-1.md\"` instead, so it"
    " never blocks this round.\n"
    "\n"
    "## How to record\n"
    "\n"
    "- Each finding: `specflo review finding add --severity blocker|should-fix|nit --at"
    " <file>:<line>[-<line>] --text \"<one line>\"`. `--at` names where the defect is, as"
    " the file is at the round's sha; a blocker or should-fix needs it, and a nit may"
    " leave it out.\n"
    "- No earlier items to check this round. A later round records each one with `specflo"
    " review finding check F-NN closed|open`.\n"
    "- One line under `## Scope reviewed` in review-1.md saying what you read.\n"
    "- A round with no findings: `- none` as the only line under `## Findings` in"
    " review-1.md.\n"
    "\n"
    "Do not choose a verdict. `specflo review done` derives it from what you recorded"
    " when the round closes.\n"
    "\n"
    "## Tests\n"
    "\n"
    "Run only the tests for the files in scope. The whole suite ran before the first"
    " round.\n"
)
_GATE_DELTA = (
    "# Reviewer brief: thing, review round 3 (review-3.md)\n"
    "\n"
    "Review the work and record what you find through the specflo CLI.\n"
    "\n"
    "## Scope\n"
    "\n"
    "This is a delta round: review only the changes in `def5678..HEAD` (`git diff"
    " def5678..HEAD`), the fixes made since the last reviewed round.\n"
    "\n"
    "Earlier rounds left these blocker and should-fix items. Check each one and record"
    " whether it is fixed. Under each item are the tasks that fix it:\n"
    "\n"
    "- F-06\n"
    "  - T-03 Fix F-06. Verify: `uv run pytest`\n"
    "\n"
    "## Checking an item closed\n"
    "\n"
    "Check an item closed only after its pin test, the test its fix task's verify step"
    " runs, fails on the source at `def5678`, the latest reviewed round's sha, and passes"
    " on HEAD. A pin test that passes before the fix proves nothing about the fix.\n"
    "\n"
    "Check an item closed only when the defect is gone on every path that reaches it, not"
    " only the path its finding names: search for the other code that reaches the same"
    " defect. A path the fix missed keeps the item open and is not a new finding: check"
    " the item open, and do not record the path with `specflo review finding add`.\n"
    "\n"
    "## Already settled\n"
    "\n"
    "Earlier rounds settled these, so they are not findings again. Raise one again only"
    " with new evidence that it is worse than recorded, and say in the finding's text"
    " what that evidence is.\n"
    "\n"
    "- F-03 (nit) A name reads oddly\n"
    "- F-04 (should-fix) [src/app.py:9-12] A message names the wrong command - deferred"
    " to FU-120\n"
    "- F-05 (blocker) [src/app.py:20] The refusal loses the ID - rejected: The caller"
    " names the ID itself\n"
    "- FU-118 Tidy the help text - a follow-up recorded from review-1.md\n"
    "\n"
    "## Severity\n"
    "\n"
    "- blocker: wrong behaviour, a broken requirement, or data loss.\n"
    "- should-fix: a real problem to fix before merge, smaller than a blocker.\n"
    "- nit: style, naming, wording or docs polish. Wording in agent-facing text (skills,"
    " prompts, messages an agent reads) is a nit unless it tells the agent to do the"
    " wrong thing.\n"
    "\n"
    "A blocker or should-fix finding asks for changes. A nit never blocks: it goes to a"
    " follow-up when the round closes.\n"
    "\n"
    "## What is not a finding\n"
    "\n"
    "A problem the branch did not introduce, or one outside `def5678..HEAD`, is not a"
    " finding. Record it with `specflo followup add \"<title>\" --do \"<what to do>\" --from"
    " \"review-3.md\"` instead, so it never blocks this round.\n"
    "\n"
    "## How to record\n"
    "\n"
    "- Each finding: `specflo review finding add --severity blocker|should-fix|nit --at"
    " <file>:<line>[-<line>] --text \"<one line>\"`. `--at` names where the defect is, as"
    " the file is at the round's sha; a blocker or should-fix needs it, and a nit may"
    " leave it out.\n"
    "- Each earlier item above: `specflo review finding check F-NN closed|open`.\n"
    "- One line under `## Scope reviewed` in review-3.md saying what you read.\n"
    "- A round with no findings: `- none` as the only line under `## Findings` in"
    " review-3.md.\n"
    "\n"
    "Do not choose a verdict. `specflo review done` derives it from what you recorded"
    " when the round closes.\n"
    "\n"
    "## Tests\n"
    "\n"
    "Run only the tests for the files in scope. The whole suite ran before the first"
    " round.\n"
)

# What only a harden round's brief says.
_HARDEN_RULES = (
    # there is no verdict: the round closes hardened
    "has no verdict", "as hardened",
    # a blocker or should-fix only with evidence, named in its text
    "only with evidence", "a repro, a failing test or a trace",
    # wording in agent-facing text and docs is no finding unless it misleads
    "payload and hint strings", "not even a nit", "an agent or user to do the wrong thing",
    # its nits stay in the round
    "no follow-up is filed",
    # only the tests that reproduce a finding; the suite runs when hardening stops
    "reproduce a finding", "when hardening stops",
)
_HARDEN_TESTS = (
    "Run only the tests needed to reproduce a finding. The whole suite runs once,"
    " when hardening stops."
)


def _part(text, header):
    """The brief's section under ``## header``, up to the next heading."""
    marker = f"\n## {header}\n"
    assert marker in text, text
    return text.split(marker, 1)[1].split("\n## ", 1)[0]


@pytest.mark.parametrize("earlier", [None, "settled"])
def test_a_harden_round_gets_its_own_brief(tmp_path, monkeypatch, earlier):
    if earlier:
        _settled_rounds(tmp_path, monkeypatch)
    else:
        _project(tmp_path, monkeypatch)
    started = runner.invoke(app, ["review", "start", "--harden"])
    assert started.exit_code == 0, started.output

    text = _prompt()

    # Its scope is the whole branch, and a problem the branch did not bring
    # is a follow-up, not a finding.
    assert "whole branch" in _part(text, "Scope")
    not_a_finding = _part(text, "What is not a finding")
    assert "did not introduce" in not_a_finding and "specflo followup add" in not_a_finding
    for rule in _HARDEN_RULES:
        assert rule in text, rule
    assert "should-fix" in _line_with(text, "not even a nit")
    # No verdict to derive, no follow-up for its nits, no suite each round.
    for gate_rule in ("derives it", "asks for changes", "goes to a follow-up", "files in scope"):
        assert gate_rule not in text, gate_rule
    if earlier:
        # It still checks each open item, and lists what earlier rounds settled.
        assert "- F-06" in text.splitlines()
        for rule in _FIX_RULES:
            assert rule in text, rule
        assert "- F-03 (nit) A name reads oddly" in _settled_part(text).splitlines()


@pytest.mark.parametrize("test_command", [None, _TEST_COMMAND])
def test_a_harden_round_runs_only_the_tests_that_reproduce_a_finding(
    tmp_path, monkeypatch, test_command
):
    _project(tmp_path, monkeypatch)
    if test_command:
        assert runner.invoke(app, ["config", "set", "test_command", test_command]).exit_code == 0
    assert runner.invoke(app, ["review", "start", "--harden"]).exit_code == 0

    tests = _tests_part(_prompt())

    assert " ".join(tests.split()) == _HARDEN_TESTS


@pytest.mark.parametrize("delta", [False, True])
def test_a_gate_round_keeps_its_brief_word_for_word(tmp_path, monkeypatch, delta):
    if delta:
        _settled_rounds(tmp_path, monkeypatch)
    else:
        _project(tmp_path, monkeypatch)
    start = ["review", "start", "--over-budget"] if delta else ["review", "start"]
    assert runner.invoke(app, start).exit_code == 0

    text = _prompt()

    assert text == (_GATE_DELTA if delta else _GATE_FIRST)
    for rule in _HARDEN_RULES:
        assert rule not in text, rule


# A harden round's brief in a normal project, word for word: its scope is the
# whole branch, and a problem the branch did not introduce is a follow-up.
_HARDEN_FIRST = (
    "# Reviewer brief: thing, review round 1 (review-1.md)\n"
    "\n"
    "Review the work and record what you find through the specflo CLI.\n"
    "\n"
    "## Scope\n"
    "\n"
    "This is a harden round. Review its whole scope, whatever earlier rounds read: the"
    " whole branch, every change the branch makes.\n"
    "\n"
    "## Severity\n"
    "\n"
    "- blocker: wrong behaviour, a broken requirement, or data loss.\n"
    "- should-fix: a real problem to fix before merge, smaller than a blocker.\n"
    "- nit: style or naming polish.\n"
    "\n"
    "Record a blocker or should-fix only with evidence, and name that evidence in the"
    " finding's text: a repro, a failing test or a trace.\n"
    "\n"
    "A blocker or should-fix finding becomes an item to fix, which a later round checks."
    " A nit stays listed in the round: no follow-up is filed for it.\n"
    "\n"
    "## What is not a finding\n"
    "\n"
    "A problem the branch did not introduce is not a finding. Record it with `specflo"
    " followup add \"<title>\" --do \"<what to do>\" --from \"review-1.md\"` instead, so it"
    " never becomes an item to fix.\n"
    "\n"
    "Wording in agent-facing text (skills, prompts, payload and hint strings) and in docs"
    " is never a finding, not even a nit, unless it tells an agent or user to do the"
    " wrong thing: then it is a should-fix.\n"
    "\n"
    "## How to record\n"
    "\n"
    "- Each finding: `specflo review finding add --severity blocker|should-fix|nit --at"
    " <file>:<line>[-<line>] --text \"<one line>\"`. `--at` names where the defect is, as"
    " the file is at the round's sha; a blocker or should-fix needs it, and a nit may"
    " leave it out.\n"
    "- No earlier items to check this round. A later round records each one with `specflo"
    " review finding check F-NN closed|open`.\n"
    "- One line under `## Scope reviewed` in review-1.md saying what you read.\n"
    "- A round with no findings: `- none` as the only line under `## Findings` in"
    " review-1.md.\n"
    "\n"
    "A harden round has no verdict. `specflo review done` closes it as hardened, whatever"
    " you recorded.\n"
    "\n"
    "## Tests\n"
    "\n"
    "Run only the tests needed to reproduce a finding. The whole suite runs once, when"
    " hardening stops.\n"
)


def test_a_harden_round_in_a_normal_project_keeps_its_brief_word_for_word(
    tmp_path, monkeypatch
):
    _project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["review", "start", "--harden"]).exit_code == 0

    assert _prompt() == _HARDEN_FIRST


# --- a harden round in a harden project -----------------------------------------------

# The harden brief's sections, with a comment the prompt leaves out.
_SCOPE = "- src/specflo/review.py <!-- left out of the prompt -->\n- src/specflo/brief.py\n"
_QUOTED_SCOPE = "> - src/specflo/review.py\n> - src/specflo/brief.py\n"
_FOCUS = "Error paths and refusals.\n"
_STOP = "Two quiet harden rounds in a row.\n"


def _harden_level(path, monkeypatch, focus=_FOCUS, hosted=None):
    """A harden project with a filled brief and an open harden round, in a
    checkout at ``path``, or on the ``hosted`` daemon when one is given."""
    if hosted:
        _hosted_checkout(path, monkeypatch, hosted)
    else:
        _checkout(path, monkeypatch)
    _ok(["new", "Thing", "--level", "harden", *(["--remote", "home"] if hosted else [])])
    _fill(scope=_SCOPE, focus=focus, stop=_STOP)
    _ok(["review", "start", "--harden"])


def test_a_harden_round_in_a_harden_project_reviews_the_scope_its_brief_names(
    tmp_path, monkeypatch
):
    _harden_level(tmp_path / "local", monkeypatch)

    text = _prompt()

    # Its scope is the brief's Scope, quoted with its comments left out, not
    # the branch, and a problem already present there is a finding.
    scope = _part(text, "Scope")
    assert _QUOTED_SCOPE in scope
    assert "already present in that scope is a finding" in scope
    assert "<!--" not in text and "left out of the prompt" not in text
    assert "branch" not in text
    # A problem outside that scope goes to a follow-up; none is outside for
    # want of a branch that brought it.
    not_a_finding = _part(text, "What is not a finding")
    assert "outside that scope is not a finding" in not_a_finding
    assert "specflo followup add" in not_a_finding
    assert "did not introduce" not in text
    # Every other harden rule stays, the wording rule among them.
    assert "not even a nit" in not_a_finding
    for rule in _HARDEN_RULES:
        assert rule in text, rule
    assert " ".join(_tests_part(text).split()) == _HARDEN_TESTS


@pytest.mark.parametrize("focus", [_FOCUS, "None.\n"])
def test_a_harden_project_brief_shows_its_focus_and_never_its_stop_when(
    tmp_path, monkeypatch, focus
):
    _harden_level(tmp_path / "local", monkeypatch, focus=focus)

    text = _prompt()

    if focus == _FOCUS:
        assert "hardest" in _line_with(text, "Focus")
        assert "> Error paths and refusals." in _part(text, "Scope").splitlines()
    else:
        assert "Focus" not in text and "hardest" not in text
    # Stop when tells the user when to stop opening rounds, not the reviewer
    # what to review.
    assert _STOP.strip() not in text and "Stop when" not in text


@pytest.mark.parametrize("scope", [None, "None.\n"])
def test_a_harden_round_in_a_harden_project_with_no_scope_refuses_naming_it(
    tmp_path, monkeypatch, scope
):
    _checkout(tmp_path / "local", monkeypatch)
    _ok(["new", "Thing", "--level", "harden"])
    if scope:
        _fill(scope=scope)
    _ok(["review", "start", "--harden"])

    result = runner.invoke(app, ["review", "prompt"])

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit), result.exception
    assert "names no Scope" in result.output
    assert "`specflo section set brief Scope --stdin`" in result.output


def test_a_gate_round_in_a_harden_project_keeps_the_gate_brief(tmp_path, monkeypatch):
    _checkout(tmp_path / "local", monkeypatch)
    _ok(["new", "Thing", "--level", "harden"])
    _fill(scope=_SCOPE, focus=_FOCUS, stop=_STOP)
    _ok(["review", "start"])

    assert _prompt() == _GATE_FIRST


def test_a_hosted_harden_project_gets_the_same_brief_as_a_local_one(
    tmp_path, monkeypatch, live_daemon
):
    briefs = {}
    for where, hosted in (("local", None), ("hosted", live_daemon)):
        _harden_level(tmp_path / where, monkeypatch, hosted=hosted)
        briefs[where] = _prompt()
    local, hosted = briefs["local"], briefs["hosted"]

    assert _QUOTED_SCOPE in _part(hosted, "Scope")
    assert "> Error paths and refusals." in _part(hosted, "Scope").splitlines()
    # Only where a problem outside the scope goes differs: a daemon records no
    # follow-up, so the reviewer names it in the reply.
    hosted_outside, local_outside = (
        _line_with(_part(text, "What is not a finding"), "outside that scope is not a finding")
        for text in (hosted, local)
    )
    assert "in your reply" in hosted_outside and "specflo followup add" not in hosted_outside
    assert hosted.replace(hosted_outside, local_outside) == local
    assert "did not introduce" not in hosted

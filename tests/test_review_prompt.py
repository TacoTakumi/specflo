"""The reviewer brief: one set of rules for every review round.

``specflo review prompt`` prints what the reviewer of the open round needs:
the scope, what each severity means, what is not a finding, how to record,
that the CLI sets the verdict, and which tests to run: the configured
test_command each round when one is set, else only the tests in scope. A
round with items to check lists the tasks that fix each one and the rules for
checking an item closed: the pin test fails at the latest reviewed round's sha
and passes on HEAD, and the defect is gone on every path that reaches it.
"""

import pytest
from typer.testing import CliRunner

from reviewhelp import fix_active_open_items
from specflo import config, plan, projects, spec
from specflo.cli import app

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
    assert "F-03" not in text
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

"""Round files written before a finding could name its location keep working.

The four round files of a finished project (suite-cost), with its project.md,
plan.md and spec.md, are copied into tests/fixtures/review_compat with only a
personal name in their prose replaced.
They read with the verdicts, findings and items they always had: a finding
line with no location parses as before, and a ``file:line`` in its prose stays
in its text. ``review start``, ``review prompt`` and ``validate execute`` run
on them, a later close never refuses or rewrites them for lacking a location
or a regression mark, and every copied round file stays byte-identical.
"""

import json
import re
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from specflo import config, markdown, projects, review
from specflo.cli import app
from specflo.errors import SpecfloError

runner = CliRunner()

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "review_compat"
SLUG = "suite-cost"
ROUND_FILES = ("review-1.md", "review-2.md", "review-3.md", "review-4.md")
FILES = ROUND_FILES + ("project.md", "plan.md", "spec.md")

# What each round recorded as it closed: verdict, sha and base.
RECORDED = {
    "review-1.md": ("changes-requested", "94f79fc", ""),
    "review-2.md": ("changes-requested", "b070437", "94f79fc"),
    "review-3.md": ("changes-requested", "2c12327", "b070437"),
    "review-4.md": ("waived", "bc68d18", "2c12327"),
}
# Each round's finding lines as the old format meant them: an ID and a
# severity, then the text. None names a location or carries a mark.
FINDINGS = {
    "review-1.md": [
        ("F-01", "should-fix"), ("F-02", "should-fix"), ("F-03", "nit"),
        ("F-04", "nit"), ("F-05", "nit"), ("F-06", "nit"),
    ],
    "review-2.md": [("F-07", "should-fix")],
    "review-3.md": [("F-08", "should-fix")],
    "review-4.md": [],
}
# Each round's checks of the items earlier rounds left.
CHECKS = {
    "review-1.md": [],
    "review-2.md": ["- F-01 closed", "- F-02 closed"],
    "review-3.md": ["- F-07 open"],
    "review-4.md": [],
}
# What the findings and checks of each reviewed round give today: its
# verdict, its findings per severity, and the earlier items it checked open.
DERIVED = {
    "review-1.md": ("changes-requested", {"blocker": 0, "should-fix": 2, "nit": 4}, []),
    "review-2.md": ("changes-requested", {"blocker": 0, "should-fix": 1, "nit": 0}, []),
    "review-3.md": ("changes-requested", {"blocker": 0, "should-fix": 1, "nit": 0}, ["F-07"]),
}
# The items round N checks: blocker and should-fix findings of the rounds
# before it that no reviewed round checked closed. The waived round checks
# nothing, so the next round still checks F-07 and F-08.
ITEMS = {
    1: [],
    2: ["F-01", "F-02"],
    3: ["F-07"],
    4: ["F-07", "F-08"],
    5: ["F-07", "F-08"],
}
WAIVE_REASON = (
    "User waived round 4. F-07 and F-08 are fixed in bc68d18: hosted write_checkpoint"
    " names no command. The daemon-root parity test reads doc show checkpoint, fails"
    " without the fix, and passes with it. The whole suite passes (3918 passed, 1"
    " skipped)."
)
# What a refusal for a location or a mark would name.
FORMAT_WORDS = ("location", "regression", "mark")


def _project(tmp_path, monkeypatch):
    """The fixture project, active in a checkout at ``tmp_path``; its directory.

    ``tmp_path`` is no git checkout, so a round opened here records no sha.
    """
    cfg = config.init_config(tmp_path)
    project_dir = tmp_path / "docs" / "projects" / SLUG
    project_dir.mkdir(parents=True)
    for name in FILES:
        shutil.copyfile(FIXTURE / name, project_dir / name)
    projects.switch_project(tmp_path, cfg, SLUG)
    monkeypatch.chdir(tmp_path)
    return project_dir


def _cfg(tmp_path):
    return config.load_config(tmp_path)


def _section(path, header):
    """The non-blank lines under ``header`` in the file at ``path``."""
    body = markdown.section_body(path.read_text(), header) or ""
    return [line.strip() for line in body.splitlines() if line.strip()]


def _assert_rounds_unchanged(project_dir):
    for name in ROUND_FILES:
        assert (project_dir / name).read_bytes() == (FIXTURE / name).read_bytes(), name


def _fix_open_items():
    """Add, start and finish a task fixing each open item through the CLI; the tasks' IDs.

    The plan has milestones, so each task joins the last one and the plan
    still validates.
    """
    milestone = re.findall(r"^### (M-\d+) ", (FIXTURE / "plan.md").read_text(), re.MULTILINE)[-1]
    added = []
    for item in ("F-07", "F-08"):
        result = runner.invoke(app, [
            "task", "add", "--text", f"Fix {item}", "--acceptance", f"{item} is fixed",
            "--verify", "uv run pytest", "--fixes", item, "--milestone", milestone, "--json",
        ])
        assert result.exit_code == 0, result.output
        task = json.loads(result.stdout)
        assert task["fixes"] == [item]
        for verb in ("start", "done"):
            moved = runner.invoke(app, ["task", verb, task["id"]])
            assert moved.exit_code == 0, moved.output
        added.append(task["id"])
    return added


def _start_round():
    """``review start --json`` once each open item has a done fix task; its payload."""
    _fix_open_items()
    result = runner.invoke(app, ["review", "start", "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


# --- the readings -------------------------------------------------------------


def test_the_fixture_is_the_old_round_files():
    """No finding line of the fixture names a location or carries a mark."""
    for name in ROUND_FILES:
        for line in _section(FIXTURE / name, review.FINDINGS_HEADER):
            assert re.match(r"^- F-\d\d \((blocker|should-fix|nit)\) [^\[]", line), line


def test_each_round_reads_the_verdict_sha_and_base_it_closed_with(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)

    for name, (verdict, sha, base) in RECORDED.items():
        fields = review.frontmatter(project_dir / name)
        assert (fields["verdict"], fields["sha"], fields["base"], fields["level"]) == (
            verdict, sha, base, "full"
        ), name
    assert review.frontmatter(project_dir / "review-4.md")["reason"] == WAIVE_REASON
    assert [n for n, _ in review.round_files(tmp_path, _cfg(tmp_path), SLUG)] == [1, 2, 3, 4]


def test_each_round_has_no_kind_and_reads_as_a_gate_round(tmp_path, monkeypatch):
    """The rounds predate harden rounds, so none names a kind."""
    project_dir = _project(tmp_path, monkeypatch)

    for name in ROUND_FILES:
        fields = review.frontmatter(project_dir / name)
        assert "kind" not in fields, name
        assert review.round_kind(fields) == review.GATE, name
    _assert_rounds_unchanged(project_dir)


def test_a_finding_line_with_no_location_parses_as_before(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)

    for name, expected in FINDINGS.items():
        lines = _section(project_dir / name, review.FINDINGS_HEADER)
        parsed = [review.parse_finding_line(line) for line in lines]
        assert [(f.id, f.severity) for f in parsed] == expected, name
        for line, finding in zip(lines, parsed):
            assert finding.text == line.split(") ", 1)[1]
            assert (finding.path, finding.start, finding.end) == (None, None, None)
            assert finding.location is None
            assert finding.regression is False
            assert review.render_finding_line(finding) == line


def test_a_file_and_line_in_the_prose_stays_in_the_text(tmp_path, monkeypatch):
    """An old line opens its text with where the defect is, with no brackets:
    that is text, not a location."""
    project_dir = _project(tmp_path, monkeypatch)

    first, second = (
        review.parse_finding_line(line)
        for line in _section(project_dir / "review-1.md", review.FINDINGS_HEADER)[:2]
    )
    assert first.text.startswith(
        "src/specflo/status.py:61 and src/specflo/checkpoint.py:112 read test_command"
    )
    assert second.text.startswith("tests/pool/test_poolstore.py:484-504 still names")
    assert first.location is None and second.location is None


def test_each_round_checks_the_items_it_checked(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)

    for name, checks in CHECKS.items():
        assert _section(project_dir / name, review.EARLIER_HEADER) == checks, name


def test_the_findings_and_checks_give_the_verdict_each_round_recorded(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    cfg = _cfg(tmp_path)

    for number, name in enumerate(DERIVED, start=1):
        path = project_dir / name
        body = review.body_of(path)
        findings = review.parse_findings(tmp_path, cfg, SLUG, path, body)
        still_open = review.parse_checks(tmp_path, cfg, SLUG, path, body, number)
        verdict, counts = review.derive_verdict(findings, still_open)
        assert (verdict, counts, still_open) == DERIVED[name], name
        assert verdict == RECORDED[name][0]
    _assert_rounds_unchanged(project_dir)


def test_no_round_counts_a_regression(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)

    counts = {name: review.regression_count(project_dir / name) for name in ROUND_FILES}
    assert counts == {
        "review-1.md": 0, "review-2.md": 0, "review-3.md": 0, "review-4.md": None
    }


def test_the_ledger_leaves_the_items_it_always_left(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    cfg = _cfg(tmp_path)

    for number, items in ITEMS.items():
        assert review.items_to_check(tmp_path, cfg, SLUG, number) == items, number
    assert review.check_fixes(tmp_path, cfg, SLUG, ["F-07", "F-08"]) == ["F-07", "F-08"]
    for finding_id, why in (
        ("F-01", "F-01 was already checked closed in review-2.md."),
        ("F-02", "F-02 was already checked closed in review-2.md."),
        ("F-03", "F-03 is a nit"),
        ("F-09", "No closed review round records a finding F-09."),
    ):
        with pytest.raises(SpecfloError) as refused:
            review.check_fixes(tmp_path, cfg, SLUG, [finding_id])
        assert why in str(refused.value)
        assert "Open items a task can fix: F-07, F-08." in str(refused.value)


def test_the_state_the_budget_and_the_gate_read_as_before(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    cfg = _cfg(tmp_path)

    assert review.review_state(tmp_path, cfg, SLUG) == {
        "rounds": 4,
        "latest": 4,
        "verdict": "waived",
        "open": False,
        "passing": True,
        "date": "2026-09-28",
        "sha": "bc68d18",
        "reason": WAIVE_REASON,
        "file": "review-4.md",
        "regressions": None,
        "open_items": ["F-07", "F-08"],
        "budget_spent": False,
        "after_changes": True,
    }
    # Four full-level rounds against the default budget of two; the latest
    # was waived, not changes-requested, so the budget is not spent.
    assert review.budget(tmp_path, cfg, SLUG) == {
        "level": "full", "used": 4, "max": 2, "spent": False, "regressions": None,
    }
    assert review.completion_issues(tmp_path, cfg, SLUG) == []
    assert review.open_round(tmp_path, cfg, SLUG) is None
    # The plan has no fix task: it was written before a task could name one.
    assert review.unfixed_items(tmp_path, cfg, SLUG) == {"F-07": [], "F-08": []}
    _assert_rounds_unchanged(project_dir)


# --- the commands -------------------------------------------------------------


def test_validate_execute_passes_on_the_old_rounds(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["validate", "execute", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"ready": True, "issues": [], "notes": []}
    text = runner.invoke(app, ["validate", "execute"])
    assert text.exit_code == 0, text.output
    assert text.output == "ok - execute is ready.\n"
    _assert_rounds_unchanged(project_dir)


def test_review_start_is_refused_only_for_the_open_items_with_no_fix_task(
    tmp_path, monkeypatch
):
    """The plan predates fix tasks, so each open item has none. The refusal
    names the items and nothing about a location or a mark, and writes no file."""
    project_dir = _project(tmp_path, monkeypatch)

    for args in ([], ["--over-budget"], ["--full"]):
        result = runner.invoke(app, ["review", "start", *args])
        assert result.exit_code == 1, result.output
        assert (
            "No review round opens while an open item has no done fix task:"
            " F-07 (no fix task), F-08 (no fix task)."
        ) in result.output
        assert not any(word in result.output for word in FORMAT_WORDS), result.output
    assert not (project_dir / "review-5.md").exists()
    _assert_rounds_unchanged(project_dir)


def test_review_start_and_prompt_run_once_each_open_item_has_a_done_fix_task(
    tmp_path, monkeypatch
):
    project_dir = _project(tmp_path, monkeypatch)
    cfg = _cfg(tmp_path)

    started = _start_round()

    assert started["created"] is True
    assert started["path"].endswith("review-5.md")
    # A delta round from the latest reviewed round: the waived one reviewed nothing.
    assert (started["scope"], started["range"], started["items"]) == (
        "delta", "2c12327..HEAD", ["F-07", "F-08"]
    )
    scope = review.review_scope(tmp_path, cfg, SLUG)
    assert (scope["round"], scope["base"], scope["first_reviewed_sha"]) == (5, "2c12327", "94f79fc")

    prompt = runner.invoke(app, ["review", "prompt"])

    assert prompt.exit_code == 0, prompt.output
    lines = prompt.output.splitlines()
    assert lines[0] == "# Reviewer brief: suite-cost, review round 5 (review-5.md)"
    assert "review only the changes in `2c12327..HEAD`" in prompt.output
    assert "- F-07" in lines and "- F-08" in lines
    assert "  - No task fixes it." not in lines
    assert "at `2c12327`, the latest reviewed round's sha," in prompt.output
    _assert_rounds_unchanged(project_dir)


def test_validate_execute_names_only_the_open_round_after_start(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    _start_round()

    result = runner.invoke(app, ["validate", "execute", "--json"])

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout) == {
        "ready": False,
        "issues": [
            "review round 5 is still open (review-5.md): close it with `specflo review done`."
        ],
        "notes": [],
    }
    _assert_rounds_unchanged(project_dir)


def test_a_later_close_neither_refuses_nor_rewrites_the_old_rounds(tmp_path, monkeypatch):
    """The old should-fix lines name no location; closing the next round reads
    them and leaves them as they are."""
    project_dir = _project(tmp_path, monkeypatch)
    _start_round()
    for item in ("F-07", "F-08"):
        checked = runner.invoke(app, ["review", "finding", "check", item, "closed"])
        assert checked.exit_code == 0, checked.output
    path = project_dir / "review-5.md"
    path.write_text(
        markdown.replace_section_body(path.read_text(), review.FINDINGS_HEADER, "- none")
    )

    closed = runner.invoke(app, ["review", "done", "--json"])

    assert closed.exit_code == 0, closed.output
    payload = json.loads(closed.stdout)
    assert (payload["verdict"], payload["still_open"], payload["regressions"]) == (
        "ready-to-merge", [], 0
    )
    validated = runner.invoke(app, ["validate", "execute", "--json"])
    assert validated.exit_code == 0, validated.output
    assert json.loads(validated.stdout)["ready"] is True
    _assert_rounds_unchanged(project_dir)

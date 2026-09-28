"""Regression marks: a finding on a line a fix changed.

A blocker or should-fix finding that ``review finding add`` records is marked
``(severity, regression)`` when a line of its location, blamed at the round's
sha, was last changed by a commit after the sha of the project's first
reviewed round: the earliest closed round that was not waived. "After" is
"not contained in": the blamed commit is not that sha or an ancestor of it.

The CLI blames in the caller's checkout, where the code lives, and sends the
mark with the add; the review scope carries the first reviewed round's sha to
it. The service takes the mark as data and runs no git. A finding with no
reviewed round before it is never marked, a nit never is, and a mark the
checkout cannot compute is left off with a note.

``review done`` applies the same rules to lines written by hand, in the round
file or in a report ingested with ``--file``: a blocker or should-fix line
with no location refuses the close, and the CLI marks a located line that
carries no mark yet, sending the IDs with the close. A mark is never removed.
"""

import json
import re
import subprocess

import httpx
import pytest
from typer.testing import CliRunner

from reviewhelp import fix_active_open_items, fix_by_cli, review_done
from specflo import config, daemon, markdown, projects, review
from specflo.cli import app
from specflo.errors import SpecfloError
from specflo.service import LocalProjectService
from test_review_location import _commit, _git

runner = CliRunner()

SLUG = "thing"
# Forty lines, committed as src/x.py.
X_PY = "".join(f"line {n}\n" for n in range(1, 41))


def _change(checkout, line, text):
    """Commit src/x.py with ``line`` rewritten as ``text``; the commit's short sha."""
    lines = (checkout / "src" / "x.py").read_text().splitlines(keepends=True)
    lines[line - 1] = f"{text}\n"
    return _commit(checkout, "src/x.py", "".join(lines))


def _project(tmp_path, monkeypatch):
    """An active 'Thing' in a git checkout at ``tmp_path`` whose first commit holds src/x.py."""
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, "src/x.py", X_PY)
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing", created="2026-09-01")
    projects.switch_project(tmp_path, cfg, "Thing")
    monkeypatch.chdir(tmp_path)
    return tmp_path / "docs" / "projects" / SLUG


def _run(*args):
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return result


def _add(severity, text, at=None):
    args = ["review", "finding", "add", "--severity", severity, "--text", text]
    return runner.invoke(app, args + (["--at", at] if at is not None else []))


def _start(*args):
    # A round opens only once each open item has a done fix task.
    fix_active_open_items()
    return _run("review", "start", *args)


def _close_clean():
    """Close the open round ready-to-merge, every earlier item checked closed."""
    result = review_done(runner, app)
    assert result.exit_code == 0, result.output


def _findings(path):
    return markdown.section_body(path.read_text(), review.FINDINGS_HEADER).strip().splitlines()


def _second_round(tmp_path, monkeypatch):
    """Round 1 closed at A asking for changes, commit B changing line 10, round 2 open at B.

    A rewrote line 20 after the first commit, and round 1 found problems on
    lines 20 and 10. Returns the project directory, A and B.
    """
    project_dir = _project(tmp_path, monkeypatch)
    a = _change(tmp_path, 20, "line 20 in A")
    _run("review", "start")
    assert _add("blocker", "One", "src/x.py:20").exit_code == 0
    assert _add("should-fix", "Two", "src/x.py:10").exit_code == 0
    _run("review", "done")
    b = _change(tmp_path, 10, "line 10 fixed in B")
    _start()
    return project_dir, a, b


# --- round 2 marks a finding on a line changed after round 1 -----------------


@pytest.mark.parametrize("at", ["src/x.py:10", "src/x.py:9-10", "src/x.py:10-12"])
@pytest.mark.parametrize("severity", ["blocker", "should-fix"])
def test_a_finding_on_a_line_a_fix_changed_is_marked(tmp_path, monkeypatch, severity, at):
    project_dir, _, _ = _second_round(tmp_path, monkeypatch)

    result = _add(severity, "The fix broke it", at)

    assert result.exit_code == 0, result.output
    assert result.stdout == "Recorded F-03 in thing/review-2.\n"
    assert result.stderr == ""
    line = _findings(project_dir / "review-2.md")[0]
    assert line == f"- F-03 ({severity}, regression) [{at}] The fix broke it"
    assert review.parse_finding_line(line).regression


@pytest.mark.parametrize(
    "at",
    [
        "src/x.py:30",     # never changed
        "src/x.py:1-9",    # the lines before the one B changed
        "src/x.py:20",     # changed by A, the first reviewed round's own sha
        "src/x.py:19-21",
    ],
)
def test_a_finding_on_lines_no_later_commit_changed_is_not_marked(tmp_path, monkeypatch, at):
    project_dir, _, _ = _second_round(tmp_path, monkeypatch)

    result = _add("blocker", "Not a regression", at)

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    assert _findings(project_dir / "review-2.md") == [f"- F-03 (blocker) [{at}] Not a regression"]


def test_a_nit_is_never_marked(tmp_path, monkeypatch):
    project_dir, _, _ = _second_round(tmp_path, monkeypatch)

    result = _add("nit", "A name reads oddly", "src/x.py:10")

    assert result.exit_code == 0, result.output
    assert _findings(project_dir / "review-2.md") == ["- F-03 (nit) [src/x.py:10] A name reads oddly"]


def test_no_finding_of_the_first_round_is_marked(tmp_path, monkeypatch):
    # Line 20 was changed by A, the commit round 1 opened at, after the first
    # commit: with no reviewed round before round 1 nothing is a regression.
    project_dir, _, _ = _second_round(tmp_path, monkeypatch)
    _add("blocker", "The fix broke it", "src/x.py:10")

    assert _findings(project_dir / "review-1.md") == [
        "- F-01 (blocker) [src/x.py:20] One",
        "- F-02 (should-fix) [src/x.py:10] Two",
    ]


def test_the_line_is_blamed_at_the_rounds_sha_not_at_head(tmp_path, monkeypatch):
    project_dir, _, _ = _second_round(tmp_path, monkeypatch)
    # Line 30 changes after round 2 opened; the reviewer read it as it was.
    _change(tmp_path, 30, "line 30 after the round opened")

    result = _add("blocker", "Read at the round's sha", "src/x.py:30")

    assert result.exit_code == 0, result.output
    assert _findings(project_dir / "review-2.md") == [
        "- F-03 (blocker) [src/x.py:30] Read at the round's sha"
    ]


# --- the first reviewed round: the earliest closed round not waived ----------


def test_a_waived_round_is_not_the_first_reviewed_round(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    _run("review", "waive", "--reason", "Reviewed by hand")
    _change(tmp_path, 10, "line 10 in B")
    _change(tmp_path, 20, "line 20 in C")
    _start()
    # No round before round 2 was reviewed: nothing in it is a regression.
    assert _add("blocker", "Round 2 on B's line", "src/x.py:10").exit_code == 0
    assert _findings(project_dir / "review-2.md") == ["- F-01 (blocker) [src/x.py:10] Round 2 on B's line"]
    _run("review", "done")
    _change(tmp_path, 30, "line 30 in D")
    # The waive counts toward the level's two rounds.
    _start("--over-budget")

    results = [
        _add("blocker", "B's line", "src/x.py:10"),
        _add("blocker", "C's line", "src/x.py:20"),
        _add("blocker", "D's line", "src/x.py:30"),
    ]

    assert [r.exit_code for r in results] == [0, 0, 0]
    assert _findings(project_dir / "review-3.md") == [
        "- F-02 (blocker) [src/x.py:10] B's line",
        "- F-03 (blocker) [src/x.py:20] C's line",
        "- F-04 (blocker, regression) [src/x.py:30] D's line",
    ]


def test_the_first_reviewed_round_counts_not_the_latest(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    _run("review", "start")
    _close_clean()
    _change(tmp_path, 10, "line 10 in B")
    _start()
    _close_clean()
    _change(tmp_path, 30, "line 30 in C")
    _start()

    result = _add("should-fix", "B's line, reviewed in round 2", "src/x.py:10")

    assert result.exit_code == 0, result.output
    assert _findings(project_dir / "review-3.md") == [
        "- F-01 (should-fix, regression) [src/x.py:10] B's line, reviewed in round 2"
    ]


# --- the scope carries the first reviewed round's sha ------------------------


def test_the_scope_carries_the_first_reviewed_rounds_sha(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    svc = LocalProjectService(tmp_path, config.load_config(tmp_path))
    _run("review", "waive", "--reason", "Reviewed by hand")
    _run("review", "start")
    first = review.frontmatter(project_dir / "review-2.md")["sha"]

    assert svc.review_scope(SLUG)["first_reviewed_sha"] is None
    _close_clean()
    _change(tmp_path, 10, "line 10 in B")
    _start()
    assert svc.review_scope(SLUG)["first_reviewed_sha"] == first != ""
    _close_clean()
    _change(tmp_path, 20, "line 20 in C")
    _start()
    assert svc.review_scope(SLUG)["first_reviewed_sha"] == first


def test_a_first_round_scope_has_no_first_reviewed_sha(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    _run("review", "start")
    svc = LocalProjectService(tmp_path, config.load_config(tmp_path))

    assert svc.review_scope(SLUG)["first_reviewed_sha"] is None


# --- a mark the checkout cannot compute --------------------------------------


def _set_sha(path, sha):
    path.write_text(re.sub(r"(?m)^sha: .*$", f"sha: '{sha}'", path.read_text(), count=1))
    assert review.frontmatter(path)["sha"] == sha


@pytest.mark.parametrize(
    "sha, why",
    [
        # A hosted round reviewed by someone else, at a commit not fetched here.
        ("deadbee", "this checkout has no commit deadbee"),
        ("", "the first reviewed round records no sha"),
        # A hand-edited sha never reaches git's argv.
        ("--batch", "the first reviewed round's sha '--batch' is not a commit id"),
    ],
)
def test_a_first_reviewed_sha_the_checkout_cannot_use_leaves_the_mark_off_with_a_note(
    tmp_path, monkeypatch, sha, why
):
    project_dir, _, _ = _second_round(tmp_path, monkeypatch)
    _set_sha(project_dir / "review-1.md", sha)

    result = _add("blocker", "The fix broke it", "src/x.py:10")

    assert result.exit_code == 0, result.output
    assert result.stdout == "Recorded F-03 in thing/review-2.\n"
    assert result.stderr == f"note: --at src/x.py:10 was not checked for a regression: {why}.\n"
    assert _findings(project_dir / "review-2.md") == ["- F-03 (blocker) [src/x.py:10] The fix broke it"]


def test_a_round_sha_the_checkout_lacks_leaves_the_mark_off_with_a_note(tmp_path, monkeypatch):
    project_dir, _, _ = _second_round(tmp_path, monkeypatch)
    _set_sha(project_dir / "review-2.md", "deadbee")

    result = _add("blocker", "The fix broke it", "src/x.py:10")

    assert result.exit_code == 0, result.output
    assert "note: --at src/x.py:10 was not checked: this checkout has no commit deadbee" in result.stderr
    assert (
        "note: --at src/x.py:10 was not checked for a regression: this checkout has no commit deadbee"
        in result.stderr
    )
    assert _findings(project_dir / "review-2.md") == ["- F-03 (blocker) [src/x.py:10] The fix broke it"]


def test_no_git_leaves_the_mark_off_with_a_note(tmp_path, monkeypatch):
    project_dir, _, _ = _second_round(tmp_path, monkeypatch)
    empty = tmp_path / "no-git"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    result = _add("blocker", "The fix broke it", "src/x.py:10")

    assert result.exit_code == 0, result.output
    assert "was not checked for a regression: git cannot" in result.stderr
    assert _findings(project_dir / "review-2.md") == ["- F-03 (blocker) [src/x.py:10] The fix broke it"]


def test_a_round_with_no_reviewed_round_before_it_gets_no_regression_note(tmp_path, monkeypatch):
    # Nothing to compute, so nothing to say: the location note stands alone.
    project_dir = _project(tmp_path, monkeypatch)
    _run("review", "start")
    _set_sha(project_dir / "review-1.md", "deadbee")

    result = _add("blocker", "One", "src/x.py:10")

    assert result.exit_code == 0, result.output
    assert result.stderr == "note: --at src/x.py:10 was not checked: this checkout has no commit deadbee.\n"


def test_the_mark_helper_runs_no_git_where_nothing_can_be_marked(tmp_path, monkeypatch):
    def no_git(*args, **kwargs):
        raise AssertionError(f"ran {args[0] if args else kwargs}")

    monkeypatch.setattr(subprocess, "run", no_git)

    assert review.regression_mark(tmp_path, "abc1234", None, "blocker", "src/x.py", 10, None) == (False, None)
    assert review.regression_mark(tmp_path, "abc1234", "abc1234", "nit", "src/x.py", 10, None) == (False, None)
    assert review.regression_mark(tmp_path, "abc1234", "abc1234", "blocker", None, None, None) == (False, None)


# --- the service takes the mark as data ------------------------------------------


def test_the_service_writes_the_mark_it_is_sent_and_runs_no_git(tmp_path, monkeypatch):
    project_dir, _, _ = _second_round(tmp_path, monkeypatch)
    svc = LocalProjectService(tmp_path, config.load_config(tmp_path))

    def no_git(*args, **kwargs):
        raise AssertionError(f"the service ran {args[0] if args else kwargs}")

    monkeypatch.setattr(subprocess, "run", no_git)
    # Line 30 was never changed: the service does not look.
    finding_id, _ = svc.add_finding(SLUG, "blocker", "One", location="src/x.py:30", regression=True)

    assert finding_id == "F-03"
    assert _findings(project_dir / "review-2.md") == ["- F-03 (blocker, regression) [src/x.py:30] One"]


def test_the_service_refuses_a_marked_nit(tmp_path, monkeypatch):
    project_dir, _, _ = _second_round(tmp_path, monkeypatch)
    svc = LocalProjectService(tmp_path, config.load_config(tmp_path))
    before = (project_dir / "review-2.md").read_bytes()

    with pytest.raises(SpecfloError, match="A nit is never marked as a regression"):
        svc.add_finding(SLUG, "nit", "One", location="src/x.py:10", regression=True)

    assert (project_dir / "review-2.md").read_bytes() == before


def test_the_service_refuses_a_mark_with_no_reviewed_round_before(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    _run("review", "waive", "--reason", "Reviewed by hand")
    _run("review", "start")
    svc = LocalProjectService(tmp_path, config.load_config(tmp_path))
    before = (project_dir / "review-2.md").read_bytes()

    with pytest.raises(SpecfloError, match="no round before review-2.md was reviewed"):
        svc.add_finding(SLUG, "blocker", "One", location="src/x.py:10", regression=True)

    assert (project_dir / "review-2.md").read_bytes() == before


# --- a hosted project, as a local one ----------------------------------------


def _regression_run(tmp_path, monkeypatch, name, new_args, register=None):
    """Two rounds in a fresh git checkout ``name``, with a fix between them;
    each step's ``(args, exit code, stdout, stderr)`` and the add_finding
    request bodies."""
    checkout = tmp_path / name
    checkout.mkdir()
    _git(checkout, "init", "-q")
    _commit(checkout, "src/x.py", X_PY)
    config.init_config(checkout)
    monkeypatch.chdir(checkout)
    if register is not None:
        registered = runner.invoke(app, ["remote", "add", "home", *register])
        assert registered.exit_code == 0, registered.output
    created = runner.invoke(app, ["new", "Thing", "--summary", "One line", *new_args])
    assert created.exit_code == 0, created.output
    sent = []
    real_send = httpx.Client.send

    def recording_send(self, request, **kwargs):
        if request.url.path.endswith("/add_finding"):
            sent.append(json.loads(request.content))
        return real_send(self, request, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", recording_send)
    add = ["review", "finding", "add", "--severity"]
    results = []

    def step(*args):
        result = runner.invoke(app, list(args))
        results.append((args, result.exit_code, result.stdout, result.stderr))

    step("review", "start")
    step(*add, "should-fix", "--at", "src/x.py:10", "--text", "Two")
    step("review", "done")
    _change(checkout, 10, "line 10 fixed")
    fix_by_cli(runner, app, "F-01")
    step("review", "start")
    step(*add, "blocker", "--at", "src/x.py:10", "--text", "The fix broke it")
    step(*add, "should-fix", "--at", "src/x.py:9-10", "--text", "And here")
    step(*add, "should-fix", "--at", "src/x.py:30", "--text", "Not a regression")
    step(*add, "nit", "--at", "src/x.py:10", "--text", "A name reads oddly")
    step("doc", "show", "review-2")
    monkeypatch.setattr(httpx.Client, "send", real_send)
    return results, sent


def test_a_regression_mark_is_the_same_for_a_local_and_a_hosted_project(
    tmp_path, monkeypatch, live_daemon
):
    local, _ = _regression_run(tmp_path, monkeypatch, "local", [])
    hosted, sent = _regression_run(
        tmp_path, monkeypatch, "hosted", ["--remote", "home"],
        register=[live_daemon["url"], "--token", live_daemon["token"]],
    )

    for mine, theirs in zip(local, hosted, strict=True):
        assert mine == theirs, f"{' '.join(mine[0])}:\nlocal: {mine[1:]}\nhosted: {theirs[1:]}"
    assert [code for _, code, _, _ in local] == [0] * len(local)
    local_round = tmp_path / "local" / "docs" / "projects" / SLUG / "review-2.md"
    hosted_round = live_daemon["root"] / daemon.PROJECTS_DIRNAME / SLUG / "review-2.md"
    assert local_round.read_text() == hosted_round.read_text()
    assert _findings(hosted_round) == [
        "- F-02 (blocker, regression) [src/x.py:10] The fix broke it",
        "- F-03 (should-fix, regression) [src/x.py:9-10] And here",
        "- F-04 (should-fix) [src/x.py:30] Not a regression",
        "- F-05 (nit) [src/x.py:10] A name reads oddly",
    ]
    # The daemon holds no git repository; the mark reached it as data.
    assert not (live_daemon["root"] / ".git").exists()
    assert [body["regression"] for body in sent] == [False, True, True, False, False]


# --- review done applies the rules to hand-written lines ---------------------


def _write_findings(path, lines):
    """Write ``lines`` by hand as the Findings section of the round at ``path``."""
    path.write_text(
        markdown.replace_section_body(path.read_text(), review.FINDINGS_HEADER, "\n".join(lines))
    )


def _check_round_one_closed():
    for item in ("F-01", "F-02"):
        _run("review", "finding", "check", item, "closed")


def _docs(root):
    """Every file under ``root``'s docs directory, with its bytes."""
    return {path: path.read_bytes() for path in (root / "docs").rglob("*") if path.is_file()}


# Round 2's report, as a reviewer with no shell returns it: round 1's items
# checked, and the findings under it.
_REPORT = (
    "# Review round 2\n\n## Scope reviewed\n\n- the fix\n\n"
    "## Earlier findings\n\n- F-01 closed\n- F-02 closed\n\n"
    "## Findings\n\n{findings}\n\n## Verdict\n"
)


@pytest.mark.parametrize("severity", ["blocker", "should-fix"])
def test_done_marks_a_hand_written_line_on_a_line_a_fix_changed(tmp_path, monkeypatch, severity):
    project_dir, _, _ = _second_round(tmp_path, monkeypatch)
    path = project_dir / "review-2.md"
    _check_round_one_closed()
    _write_findings(path, [
        f"- F-03 ({severity}) [src/x.py:9-10] Hand-written on B's line",
        "- F-04 (should-fix) [src/x.py:30] Hand-written on a line no fix changed",
        "- F-05 (nit) [src/x.py:10] A nit is never marked",
    ])
    before = review.body_of(path)

    result = runner.invoke(app, ["review", "done"])

    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("thing/review-2 closed changes-requested (")
    assert result.stderr == ""
    assert _findings(path) == [
        f"- F-03 ({severity}, regression) [src/x.py:9-10] Hand-written on B's line",
        "- F-04 (should-fix) [src/x.py:30] Hand-written on a line no fix changed",
        "- F-05 (nit) [src/x.py:10] A nit is never marked",
    ]
    # The mark is the one change the close makes to the body.
    assert review.body_of(path) == before.replace(
        f"({severity}) [src/x.py:9-10]", f"({severity}, regression) [src/x.py:9-10]"
    )


def test_done_marks_a_line_that_arrives_in_an_ingested_report(tmp_path, monkeypatch):
    project_dir, _, _ = _second_round(tmp_path, monkeypatch)
    report = tmp_path / "report.md"
    report.write_text(_REPORT.format(findings=(
        "- F-03 (should-fix) [src/x.py:10] The fix broke it\n"
        "- F-04 (blocker) [src/x.py:30] Not a regression"
    )))

    result = runner.invoke(app, ["review", "done", "--file", str(report)])

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    assert review.body_of(project_dir / "review-2.md") == report.read_text().replace(
        "(should-fix) [src/x.py:10]", "(should-fix, regression) [src/x.py:10]"
    )


def test_done_keeps_a_mark_a_line_already_carries(tmp_path, monkeypatch):
    project_dir, _, _ = _second_round(tmp_path, monkeypatch)
    _check_round_one_closed()
    lines = [
        "- F-03 (should-fix, regression) [src/x.py:10] Marked by hand on B's line",
        "- F-04 (blocker, regression) [src/x.py:30] Marked by hand on a line no fix changed",
    ]
    _write_findings(project_dir / "review-2.md", lines)

    result = runner.invoke(app, ["review", "done"])

    assert result.exit_code == 0, result.output
    assert _findings(project_dir / "review-2.md") == lines


@pytest.mark.parametrize("how", ["round file", "--file"])
@pytest.mark.parametrize("severity", ["blocker", "should-fix"])
def test_done_refuses_a_hand_written_line_with_no_location_and_changes_no_file(
    tmp_path, monkeypatch, severity, how
):
    project_dir, _, _ = _second_round(tmp_path, monkeypatch)
    # The nit comes first: a close that went on would write its follow-up.
    lines = [
        "- F-03 (nit) A name reads oddly",
        f"- F-04 ({severity}) Where is it",
        "- F-05 (should-fix) [src/x.py:10] The fix broke it",
    ]
    args = ["review", "done"]
    if how == "--file":
        report = tmp_path / "report.md"
        report.write_text(_REPORT.format(findings="\n".join(lines)))
        args += ["--file", str(report)]
    else:
        _check_round_one_closed()
        _write_findings(project_dir / "review-2.md", lines)
    before = _docs(tmp_path)

    result = runner.invoke(app, args)

    assert result.exit_code == 1, result.output
    assert f"'- F-04 ({severity}) Where is it'" in result.stderr
    assert "specflo review finding add" in result.stderr
    assert "specflo review waive" in result.stderr
    assert "note:" not in result.stderr
    assert _docs(tmp_path) == before


def test_done_leaves_a_mark_it_cannot_compute_off_with_a_note(tmp_path, monkeypatch):
    project_dir, _, _ = _second_round(tmp_path, monkeypatch)
    _set_sha(project_dir / "review-1.md", "deadbee")
    _check_round_one_closed()
    _write_findings(project_dir / "review-2.md", [
        "- F-03 (blocker) [src/x.py:10] The fix broke it",
        "- F-04 (nit) [src/x.py:10] A nit gets no note",
    ])

    result = runner.invoke(app, ["review", "done"])

    assert result.exit_code == 0, result.output
    assert result.stderr == (
        "note: F-03 [src/x.py:10] was not checked for a regression:"
        " this checkout has no commit deadbee.\n"
    )
    assert _findings(project_dir / "review-2.md")[0] == "- F-03 (blocker) [src/x.py:10] The fix broke it"


def test_a_closed_round_with_no_locations_is_never_refused_or_rewritten(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    old = project_dir / "review-1.md"
    old.write_text(
        "---\nround: 1\nverdict: changes-requested\ndate: '2026-08-22'\nsha: ''\nbase: ''\n"
        "level: ''\nreason: ''\n---\n\n# Review round 1\n\n## Scope reviewed\n\n"
        "## Findings\n\n- F-01 (blocker) Written before a line named its location\n\n## Verdict\n"
    )
    before = old.read_bytes()
    _change(tmp_path, 10, "line 10 in B")
    _start()
    _run("review", "finding", "check", "F-01", "closed")
    _write_findings(project_dir / "review-2.md", ["- F-02 (should-fix) [src/x.py:10] New"])

    result = runner.invoke(app, ["review", "done"])

    assert result.exit_code == 0, result.output
    assert old.read_bytes() == before


def _no_git(*args, **kwargs):
    raise AssertionError(f"the service ran {args[0] if args else kwargs}")


def test_the_service_writes_the_marks_it_is_sent_at_close_and_runs_no_git(tmp_path, monkeypatch):
    project_dir, _, _ = _second_round(tmp_path, monkeypatch)
    _check_round_one_closed()
    _write_findings(project_dir / "review-2.md", [
        "- F-03 (blocker) [src/x.py:30] One",
        "- F-04 (should-fix, regression) [src/x.py:31] Two",
        "- F-05 (should-fix) [src/x.py:32] Three",
    ])
    svc = LocalProjectService(tmp_path, config.load_config(tmp_path))
    monkeypatch.setattr(subprocess, "run", _no_git)

    # Lines 30 to 32 were never changed: the service does not look.
    closed = svc.close_round(SLUG, regressions=["F-03", "F-04"])

    assert closed.verdict == "changes-requested"
    assert _findings(project_dir / "review-2.md") == [
        "- F-03 (blocker, regression) [src/x.py:30] One",
        "- F-04 (should-fix, regression) [src/x.py:31] Two",
        "- F-05 (should-fix) [src/x.py:32] Three",
    ]


@pytest.mark.parametrize(
    "line, marks, refusal",
    [
        ("- F-03 (nit) [src/x.py:10] One", ["F-03"], "A nit is never marked as a regression"),
        ("- F-03 (nit) One", ["F-03"], "A nit is never marked as a regression"),
        ("- F-03 (blocker) [src/x.py:10] One", ["F-09"], "F-09 is not a finding of review-2.md"),
        ("- F-03 (blocker) One", [], "- F-03 (blocker) One"),
    ],
)
def test_the_service_refuses_an_untrusted_close_and_changes_no_file(
    tmp_path, monkeypatch, line, marks, refusal
):
    project_dir, _, _ = _second_round(tmp_path, monkeypatch)
    _check_round_one_closed()
    _write_findings(project_dir / "review-2.md", [line])
    svc = LocalProjectService(tmp_path, config.load_config(tmp_path))
    before = _docs(tmp_path)

    with pytest.raises(SpecfloError, match=re.escape(refusal)):
        svc.close_round(SLUG, regressions=marks)

    assert _docs(tmp_path) == before


def test_the_service_refuses_a_mark_at_close_with_no_reviewed_round_before(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    _run("review", "waive", "--reason", "Reviewed by hand")
    _run("review", "start")
    _write_findings(project_dir / "review-2.md", ["- F-01 (blocker) [src/x.py:10] One"])
    svc = LocalProjectService(tmp_path, config.load_config(tmp_path))
    before = _docs(tmp_path)

    with pytest.raises(SpecfloError, match="no round before review-2.md was reviewed"):
        svc.close_round(SLUG, regressions=["F-01"])

    assert _docs(tmp_path) == before


def test_the_service_refuses_a_mark_on_a_waived_close(tmp_path, monkeypatch):
    project_dir, _, _ = _second_round(tmp_path, monkeypatch)
    _write_findings(project_dir / "review-2.md", ["- F-03 (blocker) [src/x.py:10] One"])
    svc = LocalProjectService(tmp_path, config.load_config(tmp_path))
    before = _docs(tmp_path)

    with pytest.raises(SpecfloError, match="A waived round"):
        svc.close_round(SLUG, "waived", reason="Reviewed by hand", regressions=["F-03"])

    assert _docs(tmp_path) == before


def _hand_written_run(tmp_path, monkeypatch, name, new_args, rounds, register=None):
    """Three rounds in a fresh git checkout ``name``, with a fix before each
    later one: round 2's lines written by hand into the round file in
    ``rounds`` (a function of the checkout), round 3's ingested with --file.
    Each step's ``(args, exit code, stdout, stderr)``, the regression marks
    each close_round request sent, and the round directory."""
    checkout = tmp_path / name
    checkout.mkdir()
    _git(checkout, "init", "-q")
    _commit(checkout, "src/x.py", X_PY)
    config.init_config(checkout)
    monkeypatch.chdir(checkout)
    if register is not None:
        registered = runner.invoke(app, ["remote", "add", "home", *register])
        assert registered.exit_code == 0, registered.output
    created = runner.invoke(app, ["new", "Thing", "--summary", "One line", *new_args])
    assert created.exit_code == 0, created.output
    sent = []
    real_send = httpx.Client.send

    def recording_send(self, request, **kwargs):
        if request.url.path.endswith("/close_round"):
            sent.append(json.loads(request.content).get("regressions"))
        return real_send(self, request, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", recording_send)
    results = []

    def step(*args):
        result = runner.invoke(app, list(args))
        results.append((args, result.exit_code, result.stdout, result.stderr))

    step("review", "start")
    step("review", "finding", "add", "--severity", "should-fix", "--at", "src/x.py:10", "--text", "One")
    step("review", "done")
    _change(checkout, 10, "line 10 fixed")
    fix_by_cli(runner, app, "F-01")
    step("review", "start")
    step("review", "finding", "check", "F-01", "closed")
    _write_findings(rounds(checkout) / "review-2.md", [
        "- F-02 (blocker) [src/x.py:10] The fix broke it",
        "- F-03 (should-fix, regression) [src/x.py:30] Marked by hand",
        "- F-04 (should-fix) [src/x.py:30] Not a regression",
    ])
    step("review", "done")
    _change(checkout, 20, "line 20 fixed")
    fix_by_cli(runner, app, "F-02", "F-03", "F-04")
    step("review", "start", "--over-budget")
    (checkout / "report.md").write_text(
        "# Review round 3\n\n## Earlier findings\n\n- F-02 closed\n- F-03 closed\n- F-04 closed\n\n"
        "## Findings\n\n- F-05 (should-fix) [src/x.py:19-20] And here\n"
        "- F-06 (blocker) [src/x.py:1] Not a regression\n"
    )
    step("review", "done", "--file", "report.md")
    step("doc", "show", "review-2")
    step("doc", "show", "review-3")
    monkeypatch.setattr(httpx.Client, "send", real_send)
    return results, sent, rounds(checkout)


def test_a_hand_written_line_is_marked_the_same_for_a_local_and_a_hosted_project(
    tmp_path, monkeypatch, live_daemon
):
    local, _, local_rounds = _hand_written_run(
        tmp_path, monkeypatch, "local", [], lambda checkout: checkout / "docs" / "projects" / SLUG,
    )
    hosted, sent, hosted_rounds = _hand_written_run(
        tmp_path, monkeypatch, "hosted", ["--remote", "home"],
        lambda _: live_daemon["root"] / daemon.PROJECTS_DIRNAME / SLUG,
        register=[live_daemon["url"], "--token", live_daemon["token"]],
    )

    for mine, theirs in zip(local, hosted, strict=True):
        assert mine == theirs, f"{' '.join(mine[0])}:\nlocal: {mine[1:]}\nhosted: {theirs[1:]}"
    assert [code for _, code, _, _ in local] == [0] * len(local)
    for name in ("review-2.md", "review-3.md"):
        assert (local_rounds / name).read_text() == (hosted_rounds / name).read_text()
    assert _findings(hosted_rounds / "review-2.md") == [
        "- F-02 (blocker, regression) [src/x.py:10] The fix broke it",
        "- F-03 (should-fix, regression) [src/x.py:30] Marked by hand",
        "- F-04 (should-fix) [src/x.py:30] Not a regression",
    ]
    assert _findings(hosted_rounds / "review-3.md") == [
        "- F-05 (should-fix, regression) [src/x.py:19-20] And here",
        "- F-06 (blocker) [src/x.py:1] Not a regression",
    ]
    # The CLI blamed the lines in the checkout; the daemon got the IDs as data.
    assert not (live_daemon["root"] / ".git").exists()
    assert sent == [[], ["F-02"], ["F-05"]]

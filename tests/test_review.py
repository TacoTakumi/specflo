"""Review rounds: the numbered ``review-N.md`` artifacts a project accrues.

A round is one end-of-execute whole-branch review. ``specflo review start``
mints the next one from a skeleton (REQ-01) and never reuses a number, so a
deleted round leaves a permanent gap (REQ-02).
"""

import yaml
from typer.testing import CliRunner

from specflo import config, projects
from specflo.cli import app

runner = CliRunner()


def _project(tmp_path, monkeypatch):
    """An active 'Thing' at ``tmp_path``, with the cwd moved into it."""
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing", created="2026-08-22")
    projects.switch_project(tmp_path, cfg, "Thing")
    monkeypatch.chdir(tmp_path)
    return tmp_path / "docs" / "projects" / "thing"


def _frontmatter(path):
    return yaml.safe_load(path.read_text().split("---", 2)[1])


def _closed_round(project_dir, number, verdict="ready-to-merge"):
    path = project_dir / f"review-{number}.md"
    path.write_text(
        f"---\nround: {number}\nverdict: {verdict}\ndate: '2026-08-22'\n"
        f"sha: abc1234\nreason: ''\n---\n\n# Review round {number}\n"
    )
    return path


def test_mint_creates_the_first_round_and_prints_its_path(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["review", "start"])

    assert result.exit_code == 0, result.output
    minted = project_dir / "review-1.md"
    assert minted.is_file()
    assert str(minted) in result.output


def test_mint_writes_the_skeleton_frontmatter_and_headings(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)

    runner.invoke(app, ["review", "start"])

    minted = project_dir / "review-1.md"
    assert set(_frontmatter(minted)) == {"round", "verdict", "date", "sha", "reason"}
    assert _frontmatter(minted)["round"] == 1
    assert not _frontmatter(minted)["verdict"]        # open until `review done`
    body = minted.read_text()
    for heading in ("## Scope reviewed", "## Findings", "## Verdict"):
        assert heading in body


def test_mint_numbers_past_the_highest_round_and_never_fills_a_gap(
    tmp_path, monkeypatch
):
    # REQ-02: review-2.md was deleted; the next round is 4, not 2.
    project_dir = _project(tmp_path, monkeypatch)
    _closed_round(project_dir, 1)
    _closed_round(project_dir, 3, verdict="changes-requested")

    result = runner.invoke(app, ["review", "start"])

    assert result.exit_code == 0, result.output
    assert (project_dir / "review-4.md").is_file()
    assert not (project_dir / "review-2.md").exists()
    assert _frontmatter(project_dir / "review-4.md")["round"] == 4


def _open_round(project_dir, number):
    """An unclosed round: present, numbered, with no verdict yet."""
    path = project_dir / f"review-{number}.md"
    path.write_text(
        f"---\nround: {number}\nverdict: ''\ndate: '2026-08-22'\n"
        f"sha: ''\nreason: ''\n---\n\n# Review round {number}\n"
    )
    return path


def test_start_with_an_open_round_reuses_it_and_mints_nothing(tmp_path, monkeypatch):
    # REQ-03: a session that died mid-review is walked back into, not stepped
    # over -- there is no discard command, so reuse is the whole recovery path.
    project_dir = _project(tmp_path, monkeypatch)
    _closed_round(project_dir, 1)
    still_open = _open_round(project_dir, 2)
    before = still_open.read_text()

    result = runner.invoke(app, ["review", "start"])

    assert result.exit_code == 0, result.output
    assert str(still_open) in result.output
    assert "open" in result.output
    assert not (project_dir / "review-3.md").exists()
    assert still_open.read_text() == before          # reused, not rewritten


def test_open_round_is_the_one_with_an_empty_verdict(tmp_path, monkeypatch):
    # A closed latest round is no obstacle: the next `review start` mints.
    project_dir = _project(tmp_path, monkeypatch)
    _closed_round(project_dir, 1, verdict="changes-requested")

    result = runner.invoke(app, ["review", "start"])

    assert result.exit_code == 0, result.output
    assert (project_dir / "review-2.md").is_file()


def test_close_writes_the_verdict_into_the_open_round(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    runner.invoke(app, ["review", "start"])
    minted = project_dir / "review-1.md"

    result = runner.invoke(app, ["review", "done", "--verdict", "ready-to-merge"])

    assert result.exit_code == 0, result.output
    assert _frontmatter(minted)["verdict"] == "ready-to-merge"
    assert "## Findings" in minted.read_text()       # the body survives the close


def test_close_accepts_each_of_the_three_verdicts(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    for number, verdict in enumerate(
        ("ready-to-merge", "changes-requested", "waived"), start=1
    ):
        runner.invoke(app, ["review", "start"])
        args = ["review", "done", "--verdict", verdict]
        if verdict == "waived":
            args += ["--reason", "not reviewing this one"]
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.output
        assert _frontmatter(project_dir / f"review-{number}.md")["verdict"] == verdict


def test_close_with_no_open_round_refuses_and_changes_nothing(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    closed = _closed_round(project_dir, 1)
    before = closed.read_text()

    result = runner.invoke(app, ["review", "done", "--verdict", "changes-requested"])

    assert result.exit_code != 0
    assert "no review is open" in result.output.lower()
    assert closed.read_text() == before


def test_close_rejects_an_unknown_verdict_and_leaves_the_round_open(
    tmp_path, monkeypatch
):
    project_dir = _project(tmp_path, monkeypatch)
    runner.invoke(app, ["review", "start"])

    result = runner.invoke(app, ["review", "done", "--verdict", "approved"])

    assert result.exit_code != 0
    for valid in ("ready-to-merge", "changes-requested", "waived"):
        assert valid in result.output
    assert not _frontmatter(project_dir / "review-1.md")["verdict"]


def test_waived_without_a_reason_is_refused_and_the_round_stays_open(
    tmp_path, monkeypatch
):
    # REQ-06: the recorded escape past the completion gate has to say why.
    project_dir = _project(tmp_path, monkeypatch)
    runner.invoke(app, ["review", "start"])

    result = runner.invoke(app, ["review", "done", "--verdict", "waived"])

    assert result.exit_code != 0
    assert "reason" in result.output.lower()
    assert not _frontmatter(project_dir / "review-1.md")["verdict"]


def test_waived_with_a_reason_closes_and_stores_it(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    runner.invoke(app, ["review", "start"])

    result = runner.invoke(
        app, ["review", "done", "--verdict", "waived", "--reason", "text"]
    )

    assert result.exit_code == 0, result.output
    fields = _frontmatter(project_dir / "review-1.md")
    assert (fields["verdict"], fields["reason"]) == ("waived", "text")


def test_waived_reason_is_not_required_by_the_other_verdicts(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    runner.invoke(app, ["review", "start"])

    result = runner.invoke(app, ["review", "done", "--verdict", "ready-to-merge"])

    assert result.exit_code == 0, result.output
    assert not _frontmatter(project_dir / "review-1.md")["reason"]


def _git_repo(path):
    """Turn ``path`` into a git repo with one commit, and return its short sha."""
    import subprocess

    run = lambda *args: subprocess.run(  # noqa: E731 - terse local shorthand
        args, cwd=path, capture_output=True, text=True, check=True
    )
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "T")
    (path / "seed.txt").write_text("seed\n")
    run("git", "add", "seed.txt")
    run("git", "commit", "-qm", "seed")
    return run("git", "rev-parse", "--short", "HEAD").stdout.strip()


def test_stamp_records_todays_date_over_the_start_date(tmp_path, monkeypatch):
    import datetime

    from specflo import review

    project_dir = _project(tmp_path, monkeypatch)
    cfg = config.load_config(tmp_path)
    review.start_round(tmp_path, cfg, "thing", today="2020-01-01")

    result = runner.invoke(app, ["review", "done", "--verdict", "ready-to-merge"])

    assert result.exit_code == 0, result.output
    stamped = str(_frontmatter(project_dir / "review-1.md")["date"])
    assert stamped == datetime.date.today().isoformat()


def test_stamp_records_the_short_head_sha_inside_a_git_repo(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    sha = _git_repo(tmp_path)
    runner.invoke(app, ["review", "start"])

    result = runner.invoke(app, ["review", "done", "--verdict", "ready-to-merge"])

    assert result.exit_code == 0, result.output
    assert _frontmatter(project_dir / "review-1.md")["sha"] == sha


def test_stamp_is_empty_outside_git_and_the_close_still_succeeds(
    tmp_path, monkeypatch
):
    # The git lookup degrades the way index.py's does: no repo, no stamp, no error.
    import datetime

    project_dir = _project(tmp_path, monkeypatch)
    runner.invoke(app, ["review", "start"])

    result = runner.invoke(app, ["review", "done", "--verdict", "ready-to-merge"])

    assert result.exit_code == 0, result.output
    fields = _frontmatter(project_dir / "review-1.md")
    assert fields["sha"] == ""
    assert str(fields["date"]) == datetime.date.today().isoformat()


def test_ingest_replaces_an_untouched_skeleton_body_with_the_report(
    tmp_path, monkeypatch
):
    # REQ-10: the escape hatch for a subagent that returns its report as text.
    project_dir = _project(tmp_path, monkeypatch)
    runner.invoke(app, ["review", "start"])
    report = tmp_path / "report.md"
    report.write_text("# Round 1\n\n## Findings\n\n- one nit.\n")

    result = runner.invoke(
        app,
        ["review", "done", "--verdict", "ready-to-merge", "--file", str(report)],
    )

    assert result.exit_code == 0, result.output
    minted = project_dir / "review-1.md"
    from specflo.review import body_of

    assert body_of(minted) == report.read_text()
    assert _frontmatter(minted)["verdict"] == "ready-to-merge"


def test_ingest_refuses_when_the_round_body_was_already_written(
    tmp_path, monkeypatch
):
    project_dir = _project(tmp_path, monkeypatch)
    runner.invoke(app, ["review", "start"])
    minted = project_dir / "review-1.md"
    minted.write_text(minted.read_text().replace("## Findings\n", "## Findings\n\n- a.\n"))
    before = minted.read_text()
    report = tmp_path / "report.md"
    report.write_text("a different report\n")

    result = runner.invoke(
        app,
        ["review", "done", "--verdict", "ready-to-merge", "--file", str(report)],
    )

    assert result.exit_code != 0
    assert "already has content" in result.output.lower()
    assert minted.read_text() == before          # still open, still the human's text


def test_ingest_of_a_missing_report_refuses_and_leaves_the_round_open(
    tmp_path, monkeypatch
):
    project_dir = _project(tmp_path, monkeypatch)
    runner.invoke(app, ["review", "start"])

    result = runner.invoke(
        app,
        ["review", "done", "--verdict", "waived", "--reason", "x",
         "--file", str(tmp_path / "nope.md")],
    )

    assert result.exit_code != 0
    assert not _frontmatter(project_dir / "review-1.md")["verdict"]


def _execute_all_done(root):
    """A 'Thing' at execute whose only task is done - ready but for the review."""
    from specflo import plan, spec

    cfg = config.init_config(root)
    projects.create_project(root, cfg, "Thing", created="2026-08-01")
    projects.switch_project(root, cfg, "Thing")
    spec.start_spec(root, cfg, "thing", today="2026-08-01")
    spec.add_requirement(root, cfg, "thing", "r", acceptance="a", today="2026-08-01")
    proj_md = root / "docs" / "projects" / "thing" / "project.md"
    proj_md.write_text(proj_md.read_text().replace("phase: brainstorm", "phase: execute"))
    plan.start_plan(root, cfg, "thing", today="2026-08-01")
    plan.add_task(root, cfg, "thing", "build it", acceptance="a", verify="v",
                  implements=["REQ-01"], today="2026-08-01")
    plan.start_task(root, cfg, "thing", "T-01", today="2026-08-01")
    plan.done_task(root, cfg, "thing", "T-01", today="2026-08-01")
    return cfg


def _derived(root, cfg):
    """Every surface that reads review state, as one comparable snapshot."""
    from specflo import status, validators

    project = projects.load_project(root, cfg, "thing")
    info = status.build_status(root, cfg, project)
    return (
        status.render_status(root, info),
        info["next_step"],
        validators.execute_issues(root, cfg, "thing"),
    )


def test_a_later_commit_leaves_a_closed_round_stale_free(tmp_path, monkeypatch):
    # REQ-08: the stamp is evidence for a human judgement call, never a derived
    # verdict. Committing after the review changes HEAD, and nothing else.
    from specflo import review

    monkeypatch.chdir(tmp_path)
    _git_repo(tmp_path)
    cfg = _execute_all_done(tmp_path)
    review.start_round(tmp_path, cfg, "thing", today="2026-08-01")
    round_file = review.close_round(
        tmp_path, cfg, "thing", "ready-to-merge", today="2026-08-02"
    )
    before = _derived(tmp_path, cfg)
    assert before[2] == []                       # the gate is open before the commit
    old_head = _frontmatter(round_file)["sha"]
    assert old_head and old_head in before[0]    # the stamp really is on the line
    round_text = round_file.read_text()

    import subprocess

    (tmp_path / "later.txt").write_text("work that landed after the review\n")
    subprocess.run(["git", "add", "later.txt"], cwd=tmp_path, check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-qm", "later"], cwd=tmp_path, check=True,
                   capture_output=True)
    new_head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=tmp_path,
                              capture_output=True, text=True, check=True).stdout.strip()

    assert _derived(tmp_path, cfg) == before     # byte-identical on every surface
    assert round_file.read_text() == round_text  # the round itself is untouched
    assert new_head not in before[0]             # the line still names the old sha


def test_no_surface_asks_git_whether_a_round_is_stale(tmp_path):
    # The same guard stated structurally: nothing in the read path compares the
    # round's sha to HEAD, so there is no code path that could grow an expiry.
    from conftest import executable_identifiers

    from specflo import review

    for func in (review.review_state, review.completion_issues):
        code = executable_identifiers(func)
        assert "head_sha" not in code
        assert "stale" not in code


# --- degrading on a round file nobody minted (round 1, F2/F3) -----------------
# Round files are CLI-owned, but `review.py` promises a hand-mangled one reads as
# open rather than taking the CLI down. These pin that promise on the paths a
# fresh session actually walks: status, checkpoint, advance, review start.


def _malformed_project(tmp_path, monkeypatch, name, text):
    """An execute-phase project carrying one hand-written round file."""
    monkeypatch.chdir(tmp_path)
    cfg = _execute_all_done(tmp_path)
    (tmp_path / "docs" / "projects" / "thing" / name).write_text(text)
    return cfg


def test_malformed_frontmatter_reads_as_an_open_round(tmp_path, monkeypatch):
    # yaml.safe_load returns a str here, not a mapping - .get() would blow up.
    _malformed_project(tmp_path, monkeypatch, "review-1.md",
                       "---\njust some text\n---\n\nbody\n")

    for args in (["status"], ["checkpoint"], ["review", "start"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, (args, result.output)
    assert "round 1 open" in runner.invoke(app, ["status"]).output
    assert not (tmp_path / "docs" / "projects" / "thing" / "review-2.md").exists()


def test_malformed_missing_frontmatter_reads_as_an_open_round(tmp_path, monkeypatch):
    _malformed_project(tmp_path, monkeypatch, "review-1.md", "no frontmatter here\n")

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0, result.output
    assert "round 1 open" in result.output


def test_malformed_zero_padded_filename_acts_on_the_file_that_exists(tmp_path, monkeypatch):
    # review-007.md parses as round 7; rebuilding "review-7.md" names nothing.
    _malformed_project(
        tmp_path, monkeypatch, "review-007.md",
        "---\nround: 7\nverdict: ready-to-merge\ndate: '2026-08-02'\n"
        "sha: abc1234\nreason: ''\n---\n\n# Review round 7\n",
    )
    project_dir = tmp_path / "docs" / "projects" / "thing"

    for args in (["status"], ["checkpoint"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, (args, result.output)
    assert "round 7 ready-to-merge" in runner.invoke(app, ["status"]).output

    assert runner.invoke(app, ["review", "start"]).exit_code == 0
    assert (project_dir / "review-8.md").is_file()          # minted past it
    assert not (project_dir / "review-7.md").exists()       # never conjured


def test_malformed_zero_padded_filename_still_clears_the_completion_gate(
    tmp_path, monkeypatch
):
    _malformed_project(
        tmp_path, monkeypatch, "review-007.md",
        "---\nround: 7\nverdict: ready-to-merge\ndate: '2026-08-02'\n"
        "sha: abc1234\nreason: ''\n---\n\n# Review round 7\n",
    )

    result = runner.invoke(app, ["advance"])

    assert result.exit_code == 0, result.output
    assert "status: complete" in (
        tmp_path / "docs" / "projects" / "thing" / "project.md"
    ).read_text()


def test_malformed_non_numeric_round_refuses_the_close_without_a_traceback(
    tmp_path, monkeypatch
):
    _malformed_project(
        tmp_path, monkeypatch, "review-1.md",
        "---\nround: one\nverdict: ''\ndate: '2026-08-01'\n"
        "sha: ''\nreason: ''\n---\n\n# Review round 1\n",
    )
    round_file = tmp_path / "docs" / "projects" / "thing" / "review-1.md"
    before = round_file.read_text()

    result = runner.invoke(app, ["review", "done", "--verdict", "ready-to-merge"])

    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "error:" in result.output.lower()
    assert round_file.read_text() == before                 # still open, untouched


def test_malformed_close_of_a_file_without_frontmatter_keeps_its_own_number(
    tmp_path, monkeypatch
):
    _malformed_project(tmp_path, monkeypatch, "review-1.md", "no frontmatter here\n")

    result = runner.invoke(app, ["review", "done", "--verdict", "ready-to-merge"])

    assert result.exit_code == 0, result.output
    fields = _frontmatter(tmp_path / "docs" / "projects" / "thing" / "review-1.md")
    assert fields["round"] == 1                             # from its name, not 0


def test_malformed_report_file_that_is_not_text_refuses_the_close(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _execute_all_done(tmp_path)
    from specflo import review

    round_file = review.start_round(tmp_path, cfg, "thing", today="2026-08-01")[0]
    before = round_file.read_text()
    binary = tmp_path / "report.bin"
    binary.write_bytes(b"\xff\xfe\x00\x80not utf-8\x00")

    result = runner.invoke(
        app,
        ["review", "done", "--verdict", "ready-to-merge", "--file", str(binary)],
    )

    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "error:" in result.output.lower()
    assert round_file.read_text() == before

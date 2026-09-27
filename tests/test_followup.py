"""Follow-ups: the work a project leaves for a later one.

Each project keeps its follow-ups in its own followup.md, created on the first
``specflo followup add``. The FU-NN numbers run across the whole projects
directory, hand-written documents included, so any project can cite an entry
with no project prefix.
"""

import datetime
import json
import multiprocessing
import os
import re
import subprocess

import pytest
from typer.testing import CliRunner

from specflo import config, followup, plan, projects, spec
from specflo.errors import SpecfloError
from specflo.cli import app

runner = CliRunner()


def _checkout(tmp_path, monkeypatch, *names):
    """A checkout holding one project per name; the first is active."""
    cfg = config.init_config(tmp_path)
    for name in names:
        projects.create_project(tmp_path, cfg, name, created="2026-09-27")
    projects.switch_project(tmp_path, cfg, names[0])
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _doc(root, slug):
    return (root / "docs" / "projects" / slug / "followup.md").read_text()


def _add(*args):
    return runner.invoke(app, ["followup", "add", *args])


# --- add ----------------------------------------------------------------------


def test_the_first_add_creates_the_document_that_doc_show_prints(tmp_path, monkeypatch):
    root = _checkout(tmp_path, monkeypatch, "Alpha")

    before = runner.invoke(app, ["doc", "show", "followup"])
    assert before.exit_code != 0
    assert not (root / "docs" / "projects" / "alpha" / "followup.md").exists()

    result = _add("Fix the flaky test", "--do", "Make it wait for idle")
    assert result.exit_code == 0, result.output
    assert "FU-01" in result.output

    shown = runner.invoke(app, ["doc", "show", "followup"])
    assert shown.exit_code == 0, shown.output
    section = shown.output.split("## Follow-ups", 1)[1]
    assert "### FU-01 - Fix the flaky test" in section


def test_the_entry_holds_the_heading_do_from_and_status_lines_in_order(tmp_path, monkeypatch):
    root = _checkout(tmp_path, monkeypatch, "Alpha")

    result = _add("T", "--do", "X", "--from", "Y")
    assert result.exit_code == 0, result.output

    assert "### FU-01 - T\n- Do: X\n- From: Y\n- Status: open\n" in _doc(root, "alpha")


def test_an_entry_without_from_has_no_from_line(tmp_path, monkeypatch):
    root = _checkout(tmp_path, monkeypatch, "Alpha")

    assert _add("T", "--do", "X").exit_code == 0

    doc = _doc(root, "alpha")
    assert "### FU-01 - T\n- Do: X\n- Status: open\n" in doc
    assert "- From:" not in doc


def test_ids_run_on_from_the_highest_fu_number_in_any_followup_document(tmp_path, monkeypatch):
    root = _checkout(tmp_path, monkeypatch, "Alpha", "Beta")
    # A hand-written document names its entries in its own shapes.
    (root / "docs" / "projects" / "beta" / "followup.md").write_text(
        "# beta\n\n### FU-85. A flaky test\n\n- **FU-89.** Another one\n"
    )

    result = _add("Next", "--do", "X")

    assert result.exit_code == 0, result.output
    assert "FU-90" in result.output
    assert "### FU-90 - Next" in _doc(root, "alpha")
    assert _add("After", "--do", "Y").output.count("FU-91") == 1


def test_adds_in_two_projects_share_one_numbering(tmp_path, monkeypatch):
    root = _checkout(tmp_path, monkeypatch, "Alpha", "Beta")
    cfg = config.load_config(root)

    first = followup.add_followup(root, cfg, "alpha", "One", "X")
    second = followup.add_followup(root, cfg, "beta", "Two", "Y")

    assert (first.id, second.id) == ("FU-01", "FU-02")
    assert "### FU-02 - Two" in _doc(root, "beta")
    assert "FU-02" not in _doc(root, "alpha")


def _add_in(args):
    root, slug, title = args
    cfg = config.load_config(root)
    return followup.add_followup(root, cfg, slug, title, "Do it").id


def test_concurrent_adds_in_two_projects_get_distinct_ids(tmp_path, monkeypatch):
    root = _checkout(tmp_path, monkeypatch, "Alpha", "Beta")
    jobs = [(root, slug, f"{slug} {i}") for i in range(4) for slug in ("alpha", "beta")]

    with multiprocessing.get_context("fork").Pool(len(jobs)) as pool:
        ids = pool.map(_add_in, jobs)

    assert sorted(ids) == [f"FU-{n:02d}" for n in range(1, 9)]
    for slug in ("alpha", "beta"):
        doc = _doc(root, slug)
        for i in range(4):
            assert doc.count(f" - {slug} {i}\n") == 1


def test_an_empty_title_or_do_is_refused_and_nothing_is_written(tmp_path, monkeypatch):
    root = _checkout(tmp_path, monkeypatch, "Alpha")
    path = root / "docs" / "projects" / "alpha" / "followup.md"

    for args in (["", "--do", "X"], ["  ", "--do", "X"], ["T"], ["T", "--do", ""], ["T", "--do", " "]):
        result = _add(*args)
        assert result.exit_code != 0, args
        assert not path.exists(), args

    assert _add("T", "--do", "X").exit_code == 0
    before = path.read_text()
    assert _add("U", "--do", "").exit_code != 0
    assert path.read_text() == before


def test_a_title_or_do_over_more_than_one_line_is_refused(tmp_path, monkeypatch):
    root = _checkout(tmp_path, monkeypatch, "Alpha")

    assert _add("T\nmore", "--do", "X").exit_code != 0
    assert _add("T", "--do", "X\n### FU-50 - forged").exit_code != 0
    assert _add("T", "--do", "X", "--from", "Y\nZ").exit_code != 0
    # Every line break the document parser splits on, not only \n and \r.
    for brk in ("\x0b", "\x0c", "\x1c", "\x85", " ", " "):
        assert _add("T", "--do", f"X{brk}- Status: closed").exit_code != 0, repr(brk)
    assert not (root / "docs" / "projects" / "alpha" / "followup.md").exists()


def test_an_add_to_a_document_without_a_final_newline_starts_a_new_line(tmp_path, monkeypatch):
    root = _checkout(tmp_path, monkeypatch, "Alpha")
    assert _add("One", "--do", "X").exit_code == 0
    path = root / "docs" / "projects" / "alpha" / "followup.md"
    path.write_text(path.read_text().rstrip("\n"))

    assert _add("Two", "--do", "Y").exit_code == 0

    listed = json.loads(_list("--json").output)
    assert [(e["id"], e["status"]) for e in listed] == [("FU-01", "open"), ("FU-02", "open")]


def _break_document(root, slug):
    """Make ``slug``'s followup document unreadable text."""
    (root / "docs" / "projects" / slug / "followup.md").write_bytes(b"\xff\xfe broken")


def test_add_and_close_refuse_by_name_when_a_document_cannot_be_read(tmp_path, monkeypatch):
    root = _checkout(tmp_path, monkeypatch, "Alpha", "Beta")
    followup.add_followup(root, config.load_config(root), "alpha", "T", "X")
    _break_document(root, "beta")
    before = _all_docs(root)

    for result in (_add("U", "--do", "Y"), _close("FU-01", "--note", "Done")):
        assert result.exit_code == 1
        assert "error: " in result.stderr and "beta/followup" in result.stderr
        assert _all_docs(root) == before


def test_list_warns_about_a_document_it_cannot_read_and_lists_the_rest(tmp_path, monkeypatch):
    root = _checkout(tmp_path, monkeypatch, "Alpha", "Beta")
    followup.add_followup(root, config.load_config(root), "alpha", "T", "X")
    _break_document(root, "beta")

    text = _list()
    result = _list("--json")

    assert text.exit_code == 0, text.output
    assert "FU-01" in text.stdout
    assert "beta/followup" in text.stderr
    assert result.exit_code == 0, result.output
    assert [e["id"] for e in json.loads(result.stdout)] == ["FU-01"]
    assert "beta/followup" in result.stderr


def test_only_project_directories_count(tmp_path, monkeypatch):
    root = _checkout(tmp_path, monkeypatch, "Alpha")
    for stray in (".staging", "not-a-project"):
        (root / "docs" / "projects" / stray).mkdir()
        (root / "docs" / "projects" / stray / "followup.md").write_text(
            "## Follow-ups\n\n### FU-40 - Stray\n- Do: X\n- Status: open\n"
        )

    assert "FU-01" in _add("T", "--do", "X").output
    assert re.findall(r"FU-\d+", _list().output) == ["FU-01"]
    assert _close("FU-40", "--note", "Done").exit_code != 0


def test_add_prints_the_id_as_json(tmp_path, monkeypatch):
    _checkout(tmp_path, monkeypatch, "Alpha")

    result = _add("T", "--do", "X", "--json")

    assert result.exit_code == 0, result.output
    assert '"id": "FU-01"' in result.output
    assert '"project": "alpha"' in result.output


# --- close --------------------------------------------------------------------


def _close(*args):
    return runner.invoke(app, ["followup", "close", *args])


def _all_docs(root):
    return {
        path: path.read_bytes()
        for path in sorted((root / "docs" / "projects").glob("*/followup.md"))
    }


def test_close_closes_an_open_entry_in_another_project(tmp_path, monkeypatch):
    root = _checkout(tmp_path, monkeypatch, "Alpha", "Beta")
    cfg = config.load_config(root)
    followup.add_followup(root, cfg, "beta", "T", "X")

    result = _close("FU-01", "--note", "Done in alpha")

    assert result.exit_code == 0, result.output
    assert "FU-01" in result.output
    today = datetime.date.today().isoformat()
    assert (
        f"### FU-01 - T\n- Do: X\n- Status: closed\n- Closed: {today}: Done in alpha\n"
        in _doc(root, "beta")
    )
    assert not (root / "docs" / "projects" / "alpha" / "followup.md").exists()


def test_close_refusals_change_no_followup_document(tmp_path, monkeypatch):
    root = _checkout(tmp_path, monkeypatch, "Alpha", "Beta", "Gamma")
    cfg = config.load_config(root)
    (root / "docs" / "projects" / "gamma" / "followup.md").write_text(
        "# gamma\n\n### FU-85. A hand-written entry\n\nText.\n"
    )
    followup.add_followup(root, cfg, "alpha", "Open one", "X")
    followup.add_followup(root, cfg, "beta", "Closed one", "Y")
    assert _close("FU-87", "--note", "Done").exit_code == 0
    before = _all_docs(root)

    for args in (
        ["FU-87", "--note", "Again"],
        ["FU-999", "--note", "Done"],
        ["FU-85", "--note", "Done"],
        ["FU-86"],
        ["FU-86", "--note", ""],
        ["FU-86", "--note", "  "],
        ["FU-86", "--note", "one\ntwo"],
    ):
        result = _close(*args)
        assert result.exit_code != 0, args
        assert _all_docs(root) == before, args


def test_close_takes_the_open_entry_when_two_projects_hold_the_id(tmp_path, monkeypatch):
    root = _checkout(tmp_path, monkeypatch, "Alpha", "Beta")
    assert _add("T", "--do", "X").exit_code == 0
    # A merge of two branches can leave the same ID in two projects.
    alpha, beta = (root / "docs" / "projects" / slug / "followup.md" for slug in ("alpha", "beta"))
    beta.write_text(alpha.read_text())

    assert _close("FU-01", "--note", "First").exit_code == 0
    assert _close("FU-01", "--note", "Second").exit_code == 0

    for path in (alpha, beta):
        assert "- Status: closed\n" in path.read_text()
    assert _close("FU-01", "--note", "Third").exit_code != 0


# --- close --by -----------------------------------------------------------------


def _with_task(root, cfg, slug):
    """Give ``slug`` a plan holding one task, T-01."""
    spec.start_spec(root, cfg, slug, today="2026-09-27")
    spec.add_requirement(root, cfg, slug, "r", acceptance="a", today="2026-09-27")
    plan.start_plan(root, cfg, slug, today="2026-09-27")
    plan.add_task(root, cfg, slug, "build it", acceptance="a", verify="v",
                  implements=["REQ-01"], today="2026-09-27")


def _git_commit(root):
    """Make ``root`` a git repository with one commit and return its full SHA."""
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": os.environ["PATH"]}
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, env=env)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "work"], cwd=root, check=True, env=env)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, env=env,
                          capture_output=True, text=True).stdout.strip()


def test_close_by_a_task_adds_a_closed_by_line_below_the_closed_line(tmp_path, monkeypatch):
    root = _checkout(tmp_path, monkeypatch, "Alpha", "Beta")
    cfg = config.load_config(root)
    _with_task(root, cfg, "alpha")
    followup.add_followup(root, cfg, "beta", "T", "X")

    result = _close("FU-01", "--note", "Done", "--by", "alpha/T-01")

    assert result.exit_code == 0, result.output
    today = datetime.date.today().isoformat()
    assert (
        f"### FU-01 - T\n- Do: X\n- Status: closed\n- Closed: {today}: Done\n"
        "- Closed by: alpha/T-01\n"
    ) in _doc(root, "beta")


def test_close_by_a_project_names_the_project(tmp_path, monkeypatch):
    root = _checkout(tmp_path, monkeypatch, "Alpha", "Beta")
    cfg = config.load_config(root)
    followup.add_followup(root, cfg, "beta", "T", "X")

    result = _close("FU-01", "--note", "Done", "--by", "alpha")

    assert result.exit_code == 0, result.output
    assert "- Closed by: alpha\n" in _doc(root, "beta")


def test_close_by_a_commit_stores_its_short_sha(tmp_path, monkeypatch):
    root = _checkout(tmp_path, monkeypatch, "Alpha")
    cfg = config.load_config(root)
    followup.add_followup(root, cfg, "alpha", "T", "X")
    full = _git_commit(root)
    short = subprocess.run(["git", "rev-parse", "--short", full], cwd=root, check=True,
                           capture_output=True, text=True).stdout.strip()

    result = _close("FU-01", "--note", "Done", "--by", full)

    assert result.exit_code == 0, result.output
    assert f"- Closed by: {short}\n" in _doc(root, "alpha")
    assert full not in _doc(root, "alpha")


@pytest.mark.parametrize(
    "ref",
    ["gamma/T-01", "alpha/T-02", "nosuchthing"],
    ids=["unknown project", "unknown task", "neither project nor commit"],
)
def test_close_by_refuses_a_ref_that_names_no_work_and_writes_nothing(
    tmp_path, monkeypatch, ref
):
    root = _checkout(tmp_path, monkeypatch, "Alpha", "Beta")
    cfg = config.load_config(root)
    _with_task(root, cfg, "alpha")
    followup.add_followup(root, cfg, "beta", "T", "X")
    _git_commit(root)
    before = _all_docs(root)

    result = _close("FU-01", "--note", "Done", "--by", ref)

    assert result.exit_code != 0
    assert ref in result.output
    assert _all_docs(root) == before


def test_close_by_a_word_that_names_a_project_and_a_commit_names_the_project(
    tmp_path, monkeypatch
):
    root = _checkout(tmp_path, monkeypatch, "Alpha")
    cfg = config.load_config(root)
    followup.add_followup(root, cfg, "alpha", "T", "X")
    _git_commit(root)
    subprocess.run(["git", "tag", "alpha"], cwd=root, check=True)

    result = _close("FU-01", "--note", "Done", "--by", "alpha")

    assert result.exit_code == 0, result.output
    assert "- Closed by: alpha\n" in _doc(root, "alpha")


def test_close_without_by_writes_no_closed_by_line(tmp_path, monkeypatch):
    root = _checkout(tmp_path, monkeypatch, "Alpha")
    cfg = config.load_config(root)
    followup.add_followup(root, cfg, "alpha", "T", "X")

    assert _close("FU-01", "--note", "Done").exit_code == 0

    assert "- Closed by:" not in _doc(root, "alpha")


# --- task done --closes -----------------------------------------------------------


def _task_in_progress(tmp_path, monkeypatch):
    """Alpha active with T-01 in progress, Beta beside it."""
    root = _checkout(tmp_path, monkeypatch, "Alpha", "Beta")
    cfg = config.load_config(root)
    _with_task(root, cfg, "alpha")
    plan.start_task(root, cfg, "alpha", "T-01", today="2026-09-27")
    return root, cfg


def _progress(root, cfg):
    return plan.list_tasks(root, cfg, "alpha")[0].progress


def test_task_done_closes_each_named_followup_with_the_task_and_its_note(tmp_path, monkeypatch):
    root, cfg = _task_in_progress(tmp_path, monkeypatch)
    followup.add_followup(root, cfg, "beta", "One", "X")
    followup.add_followup(root, cfg, "alpha", "Two", "Y")

    result = runner.invoke(app, ["task", "done", "T-01", "--note", "Built it",
                                 "--closes", "FU-01", "--closes", "FU-02"])

    assert result.exit_code == 0, result.output
    assert _progress(root, cfg) == "done"
    today = datetime.date.today().isoformat()
    closed = f"- Status: closed\n- Closed: {today}: Built it\n- Closed by: alpha/T-01\n"
    assert f"### FU-01 - One\n- Do: X\n{closed}" in _doc(root, "beta")
    assert f"### FU-02 - Two\n- Do: Y\n{closed}" in _doc(root, "alpha")


def test_task_done_without_a_note_closes_with_the_task_title(tmp_path, monkeypatch):
    root, cfg = _task_in_progress(tmp_path, monkeypatch)
    followup.add_followup(root, cfg, "beta", "One", "X")

    result = runner.invoke(app, ["task", "done", "T-01", "--closes", "FU-01"])

    assert result.exit_code == 0, result.output
    today = datetime.date.today().isoformat()
    assert f"- Closed: {today}: build it\n- Closed by: alpha/T-01\n" in _doc(root, "beta")


def test_task_done_refuses_a_followup_that_is_not_open_before_the_task_changes(
    tmp_path, monkeypatch
):
    root, cfg = _task_in_progress(tmp_path, monkeypatch)
    followup.add_followup(root, cfg, "beta", "Open", "X")
    followup.add_followup(root, cfg, "beta", "Closed", "Y")
    followup.close_followup(root, cfg, "FU-02", "Done")
    before = _all_docs(root)
    plan_before = plan.plan_path(root, cfg, "alpha").read_bytes()

    for closes in (["FU-01", "FU-02"], ["FU-01", "FU-99"]):
        args = [arg for fu in closes for arg in ("--closes", fu)]
        result = runner.invoke(app, ["task", "done", "T-01", *args])
        assert result.exit_code != 0, closes
        assert closes[1] in result.output, closes
        assert _progress(root, cfg) == "in_progress", closes
        assert _all_docs(root) == before, closes
        assert plan.plan_path(root, cfg, "alpha").read_bytes() == plan_before, closes


def test_task_done_on_a_task_with_a_blank_title_still_closes_with_a_note(tmp_path, monkeypatch):
    root = _checkout(tmp_path, monkeypatch, "Alpha", "Beta")
    cfg = config.load_config(root)
    spec.start_spec(root, cfg, "alpha", today="2026-09-27")
    spec.add_requirement(root, cfg, "alpha", "r", acceptance="a", today="2026-09-27")
    plan.start_plan(root, cfg, "alpha", today="2026-09-27")
    plan.add_task(root, cfg, "alpha", "   ", acceptance="a", verify="v",
                  implements=["REQ-01"], today="2026-09-27")
    plan.start_task(root, cfg, "alpha", "T-01", today="2026-09-27")
    followup.add_followup(root, cfg, "beta", "One", "X")

    result = runner.invoke(app, ["task", "done", "T-01", "--closes", "FU-01"])

    assert result.exit_code == 0, result.output
    assert _progress(root, cfg) == "done"
    entry = followup.show_followup(root, cfg, "FU-01")
    assert entry.status == "closed"
    assert entry.closed.split(": ", 1)[1].strip()
    assert entry.closed_by == "alpha/T-01"


def test_task_done_that_fails_a_later_close_names_the_ones_it_closed(tmp_path, monkeypatch):
    root, cfg = _task_in_progress(tmp_path, monkeypatch)
    followup.add_followup(root, cfg, "beta", "One", "X")
    followup.add_followup(root, cfg, "beta", "Two", "Y")
    close = followup.close_followup

    def close_the_first_only(root, cfg, followup_id, *args, **kwargs):
        if followup_id == "FU-02":
            # Another command closed it between the check and the close.
            raise SpecfloError("FU-02 in beta/followup is closed, not open.")
        return close(root, cfg, followup_id, *args, **kwargs)

    monkeypatch.setattr(followup, "close_followup", close_the_first_only)
    result = runner.invoke(app, ["task", "done", "T-01", "--closes", "FU-01",
                                 "--closes", "FU-02"])

    assert result.exit_code != 0
    message = result.output.split("FU-02 was not closed", 1)[1]
    assert "FU-01" in message


def test_task_done_closes_with_the_one_line_note_the_task_records(tmp_path, monkeypatch):
    root, cfg = _task_in_progress(tmp_path, monkeypatch)
    followup.add_followup(root, cfg, "beta", "One", "X")

    result = runner.invoke(app, ["task", "done", "T-01", "--note", "line one\nline two",
                                 "--closes", "FU-01"])

    assert result.exit_code == 0, result.output
    assert _progress(root, cfg) == "done"
    today = datetime.date.today().isoformat()
    assert f"- Closed: {today}: line one line two\n" in _doc(root, "beta")


def test_task_done_refuses_an_unreadable_followup_document_before_the_task_changes(
    tmp_path, monkeypatch
):
    root, cfg = _task_in_progress(tmp_path, monkeypatch)
    followup.add_followup(root, cfg, "beta", "One", "X")
    projects.create_project(root, cfg, "Gamma", created="2026-09-27")
    _break_document(root, "gamma")
    before = _all_docs(root)

    result = runner.invoke(app, ["task", "done", "T-01", "--closes", "FU-01"])

    assert result.exit_code != 0
    assert "gamma/followup" in result.output
    assert _progress(root, cfg) == "in_progress"
    assert _all_docs(root) == before


def test_task_done_closes_a_repeated_followup_once(tmp_path, monkeypatch):
    root, cfg = _task_in_progress(tmp_path, monkeypatch)
    followup.add_followup(root, cfg, "beta", "One", "X")

    result = runner.invoke(app, ["task", "done", "T-01", "--closes", "FU-01",
                                 "--closes", "FU-01"])

    assert result.exit_code == 0, result.output
    assert _doc(root, "beta").count("- Closed by: alpha/T-01\n") == 1


def test_task_done_json_names_the_followups_it_closed(tmp_path, monkeypatch):
    root, cfg = _task_in_progress(tmp_path, monkeypatch)
    followup.add_followup(root, cfg, "beta", "One", "X")

    result = runner.invoke(app, ["task", "done", "T-01", "--closes", "FU-01", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["closed_followups"] == ["FU-01"]


def test_task_done_that_fails_to_close_still_refreshes_the_checkpoint(tmp_path, monkeypatch):
    root, cfg = _task_in_progress(tmp_path, monkeypatch)
    followup.add_followup(root, cfg, "beta", "One", "X")
    checkpoint = root / "docs" / "projects" / "alpha" / "checkpoint.md"
    checkpoint.unlink(missing_ok=True)

    def refuse(*args, **kwargs):
        # Another command closed it between the check and the close.
        raise SpecfloError("FU-01 in beta/followup is closed, not open.")

    monkeypatch.setattr(followup, "close_followup", refuse)
    result = runner.invoke(app, ["task", "done", "T-01", "--closes", "FU-01"])

    assert result.exit_code != 0
    assert "T-01 is done, but FU-01 was not closed" in result.output
    assert _progress(root, cfg) == "done"
    assert checkpoint.is_file()


def test_task_done_closes_refuses_a_daemon_hosted_project_without_asking_the_daemon(
    tmp_path, monkeypatch
):
    root = tmp_path
    cfg = config.init_config(root)
    # A remote nothing listens on: a request to it would fail with a
    # connection error, not the checkout-only refusal.
    config.add_remote(root, "home", "http://127.0.0.1:9", "s3cret")
    config.record_hosted_project(root, "demo", "home")
    cfg.active_project = "demo"
    config.save_config(root, cfg)
    monkeypatch.chdir(root)

    result = runner.invoke(app, ["task", "done", "T-01", "--closes", "FU-01"])

    assert result.exit_code != 0
    assert "checkout" in result.output
    assert "127.0.0.1:9" not in result.output


# --- list ---------------------------------------------------------------------


def _list(*args):
    return runner.invoke(app, ["followup", "list", *args])


def _open_closed_and_hand_written(tmp_path, monkeypatch):
    """FU-86 open in alpha, FU-87 closed in beta, FU-85 hand-written in gamma."""
    root = _checkout(tmp_path, monkeypatch, "Alpha", "Beta", "Gamma")
    cfg = config.load_config(root)
    (root / "docs" / "projects" / "gamma" / "followup.md").write_text(
        "# gamma\n\n### FU-85. A hand-written entry\n\nText.\n"
    )
    followup.add_followup(root, cfg, "alpha", "Open one", "Do the open one")
    followup.add_followup(root, cfg, "beta", "Closed one", "Do the closed one")
    followup.close_followup(root, cfg, "FU-87", "Done")
    return root


def test_list_shows_only_the_open_entries_with_project_id_title_and_do(tmp_path, monkeypatch):
    _open_closed_and_hand_written(tmp_path, monkeypatch)

    result = _list()

    assert result.exit_code == 0, result.output
    assert re.findall(r"FU-\d+", result.output) == ["FU-86"]
    heading = next(line for line in result.output.splitlines() if "FU-86" in line)
    assert "alpha" in heading and "Open one" in heading
    assert "Do the open one" in result.output
    assert "Closed one" not in result.output
    assert "hand-written" not in result.output


def test_list_all_adds_the_closed_entries(tmp_path, monkeypatch):
    _open_closed_and_hand_written(tmp_path, monkeypatch)

    result = _list("--all")

    assert result.exit_code == 0, result.output
    assert re.findall(r"FU-\d+", result.output) == ["FU-86", "FU-87"]
    heading = next(line for line in result.output.splitlines() if "FU-87" in line)
    assert "beta" in heading and "Closed one" in heading and "closed" in heading
    assert "Do the closed one" in result.output


def test_list_json_holds_the_same_entries_as_the_text(tmp_path, monkeypatch):
    _open_closed_and_hand_written(tmp_path, monkeypatch)

    for args in ([], ["--all"]):
        text = _list(*args)
        result = _list(*args, "--json")
        assert result.exit_code == 0, result.output
        entries = json.loads(result.output)
        assert [e["id"] for e in entries] == re.findall(r"FU-\d+", text.output), args
        for entry in entries:
            heading = next(line for line in text.output.splitlines() if entry["id"] in line)
            assert entry["project"] in heading and entry["title"] in heading, args
            assert entry["do"] in text.output, args

    assert json.loads(_list("--json").output) == [
        {
            "id": "FU-86",
            "project": "alpha",
            "title": "Open one",
            "do": "Do the open one",
            "from": None,
            "status": "open",
        }
    ]


def test_list_with_no_open_entries_says_so(tmp_path, monkeypatch):
    _checkout(tmp_path, monkeypatch, "Alpha")

    result = _list()

    assert result.exit_code == 0, result.output
    assert "No open follow-ups" in result.output
    assert json.loads(_list("--json").output) == []


# --- show ---------------------------------------------------------------------


def _show(*args):
    return runner.invoke(app, ["followup", "show", *args])


def test_show_prints_every_field_of_an_open_entry_in_another_project(tmp_path, monkeypatch):
    root = _checkout(tmp_path, monkeypatch, "Alpha", "Beta")
    cfg = config.load_config(root)
    followup.add_followup(root, cfg, "beta", "Open one", "Do it", source="review-1 F2")

    result = _show("FU-01")

    assert result.exit_code == 0, result.output
    assert result.output == (
        "FU-01  beta  Open one\n"
        "    Do: Do it\n"
        "    From: review-1 F2\n"
        "    Status: open\n"
    )
    assert json.loads(_show("FU-01", "--json").output) == {
        "id": "FU-01",
        "project": "beta",
        "title": "Open one",
        "do": "Do it",
        "from": "review-1 F2",
        "status": "open",
        "closed": None,
        "closed_by": None,
    }


def test_show_prints_the_closed_line_of_a_closed_entry(tmp_path, monkeypatch):
    _open_closed_and_hand_written(tmp_path, monkeypatch)
    today = datetime.date.today().isoformat()

    result = _show("FU-87")

    assert result.exit_code == 0, result.output
    assert result.output == (
        "FU-87  beta  Closed one\n"
        "    Do: Do the closed one\n"
        "    Status: closed\n"
        f"    Closed: {today}: Done\n"
    )
    assert json.loads(_show("FU-87", "--json").output) == {
        "id": "FU-87",
        "project": "beta",
        "title": "Closed one",
        "do": "Do the closed one",
        "from": None,
        "status": "closed",
        "closed": f"{today}: Done",
        "closed_by": None,
    }


def test_show_prints_the_closed_by_line_of_an_entry_closed_by_a_project(tmp_path, monkeypatch):
    root = _checkout(tmp_path, monkeypatch, "Alpha", "Beta")
    cfg = config.load_config(root)
    followup.add_followup(root, cfg, "beta", "Closed one", "Do it")
    followup.close_followup(root, cfg, "FU-01", "Done", by="alpha")
    today = datetime.date.today().isoformat()

    result = _show("FU-01")

    assert result.exit_code == 0, result.output
    assert result.output == (
        "FU-01  beta  Closed one\n"
        "    Do: Do it\n"
        "    Status: closed\n"
        f"    Closed: {today}: Done\n"
        "    Closed by: alpha\n"
    )
    shown = json.loads(_show("FU-01", "--json").output)
    assert shown["closed"] == f"{today}: Done"
    assert shown["closed_by"] == "alpha"


def test_show_refuses_an_unknown_or_hand_written_id(tmp_path, monkeypatch):
    _open_closed_and_hand_written(tmp_path, monkeypatch)

    for followup_id in ("FU-999", "FU-85"):
        result = _show(followup_id)
        assert result.exit_code != 0, followup_id
        assert f"No follow-up {followup_id}" in result.output


def test_show_warns_about_a_document_it_cannot_read_and_shows_the_entry(tmp_path, monkeypatch):
    root = _checkout(tmp_path, monkeypatch, "Alpha", "Beta")
    cfg = config.load_config(root)
    followup.add_followup(root, cfg, "alpha", "Readable", "X")
    followup.add_followup(root, cfg, "beta", "Broken", "Y")
    _break_document(root, "beta")

    result = _show("FU-01")

    assert result.exit_code == 0, result.output
    assert "Readable" in result.output
    assert "beta" in result.output and "not searched" in result.output


def test_show_takes_the_open_entry_when_two_projects_hold_the_id(tmp_path, monkeypatch):
    root = _checkout(tmp_path, monkeypatch, "Alpha", "Beta")
    assert _add("T", "--do", "X").exit_code == 0
    alpha, beta = (root / "docs" / "projects" / slug / "followup.md" for slug in ("alpha", "beta"))
    beta.write_text(alpha.read_text())
    assert _close("FU-01", "--note", "First").exit_code == 0

    shown = json.loads(_show("FU-01", "--json").output)

    assert shown["status"] == "open"
    assert shown["project"] in ("alpha", "beta")


# --- advance ------------------------------------------------------------------


def _quick_project_ready_to_complete(tmp_path, monkeypatch, *titles):
    """An active quick project 'thing' with a filled brief and one open follow-up
    per title, beside a project 'other' that holds an open follow-up of its own."""
    root = _checkout(tmp_path, monkeypatch, "Other")
    cfg = config.load_config(root)
    followup.add_followup(root, cfg, "other", "Elsewhere", "X")
    assert runner.invoke(app, ["new", "Thing", "--level", "quick"]).exit_code == 0
    for title, body in (("Goal", "Fix it.\n"), ("Done when", "- it works\n"), ("Proof", "ran: ok\n")):
        result = runner.invoke(app, ["section", "set", "brief", title, "--stdin"], input=body)
        assert result.exit_code == 0, result.output
    for title in titles:
        assert _add(title, "--do", "X").exit_code == 0
    assert _add("Already closed", "--do", "X").exit_code == 0
    closed = followup.list_followups(root, cfg)[-1].id
    followup.close_followup(root, cfg, closed, "Done")
    return root


def test_advance_lists_the_completing_projects_open_followups(tmp_path, monkeypatch):
    _quick_project_ready_to_complete(tmp_path, monkeypatch, "First left", "Second left")

    result = runner.invoke(app, ["advance"])

    assert result.exit_code == 0, result.output
    assert "Completed project 'thing'." in result.output
    assert "FU-02" in result.output and "First left" in result.output
    assert "FU-03" in result.output and "Second left" in result.output
    assert "specflo followup list" in result.output
    assert "Elsewhere" not in result.output
    assert "Already closed" not in result.output


def test_advance_json_carries_the_open_followups(tmp_path, monkeypatch):
    _quick_project_ready_to_complete(tmp_path, monkeypatch, "First left", "Second left")

    result = runner.invoke(app, ["advance", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["followups"] == [
        {"id": "FU-02", "title": "First left"},
        {"id": "FU-03", "title": "Second left"},
    ]


def test_advance_with_no_open_followups_prints_no_followup_line(tmp_path, monkeypatch):
    _quick_project_ready_to_complete(tmp_path, monkeypatch)

    result = runner.invoke(app, ["advance"])

    assert result.exit_code == 0, result.output
    assert "Completed project 'thing'." in result.output
    assert "follow" not in result.output.lower()
    assert not re.search(r"FU-\d+", result.output)


def test_an_unreadable_followup_document_does_not_fail_advance(tmp_path, monkeypatch):
    root = _quick_project_ready_to_complete(tmp_path, monkeypatch, "First left")
    (root / "docs" / "projects" / "other" / "followup.md").write_bytes(b"\xff\xfe broken")

    result = runner.invoke(app, ["advance"])

    assert result.exit_code == 0, result.output
    assert "Completed project 'thing'." in result.output


def test_advance_json_with_no_open_followups_has_an_empty_list(tmp_path, monkeypatch):
    _quick_project_ready_to_complete(tmp_path, monkeypatch)

    result = runner.invoke(app, ["advance", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["followups"] == []


# --- new ----------------------------------------------------------------------


def test_new_counts_the_open_followups_of_the_other_projects(tmp_path, monkeypatch):
    root = _checkout(tmp_path, monkeypatch, "Alpha", "Beta", "Gamma")
    cfg = config.load_config(root)
    (root / "docs" / "projects" / "gamma" / "followup.md").write_text(
        "# gamma\n\n### FU-85. A hand-written entry\n\nText.\n"
    )
    for slug, title in (("alpha", "One"), ("alpha", "Two"), ("beta", "Three"), ("beta", "Shut")):
        followup.add_followup(root, cfg, slug, title, "X")
    followup.close_followup(root, cfg, "FU-89", "Done")

    result = runner.invoke(app, ["new", "X"])

    assert result.exit_code == 0, result.output
    line = next(line for line in result.output.splitlines() if "specflo followup list" in line)
    assert re.findall(r"\d+", line) == ["3"]


def test_an_unreadable_followup_document_does_not_fail_new(tmp_path, monkeypatch):
    root = _checkout(tmp_path, monkeypatch, "Alpha")
    (root / "docs" / "projects" / "alpha" / "followup.md").write_bytes(b"\xff\xfe broken")

    result = runner.invoke(app, ["new", "X"])

    assert result.exit_code == 0, result.output
    assert "Created project 'x'" in result.output


def test_new_with_no_open_followups_prints_no_followup_line(tmp_path, monkeypatch):
    root = _checkout(tmp_path, monkeypatch, "Alpha")
    followup.add_followup(root, config.load_config(root), "alpha", "Shut", "X")
    followup.close_followup(root, config.load_config(root), "FU-01", "Done")

    result = runner.invoke(app, ["new", "X"])

    assert result.exit_code == 0, result.output
    assert "follow" not in result.output.lower()


# --- the document is the verbs' own ---------------------------------------------


def test_section_set_refuses_the_followup_document(tmp_path, monkeypatch):
    root = _checkout(tmp_path, monkeypatch, "Alpha")
    assert _add("T", "--do", "X").exit_code == 0
    before = _doc(root, "alpha")

    result = runner.invoke(
        app, ["section", "set", "followup", "Follow-ups", "--stdin"], input="replaced\n"
    )

    assert result.exit_code != 0
    assert _doc(root, "alpha") == before


# --- daemon-hosted projects ---------------------------------------------------


def test_add_refuses_a_daemon_hosted_project_without_asking_the_daemon(tmp_path, monkeypatch):
    root = tmp_path
    cfg = config.init_config(root)
    # A remote nothing listens on: a request to it would fail with a
    # connection error, not the checkout-only refusal.
    config.add_remote(root, "home", "http://127.0.0.1:9", "s3cret")
    config.record_hosted_project(root, "demo", "home")
    cfg.active_project = "demo"
    config.save_config(root, cfg)
    monkeypatch.chdir(root)

    result = _add("T", "--do", "X")

    assert result.exit_code != 0
    assert "checkout" in result.output
    assert "127.0.0.1:9" not in result.output
    assert not re.search(r"FU-\d+", result.output)

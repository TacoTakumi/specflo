"""Follow-ups: the work a project leaves for a later one.

Each project keeps its follow-ups in its own followup.md, created on the first
``specflo followup add``. The FU-NN numbers run across the whole projects
directory, hand-written documents included, so any project can cite an entry
with no project prefix.
"""

import multiprocessing
import re

from typer.testing import CliRunner

from specflo import config, followup, projects
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
        "# beta\n\n### FU-85. A flaky test\n\n- **FU-89 (O2).** Another one\n"
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
    assert not (root / "docs" / "projects" / "alpha" / "followup.md").exists()


def test_add_prints_the_id_as_json(tmp_path, monkeypatch):
    _checkout(tmp_path, monkeypatch, "Alpha")

    result = _add("T", "--do", "X", "--json")

    assert result.exit_code == 0, result.output
    assert '"id": "FU-01"' in result.output
    assert '"project": "alpha"' in result.output


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

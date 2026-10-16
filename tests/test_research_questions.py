"""The research question commands: add an open question and resolve it.

A question is a Q-NN entry of the questions ledger: who answers it (the user
or a developer), its options, default and why, and a Status of open or
resolved. A developer question needs a default so the work goes on when
nobody answers. A later question supersedes an older one through the
ledger's own marks; resolve records the resolution and who gave it. Every
refusal leaves the checkout byte-identical and only the CLI writes the ledger.
"""

import datetime

import pytest
from typer.testing import CliRunner

from specflo import config, guide
from specflo.cli import app
from specflo.errors import SpecfloError
from specflo.research import document, ledger, questions

runner = CliRunner()

TODAY = datetime.date.today().isoformat()
BROKER = "A broker with twenty years in commercial real estate."
QUESTION_ADD = ["research", "question", "add"]
QUESTION_RESOLVE = ["research", "question", "resolve"]
USER_QUESTION = [*QUESTION_ADD, "--text", "Who signs the lease?", "--by", "user"]
DEVELOPER_QUESTION = [
    *QUESTION_ADD, "--text", "Which market first?", "--by", "developer",
    "--option", "Toronto", "--option", "Calgary", "--default", "Toronto",
    "--why", "the user lives there",
]
EVERY_COMMAND = [
    USER_QUESTION,
    [*QUESTION_RESOLVE, "Q-01", "--resolution", "The tenant signs."],
]


def _ok(args, stdin=None):
    result = runner.invoke(app, args, input=stdin)
    assert result.exit_code == 0, (args, result.output)
    return result


def _refused(args):
    result = runner.invoke(app, args)
    assert result.exit_code != 0, (args, result.output)
    return result.output


def _checkout(path, monkeypatch):
    path.mkdir()
    config.init_config(path)
    monkeypatch.chdir(path)
    return path


def _research_project(tmp_path, monkeypatch):
    root = _checkout(tmp_path / "local", monkeypatch)
    _ok(["new", "Thing", "--level", "research"])
    return root


def _tree(directory):
    """Every file under ``directory`` with its bytes, keyed by relative path."""
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _entries(root, slug="thing"):
    return {e.id: e for e in ledger.QUESTIONS.entries(root, config.load_config(root), slug)}


def _ledger_text(root, slug="thing"):
    return ledger.QUESTIONS.path(root, config.load_config(root), slug).read_text()


# --- question add -------------------------------------------------------------


def test_add_records_a_user_question_open_with_no_default_needed(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)

    shown = _ok([*USER_QUESTION, "--why", "the lease names a signer"]).output

    (entry,) = _entries(root).values()
    assert entry.id == "Q-01"
    assert entry.title == "Who signs the lease?"
    assert entry.fields == (("By", "user"), ("Why", "the lease names a signer"), ("Status", "open"))
    assert entry.status == "open" and entry.active
    assert entry.supersedes is None and entry.superseded_by is None
    assert "Q-01" in shown and "thing" in shown and "open" in shown
    assert (
        "### Q-01 - Who signs the lease?\n- By: user\n- Why: the lease names a signer\n"
        "- Status: open\n"
    ) in _ledger_text(root)


def test_add_repeats_each_option_and_keeps_the_field_order(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)

    _ok([
        *USER_QUESTION, "--option", "The tenant", "--option", "The landlord",
        "--option", "Both", "--default", "The tenant",
    ])

    entry = _entries(root)["Q-01"]
    assert entry.all("Option") == ["The tenant", "The landlord", "Both"]
    assert entry.fields == (
        ("By", "user"), ("Option", "The tenant"), ("Option", "The landlord"),
        ("Option", "Both"), ("Default", "The tenant"), ("Status", "open"),
    )


def test_add_records_a_developer_question_with_its_default(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)

    shown = _ok(DEVELOPER_QUESTION).output

    entry = _entries(root)["Q-01"]
    assert entry.fields == (
        ("By", "developer"), ("Option", "Toronto"), ("Option", "Calgary"),
        ("Default", "Toronto"), ("Why", "the user lives there"), ("Status", "open"),
    )
    assert "developer" in shown


def test_add_refuses_a_developer_question_without_a_default_and_writes_nothing(
    tmp_path, monkeypatch
):
    root = _research_project(tmp_path, monkeypatch)
    before = _tree(root)

    shown = _refused([
        *QUESTION_ADD, "--text", "Which market first?", "--by", "developer",
        "--option", "Toronto", "--option", "Calgary",
    ])

    assert "default" in shown.lower() and "developer" in shown
    assert "Traceback" not in shown
    assert _tree(root) == before
    assert _entries(root) == {}


def test_add_refuses_bad_input_before_it_writes(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    # One add first: the lock file it leaves under .specflo/locks is the lock's
    # own, so the snapshot holds it and a refusal that reads the ledger under
    # the lock still leaves the tree as it was.
    _ok(USER_QUESTION)
    before = _tree(root)

    for args in (
        [*QUESTION_ADD, "--text", "Who signs?"],
        [*QUESTION_ADD, "--by", "user"],
        [*QUESTION_ADD, "--text", "Who signs?", "--by", "nobody"],
        [*QUESTION_ADD, "--text", "Who signs?", "--by", "User"],
        [*QUESTION_ADD, "--text", "", "--by", "user"],
        [*QUESTION_ADD, "--text", "   ", "--by", "user"],
        [*QUESTION_ADD, "--text", "one\ntwo", "--by", "user"],
        [*USER_QUESTION, "--option", "a\nb"],
        [*USER_QUESTION, "--default", ""],
        [*USER_QUESTION, "--why", "x\ny"],
        [*USER_QUESTION, "--supersedes", "Q-05"],
        [*USER_QUESTION, "--supersedes", "S-01"],
    ):
        shown = _refused(args)
        assert "Traceback" not in shown, args
    assert _tree(root) == before
    assert list(_entries(root)) == ["Q-01"]


def test_add_with_supersedes_marks_the_old_question_and_links_the_new_one(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    _ok(USER_QUESTION)

    shown = _ok([
        *QUESTION_ADD, "--text", "Who signs the lease, and who witnesses it?", "--by", "user",
        "--supersedes", "Q-01",
    ]).output

    entries = _entries(root)
    old, new = entries["Q-01"], entries["Q-02"]
    assert old.superseded_by == "Q-02" and not old.active
    assert old.status == "open"
    assert new.supersedes == "Q-01" and new.active and new.status == "open"
    assert new.fields == (("By", "user"), ("Status", "open"), ("Supersedes", "Q-01"))
    assert "Q-02" in shown and "Q-01" in shown
    cfg = config.load_config(root)
    assert [q.id for q in questions.open_questions(root, cfg, "thing")] == ["Q-02"]

    before = _tree(root)
    shown = _refused([*USER_QUESTION, "--supersedes", "Q-01"])
    assert "Q-01" in shown and "Q-02" in shown
    assert _tree(root) == before


# --- question resolve ---------------------------------------------------------


def test_resolve_records_the_status_the_resolution_and_the_name(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    _ok(DEVELOPER_QUESTION)

    shown = _ok([*QUESTION_RESOLVE, "Q-01", "--resolution", "Toronto first.", "--by", "Ana"]).output

    entry = _entries(root)["Q-01"]
    assert entry.status == "resolved"
    assert entry.get("Resolution") == "Toronto first."
    assert entry.get("Resolved by") == "Ana"
    assert entry.fields == (
        ("By", "developer"), ("Option", "Toronto"), ("Option", "Calgary"),
        ("Default", "Toronto"), ("Why", "the user lives there"), ("Status", "resolved"),
        ("Resolution", "Toronto first."), ("Resolved by", "Ana"),
    )
    assert "Q-01" in shown and "Ana" in shown
    cfg = config.load_config(root)
    assert questions.open_questions(root, cfg, "thing") == []
    assert (
        "- Status: resolved\n- Resolution: Toronto first.\n- Resolved by: Ana\n"
    ) in _ledger_text(root)


def test_resolve_names_the_usual_user_by_default(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    _ok(USER_QUESTION)
    _ok(USER_QUESTION)

    _ok([*QUESTION_RESOLVE, "Q-01", "--resolution", "The tenant signs."])
    assert _entries(root)["Q-01"].get("Resolved by") == document.DEFAULT_PRESENT == "the usual user"

    _ok(["research", "user", "set", "--text", f"{BROKER}\n\nShe knows every landlord downtown."])
    _ok([*QUESTION_RESOLVE, "Q-02", "--resolution", "The landlord signs."])
    assert _entries(root)["Q-02"].get("Resolved by") == BROKER


def test_resolve_refuses_an_unknown_superseded_or_resolved_question_and_writes_nothing(
    tmp_path, monkeypatch
):
    root = _research_project(tmp_path, monkeypatch)
    _ok(USER_QUESTION)
    _ok([*USER_QUESTION, "--supersedes", "Q-01"])
    _ok([*QUESTION_ADD, "--text", "How long is the term?", "--by", "user"])
    _ok([*QUESTION_RESOLVE, "Q-03", "--resolution", "Five years."])
    before = _tree(root)

    for args, expected in (
        ([*QUESTION_RESOLVE, "Q-09", "--resolution", "x"], "Q-09"),
        ([*QUESTION_RESOLVE, "Q-01", "--resolution", "x"], "Q-02"),
        ([*QUESTION_RESOLVE, "Q-03", "--resolution", "x"], "resolved"),
        ([*QUESTION_RESOLVE, "S-01", "--resolution", "x"], "S-01"),
        ([*QUESTION_RESOLVE, "Q-02"], None),
        ([*QUESTION_RESOLVE, "Q-02", "--resolution", ""], None),
        ([*QUESTION_RESOLVE, "Q-02", "--resolution", "one\ntwo"], None),
        ([*QUESTION_RESOLVE, "Q-02", "--resolution", "x", "--by", ""], None),
        ([*QUESTION_RESOLVE, "Q-02", "--resolution", "x", "--by", "a\nb"], None),
    ):
        shown = _refused(args)
        assert "Traceback" not in shown, args
        if expected is not None:
            assert expected in shown, args
    assert _tree(root) == before


# --- which project the commands work on ---------------------------------------


def test_the_commands_refuse_a_project_without_a_research_document(tmp_path, monkeypatch):
    root = _checkout(tmp_path / "local", monkeypatch)
    _ok(["new", "Plain"])
    before = _tree(root)

    for args in EVERY_COMMAND:
        shown = _refused(args)
        assert "plain" in shown and document.RESEARCH_FILENAME in shown, args
        assert "Traceback" not in shown, args
    assert _tree(root) == before


def test_the_commands_need_a_checkout_and_an_active_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for args in EVERY_COMMAND:
        assert "specflo init" in _refused(args), args

    root = _checkout(tmp_path / "local", monkeypatch)
    before = _tree(root)
    for args in EVERY_COMMAND:
        assert "No active project" in _refused(args), args
    assert _tree(root) == before


# --- the functions on their own -------------------------------------------------


def test_the_functions_validate_before_they_touch_the_ledger(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    cfg = config.load_config(root)
    path = ledger.QUESTIONS.path(root, cfg, "thing")
    before = path.read_bytes()

    with pytest.raises(SpecfloError, match="developer"):
        questions.add_question(root, cfg, "thing", "Which?", "developer", today=TODAY)
    with pytest.raises(SpecfloError, match="nobody"):
        questions.add_question(root, cfg, "thing", "Which?", "nobody", today=TODAY)
    with pytest.raises(SpecfloError):
        questions.add_question(root, cfg, "thing", "", "user", today=TODAY)
    with pytest.raises(SpecfloError):
        questions.add_question(root, cfg, "thing", "one\ntwo", "user", today=TODAY)
    with pytest.raises(SpecfloError, match="Q-07"):
        questions.resolve_question(root, cfg, "thing", "Q-07", "x", today=TODAY)
    with pytest.raises(SpecfloError):
        questions.resolve_question(root, cfg, "thing", "Q-01", "", today=TODAY)
    assert path.read_bytes() == before

    path.unlink()
    with pytest.raises(SpecfloError, match="questions"):
        questions.add_question(root, cfg, "thing", "Which?", "user", today=TODAY)
    with pytest.raises(SpecfloError, match="questions"):
        questions.resolve_question(root, cfg, "thing", "Q-01", "x", today=TODAY)
    assert not path.exists()


def test_the_functions_return_the_entry_and_the_typed_view_reads_it(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    cfg = config.load_config(root)

    added = questions.add_question(
        root, cfg, "thing", "Which market first?", "developer",
        options=("Toronto", "Calgary"), default="Toronto", why="the user lives there",
        today=TODAY,
    )
    assert added.id == "Q-01" and added.status == "open"
    view = questions.question_of(added)
    assert (view.id, view.text, view.by) == ("Q-01", "Which market first?", "developer")
    assert view.options == ("Toronto", "Calgary")
    assert (view.default, view.why, view.status) == ("Toronto", "the user lives there", "open")
    assert view.resolution is None and view.resolved_by is None and view.open

    resolved = questions.resolve_question(
        root, cfg, "thing", "Q-01", "Toronto first.", by="Ana", today="2026-10-17"
    )
    view = questions.question_of(resolved)
    assert (view.status, view.resolution, view.resolved_by) == ("resolved", "Toronto first.", "Ana")
    assert not view.open
    assert "updated: 2026-10-17\n" in _ledger_text(root)


# --- the guide ------------------------------------------------------------------


def test_each_question_command_has_a_guide_row_under_the_research_group():
    rows = {entry["name"]: entry for entry in guide.COMMANDS if entry["group"] == "research"}

    assert {"research question add", "research question resolve"} <= set(rows)
    add = rows["research question add"]["args"]
    for part in ("--text <text>", "--by user|developer", "[--option <text>]...",
                 "[--default <text>]", "[--why <text>]", "[--supersedes Q-NN]"):
        assert part in add, part
    assert "<Q-NN> --resolution <text> [--by <name>]" in rows["research question resolve"]["args"]
    for row in rows.values():
        assert row["summary"].isascii() and row["args"].isascii()
    names = [entry["name"] for entry in guide.COMMANDS]
    assert names.index("research user set") < names.index("research question add")
    assert names.index("research question resolve") < names.index("research destination set")


def test_the_guide_text_shows_both_question_rows(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    out = _ok(["guide"]).output

    for name in ("research question add", "research question resolve"):
        assert f"\n    {name} " in out, name
    assert out.index("\n    research user set ") < out.index("\n    research question add ")
    assert out.index("\n    research question resolve ") < out.index("\n    research destination set ")

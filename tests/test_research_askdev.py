"""The ``research ask-dev`` command: open developer questions as email text.

A developer question waits for someone who is not at the keyboard, so the
user carries it over by email. The command prints a subject line and a
plain-text body with every open developer question, its options, default and
why; a user question, a resolved one and a superseded one stay out. It reads
the project and writes nothing, sends nothing: the user pastes the text into
their own mail client. With no open developer question it says so in one line.
"""

import datetime
import sys

from typer.testing import CliRunner

from specflo import config, guide
from specflo.cli import app
from specflo.research import askdev, document, questions

runner = CliRunner()

TODAY = datetime.date.today().isoformat()
ASK_DEV = ["research", "ask-dev"]
QUESTION_ADD = ["research", "question", "add"]
QUESTION_RESOLVE = ["research", "question", "resolve"]
MARKET_QUESTION = [
    *QUESTION_ADD, "--text", "Which market first?", "--by", "developer",
    "--option", "Toronto", "--option", "Calgary", "--default", "Toronto",
    "--why", "the user lives there",
]
SOURCES_QUESTION = [
    *QUESTION_ADD, "--text", "How many sources per claim?", "--by", "developer",
    "--default", "two",
]
USER_QUESTION = [*QUESTION_ADD, "--text", "Who signs the lease?", "--by", "user"]
NO_OPEN = "No open developer questions."
SUBJECT_FOR_TWO = "Research questions for Thing (2 open)"


def _ok(args):
    result = runner.invoke(app, args)
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


# --- the command ------------------------------------------------------------


def test_ask_dev_prints_a_subject_and_every_open_developer_question(tmp_path, monkeypatch):
    _research_project(tmp_path, monkeypatch)
    _ok(MARKET_QUESTION)
    _ok(SOURCES_QUESTION)
    _ok(USER_QUESTION)

    out = _ok(ASK_DEV).output

    assert out.startswith(f"Subject: {SUBJECT_FOR_TWO}\n\n")
    body = out.split("\n\n", 1)[1]
    assert "1. Q-01: Which market first?\n" in body
    assert "   Options:\n   - Toronto\n   - Calgary\n" in body
    assert "   Default: Toronto\n" in body
    assert "   Why: the user lives there\n" in body
    assert "2. Q-02: How many sources per claim?\n" in body
    assert "   Default: two\n" in body
    assert body.index("Q-01") < body.index("Q-02")
    assert "one sitting" in body
    assert "Who signs the lease?" not in out
    assert "Q-03" not in out
    assert "Options: none" not in out
    assert out.isascii()
    assert all(len(line) <= 78 for line in out.splitlines())


def test_ask_dev_omits_a_resolved_and_a_superseded_developer_question(tmp_path, monkeypatch):
    _research_project(tmp_path, monkeypatch)
    _ok(MARKET_QUESTION)
    _ok([*QUESTION_RESOLVE, "Q-01", "--resolution", "Toronto first.", "--by", "Ana"])
    _ok([*QUESTION_ADD, "--text", "Which lease act applies?", "--by", "developer",
         "--default", "Ontario"])
    _ok([*QUESTION_ADD, "--text", "Which lease act, and which year of it?", "--by", "developer",
         "--default", "Ontario, 2024", "--supersedes", "Q-02"])
    _ok(SOURCES_QUESTION)

    out = _ok(ASK_DEV).output

    assert out.startswith(f"Subject: {SUBJECT_FOR_TWO}\n\n")
    assert "1. Q-03: Which lease act, and which year of it?\n" in out
    assert "2. Q-04: How many sources per claim?\n" in out
    assert "Q-01" not in out and "Which market first?" not in out
    assert "Q-02" not in out and "Which lease act applies?" not in out


def test_ask_dev_says_so_with_no_open_developer_question(tmp_path, monkeypatch):
    _research_project(tmp_path, monkeypatch)
    assert _ok(ASK_DEV).output == f"{NO_OPEN}\n"

    _ok(USER_QUESTION)
    assert _ok(ASK_DEV).output == f"{NO_OPEN}\n"

    _ok(MARKET_QUESTION)
    _ok([*QUESTION_RESOLVE, "Q-02", "--resolution", "Toronto first."])
    assert _ok(ASK_DEV).output == f"{NO_OPEN}\n"


def test_ask_dev_changes_no_file(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    before = _tree(root)
    _ok(ASK_DEV)
    assert _tree(root) == before

    _ok(MARKET_QUESTION)
    _ok(USER_QUESTION)
    before = _tree(root)
    _ok(ASK_DEV)
    _ok(ASK_DEV)
    assert _tree(root) == before


def test_ask_dev_refuses_a_project_without_a_research_document(tmp_path, monkeypatch):
    root = _checkout(tmp_path / "local", monkeypatch)
    _ok(["new", "Plain"])
    before = _tree(root)

    shown = _refused(ASK_DEV)

    assert "plain" in shown and document.RESEARCH_FILENAME in shown
    assert "Traceback" not in shown
    assert _tree(root) == before


def test_ask_dev_needs_a_checkout_and_an_active_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert "specflo init" in _refused(ASK_DEV)

    root = _checkout(tmp_path / "local", monkeypatch)
    before = _tree(root)
    assert "No active project" in _refused(ASK_DEV)
    assert _tree(root) == before


# --- the function on its own ---------------------------------------------------


def test_render_ask_dev_gives_the_subject_and_an_ascii_body_wrapped_to_78_columns(
    tmp_path, monkeypatch
):
    root = _research_project(tmp_path, monkeypatch)
    cfg = config.load_config(root)
    long_text = " ".join(f"word{n}" for n in range(30)) + "?"
    long_why = " ".join(["because"] * 25)
    questions.add_question(
        root, cfg, "thing", long_text, "developer",
        options=("Toronto", " ".join(["Calgary"] * 15)), default="Toronto", why=long_why,
        today=TODAY,
    )
    questions.add_question(root, cfg, "thing", "Who signs the lease?", "user", today=TODAY)
    questions.add_question(
        root, cfg, "thing", "How many sources per claim?", "developer", default="two",
        today=TODAY,
    )

    subject, body = askdev.render_ask_dev(root, cfg, "thing")

    assert subject == SUBJECT_FOR_TWO
    assert subject.isascii() and body.isascii()
    lines = body.splitlines()
    assert lines and all(len(line) <= 78 for line in lines), max(lines, key=len)
    assert any(len(line) > 60 for line in lines)
    # A long question wraps; its continuation lines keep the block's indent,
    # as every indented line of the body does.
    first = next(i for i, line in enumerate(lines) if line.startswith("1. Q-01: "))
    assert lines[first + 1].startswith("   word")
    assert all(line.startswith("   ") for line in lines if line.startswith(" "))
    assert body.count("Default: Toronto") == 1 and body.count("Default: two") == 1
    assert "Who signs the lease?" not in body
    assert not body.endswith("\n")
    assert "\n\n\n" not in body


def test_render_ask_dev_with_nothing_open_returns_no_subject_and_the_one_line(
    tmp_path, monkeypatch
):
    root = _research_project(tmp_path, monkeypatch)
    cfg = config.load_config(root)
    assert askdev.render_ask_dev(root, cfg, "thing") == ("", askdev.NO_OPEN_MESSAGE)
    assert askdev.NO_OPEN_MESSAGE == NO_OPEN
    assert askdev.open_developer_questions(root, cfg, "thing") == []


def test_askdev_module_imports_nothing_that_reaches_the_network():
    imported = {
        value.__name__.split(".")[0]
        for value in vars(askdev).values()
        if isinstance(value, type(sys))
    }
    assert not imported & {"socket", "smtplib", "http", "urllib", "httpx", "requests"}


# --- the guide ----------------------------------------------------------------


def test_the_guide_has_the_ask_dev_row_after_question_resolve(tmp_path, monkeypatch):
    rows = {entry["name"]: entry for entry in guide.COMMANDS if entry["group"] == "research"}
    assert "research ask-dev" in rows
    row = rows["research ask-dev"]
    assert row["args"] == ""
    assert row["summary"].isascii()
    assert "email" in row["summary"].lower()
    names = [entry["name"] for entry in guide.COMMANDS]
    assert names.index("research question resolve") + 1 == names.index("research ask-dev")
    assert names.index("research ask-dev") < names.index("research destination set")

    monkeypatch.chdir(tmp_path)
    out = _ok(["guide"]).output
    assert "\n    research ask-dev " in out
    assert out.index("\n    research question resolve ") < out.index("\n    research ask-dev ")
    assert out.index("\n    research ask-dev ") < out.index("\n    research destination set ")

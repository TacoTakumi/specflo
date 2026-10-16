"""The research session commands: the usual user, the destination and session close.

``specflo research`` works on the active project when it has a research
document, whatever its level: a research project moved up to full keeps its
research files and the commands keep working on it. Every refusal leaves the
checkout byte-identical, and the only writer of ``research.md`` is the CLI.
"""

import datetime

import pytest
from typer.testing import CliRunner

from specflo import config, guide
from specflo.cli import app
from specflo.errors import SpecfloError
from specflo.research import document

runner = CliRunner()

TODAY = datetime.date.today().isoformat()
BROKER = "A broker with twenty years in commercial real estate."
REFUSED_TEXT = "the research commands"


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


def _parsed(root, slug="thing"):
    return document.load(root, config.load_config(root), slug)


def _doc(root, slug="thing"):
    return document.research_path(root, config.load_config(root), slug).read_text()


USER_SET = ["research", "user", "set"]
DESTINATION_SET = ["research", "destination", "set"]
SESSION_CLOSE = ["research", "session", "close"]
EVERY_COMMAND = [
    [*USER_SET, "--text", BROKER],
    [*DESTINATION_SET, "build", "--reason", "the user wants a tool"],
    [*SESSION_CLOSE, "--summary", "Read the act", "--next", "Ask about renewals"],
]


# --- user set -----------------------------------------------------------------


def test_user_set_records_the_text_and_keeps_the_managed_note(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)

    shown = _ok([*USER_SET, "--text", BROKER]).output

    parsed = _parsed(root)
    assert parsed.usual_user == BROKER
    assert parsed.log == ()
    assert "usual user" in shown and "thing" in shown
    doc = _doc(root)
    assert "<!-- managed by the research commands: who usually sits at the keyboard" in doc
    assert (
        "## Usual user\n\n<!-- managed by the research commands: who usually sits at the"
        f" keyboard, in their own words -->\n{BROKER}\n\n## Decisions"
    ) in doc


def test_user_set_again_replaces_the_text_and_the_log_keeps_the_old_one(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    _ok([*USER_SET, "--text", BROKER])

    shown = _ok([*USER_SET, "--text", "A property manager who runs forty buildings."]).output

    parsed = _parsed(root)
    assert parsed.usual_user == "A property manager who runs forty buildings."
    assert len(parsed.log) == 1
    entry = parsed.log[0]
    assert (entry.date, entry.kind) == (TODAY, document.USER_KIND)
    assert BROKER in entry.text
    assert BROKER not in _doc(root).split("## Log")[0]
    assert "log" in shown.lower()


def test_user_set_from_a_file_takes_several_lines_and_logs_them_on_one(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    description = tmp_path / "user.txt"
    description.write_text("A broker.\n\nTwenty years in the trade.\n")

    _ok([*USER_SET, "--file", str(description)])
    assert _parsed(root).usual_user == "A broker.\n\nTwenty years in the trade."

    _ok([*USER_SET, "--text", "Someone else."])
    parsed = _parsed(root)
    assert parsed.usual_user == "Someone else."
    (entry,) = parsed.log
    assert entry.kind == document.USER_KIND
    assert "A broker. Twenty years in the trade." in entry.text
    assert "\n" not in entry.text


def test_user_set_refuses_both_or_neither_option_and_an_unreadable_file(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    before = _tree(root)

    for args in (
        USER_SET,
        [*USER_SET, "--text", BROKER, "--file", str(tmp_path / "user.txt")],
        [*USER_SET, "--file", str(tmp_path / "missing.txt")],
        [*USER_SET, "--text", ""],
        [*USER_SET, "--text", "   "],
        [*USER_SET, "--text", "A broker.\n## Decisions\nnot a section"],
    ):
        shown = _refused(args)
        assert "Traceback" not in shown, args
        assert _tree(root) == before, args


# --- destination set ----------------------------------------------------------


def test_destination_set_records_value_and_reason_and_logs_each_change(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)

    first = _ok([*DESTINATION_SET, "build", "--reason", "the user wants a tool"]).output
    parsed = _parsed(root)
    assert (parsed.destination, parsed.reason) == ("build", "the user wants a tool")
    assert "unknown -> build" in first

    _ok([*DESTINATION_SET, "inform", "--reason", "a report is enough for now"])

    parsed = _parsed(root)
    assert (parsed.destination, parsed.reason) == ("inform", "a report is enough for now")
    assert [(e.date, e.kind) for e in parsed.log] == [
        (TODAY, document.DESTINATION_KIND), (TODAY, document.DESTINATION_KIND),
    ]
    assert "unknown" in parsed.log[0].text and "build" in parsed.log[0].text
    assert "the user wants a tool" in parsed.log[0].text
    assert "build" in parsed.log[1].text and "inform" in parsed.log[1].text
    assert "a report is enough for now" in parsed.log[1].text
    doc = _doc(root)
    assert (
        "## Destination\n\n<!-- managed by the research commands: build, inform, learn or unknown,"
        " with the reason of the last change -->\n- Destination: inform\n"
        "- Reason: a report is enough for now\n\n## Usual user"
    ) in doc


@pytest.mark.parametrize("value", document.DESTINATIONS)
def test_destination_set_takes_each_of_the_four_values(tmp_path, monkeypatch, value):
    root = _research_project(tmp_path, monkeypatch)

    _ok([*DESTINATION_SET, value, "--reason", "because"])

    assert (_parsed(root).destination, _parsed(root).reason) == (value, "because")


def test_destination_set_refuses_another_value_or_no_reason_and_writes_nothing(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    before = _tree(root)

    shown = _refused([*DESTINATION_SET, "ship", "--reason", "we want to ship"])
    assert "ship" in shown and "build" in shown and "inform" in shown
    for args in (
        [*DESTINATION_SET, "build"],
        [*DESTINATION_SET, "build", "--reason", ""],
        [*DESTINATION_SET, "build", "--reason", "   "],
        [*DESTINATION_SET, "build", "--reason", "one\ntwo"],
        [*DESTINATION_SET, "Build", "--reason", "case matters"],
        [*DESTINATION_SET, "--reason", "no value"],
    ):
        shown = _refused(args)
        assert "Traceback" not in shown, args
    assert _tree(root) == before


# --- session close ------------------------------------------------------------


def test_session_close_appends_one_dated_entry_with_the_usual_user_present_by_default(
    tmp_path, monkeypatch
):
    root = _research_project(tmp_path, monkeypatch)

    shown = _ok([*SESSION_CLOSE, "--summary", "Read the act", "--next", "Ask about renewals"]).output

    parsed = _parsed(root)
    (entry,) = parsed.log
    assert (entry.date, entry.kind) == (TODAY, document.SESSION_KIND)
    session = document.parse_session(entry)
    assert (session.summary, session.next_steps) == ("Read the act", "Ask about renewals")
    assert session.present == document.DEFAULT_PRESENT == "the usual user"
    assert "the usual user" in shown
    assert entry.text == "Summary: Read the act | Next: Ask about renewals | Present: the usual user"


def test_session_close_names_the_recorded_user_by_the_first_line_of_the_description(
    tmp_path, monkeypatch
):
    root = _research_project(tmp_path, monkeypatch)
    _ok([*USER_SET, "--text", f"{BROKER}\n\nShe knows every landlord downtown."])

    _ok([*SESSION_CLOSE, "--summary", "Read the act", "--next", "Ask about renewals"])

    session = document.last_session(_parsed(root))
    assert session.present == BROKER


def test_session_close_shortens_a_long_description_to_name_who_was_present(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    long_line = " ".join(["word"] * 40)
    _ok([*USER_SET, "--text", long_line])

    _ok([*SESSION_CLOSE, "--summary", "s", "--next", "n"])

    present = document.last_session(_parsed(root)).present
    assert present.endswith("...") and len(present) <= document.PRESENT_WIDTH
    assert long_line.startswith(present[:-3].rstrip())


def test_session_close_with_present_records_who_was_there_and_the_last_session_wins(
    tmp_path, monkeypatch
):
    root = _research_project(tmp_path, monkeypatch)
    _ok([*SESSION_CLOSE, "--summary", "First", "--next", "Second", "--present", "Rob and Ana"])

    shown = _ok([*SESSION_CLOSE, "--summary", "Second", "--next", "Third", "--present", "Ana"]).output

    parsed = _parsed(root)
    assert [e.kind for e in parsed.log] == [document.SESSION_KIND, document.SESSION_KIND]
    assert document.parse_session(parsed.log[0]).present == "Rob and Ana"
    last = document.last_session(parsed)
    assert (last.summary, last.next_steps, last.present, last.date) == ("Second", "Third", "Ana", TODAY)
    assert "Ana" in shown


def test_last_session_is_none_before_a_session_closed_and_skips_other_kinds(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    assert document.last_session(_parsed(root)) is None

    _ok([*DESTINATION_SET, "learn", "--reason", "curiosity"])

    assert document.last_session(_parsed(root)) is None
    with pytest.raises(SpecfloError):
        document.parse_session(_parsed(root).log[0])


def test_session_close_refuses_an_empty_field_or_the_separator_and_writes_nothing(
    tmp_path, monkeypatch
):
    root = _research_project(tmp_path, monkeypatch)
    before = _tree(root)

    for args in (
        [*SESSION_CLOSE, "--summary", "Read the act"],
        [*SESSION_CLOSE, "--next", "Ask about renewals"],
        [*SESSION_CLOSE, "--summary", "", "--next", "n"],
        [*SESSION_CLOSE, "--summary", "s", "--next", "  "],
        [*SESSION_CLOSE, "--summary", "one\ntwo", "--next", "n"],
        [*SESSION_CLOSE, "--summary", "a | Next: b", "--next", "n"],
        [*SESSION_CLOSE, "--summary", "s", "--next", "n", "--present", ""],
        [*SESSION_CLOSE, "--summary", "s", "--next", "n", "--present", "Rob | Present: Ana"],
    ):
        shown = _refused(args)
        assert "Traceback" not in shown, args
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


def test_the_commands_work_on_a_project_with_the_document_at_another_level(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    project_md = root / "docs" / "projects" / "thing" / "project.md"
    project_md.write_text(project_md.read_text().replace("\nlevel: research\n", "\nlevel: full\n"))
    assert "Level: full" in _ok(["status"]).output

    for args in EVERY_COMMAND:
        _ok(args)

    parsed = _parsed(root)
    assert parsed.usual_user == BROKER
    assert parsed.destination == "build"
    assert document.last_session(parsed).summary == "Read the act"


def test_the_commands_need_a_checkout_and_an_active_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for args in EVERY_COMMAND:
        assert "specflo init" in _refused(args), args

    root = _checkout(tmp_path / "local", monkeypatch)
    before = _tree(root)
    for args in EVERY_COMMAND:
        assert "No active project" in _refused(args), args
    assert _tree(root) == before


# --- the document functions on their own -----------------------------------------


def test_the_document_functions_validate_before_they_touch_the_file(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    cfg = config.load_config(root)
    path = document.research_path(root, cfg, "thing")
    before = path.read_bytes()

    with pytest.raises(SpecfloError, match="ship"):
        document.set_destination(root, cfg, "thing", "ship", "why", today=TODAY)
    with pytest.raises(SpecfloError, match="reason"):
        document.set_destination(root, cfg, "thing", "build", "", today=TODAY)
    with pytest.raises(SpecfloError):
        document.set_user(root, cfg, "thing", "", today=TODAY)
    with pytest.raises(SpecfloError):
        document.close_session(root, cfg, "thing", "", "n", today=TODAY)
    assert path.read_bytes() == before

    path.unlink()
    for call in (
        lambda: document.set_user(root, cfg, "thing", BROKER, today=TODAY),
        lambda: document.set_destination(root, cfg, "thing", "build", "why", today=TODAY),
        lambda: document.close_session(root, cfg, "thing", "s", "n", today=TODAY),
    ):
        with pytest.raises(SpecfloError, match="research"):
            call()
    assert not path.exists()
    assert not document.has_document(root, cfg, "thing")


def test_set_user_and_set_destination_return_what_they_replaced(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    cfg = config.load_config(root)

    assert document.set_user(root, cfg, "thing", BROKER, today=TODAY) is None
    assert document.set_user(root, cfg, "thing", "Someone else.", today="2026-10-17") == BROKER
    assert document.set_destination(root, cfg, "thing", "build", "a tool", today="2026-10-17") == (
        "unknown", "build",
    )
    session = document.close_session(root, cfg, "thing", "s", "n", today="2026-10-18")
    assert session == document.Session("s", "n", "Someone else.", "2026-10-18")
    doc = _doc(root)
    assert "updated: 2026-10-18\n" in doc
    assert document.has_document(root, cfg, "thing")


def test_the_session_line_format_round_trips_through_the_parser():
    line = document.format_session("Read the act", "Ask about renewals", "the usual user")
    assert line == "Summary: Read the act | Next: Ask about renewals | Present: the usual user"
    entry = document.LogEntry(date=TODAY, kind=document.SESSION_KIND, text=line)

    session = document.parse_session(entry)

    assert session == document.Session("Read the act", "Ask about renewals", "the usual user", TODAY)
    with pytest.raises(SpecfloError):
        document.parse_session(document.LogEntry(date=TODAY, kind=document.SESSION_KIND, text="no form"))
    with pytest.raises(SpecfloError):
        document.parse_session(document.LogEntry(date=TODAY, kind="destination", text=line))


# --- the guide ------------------------------------------------------------------


def test_each_research_command_has_a_guide_row_under_the_research_group():
    rows = {entry["name"]: entry for entry in guide.COMMANDS if entry["group"] == "research"}

    assert {"research user set", "research destination set", "research session close"} <= set(rows)
    assert "--text <text> | --file <path>" in rows["research user set"]["args"]
    assert "build|inform|learn|unknown --reason <text>" in rows["research destination set"]["args"]
    assert "--summary <text> --next <text> [--present <who>]" in rows["research session close"]["args"]
    for row in rows.values():
        assert row["summary"].isascii() and row["args"].isascii()


def test_the_guide_text_shows_the_research_heading_and_its_rows(tmp_path, monkeypatch):
    """The rows render in `specflo guide`, not only in its JSON: the text output
    lists a group only when the CLI names its heading, after the project-wide
    verbs and before the agents."""
    monkeypatch.chdir(tmp_path)

    out = _ok(["guide"]).output

    assert "\n  Research\n" in out
    assert out.index("\n  Any phase\n") < out.index("\n  Research\n") < out.index("\n  Agents\n")
    for name in ("research user set", "research destination set", "research session close"):
        assert f"\n    {name} " in out, name
    assert out.index("\n    followup close ") < out.index("\n    research user set ")
    assert out.index("\n    research session close ") < out.index("\n    agent start ")

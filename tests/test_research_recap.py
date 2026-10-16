"""The session recap: ``specflo research recap``.

The recap is what an agent reads at the start of a session: where the research
is headed and why, who the usual user is, what the last session did and left
for the next one, which questions still wait and for whom, how much the
ledgers hold, and the one rule that guards the user's privacy in an outgoing
web search. It reads every file and writes none.
"""

import datetime
import json

from typer.testing import CliRunner

from specflo import config, guide
from specflo.cli import app
from specflo.research import document, ledger, recap

runner = CliRunner()

TODAY = datetime.date.today().isoformat()
RECAP = ["research", "recap"]
BROKER = "A broker with twenty years in commercial real estate."
SOURCE_FIELDS = [
    ("Kind", "document"), ("Date", "2026-10-16"), ("Format", "md"), ("Sha256", "def"),
    ("Original", "research/sources/S-01/notes.md"), ("Text", "research/sources/S-01/text.md"),
]


def _ok(args):
    result = runner.invoke(app, args)
    assert result.exit_code == 0, (args, result.output)
    return result


def _refused(args):
    result = runner.invoke(app, args)
    assert result.exit_code != 0, (args, result.output)
    assert "Traceback" not in result.output, args
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


def _filled_project(tmp_path, monkeypatch):
    """A research project with every kind of record the recap reads.

    The destination is set twice, so the recap has to show the latest value
    with its latest reason; one question is resolved and one finding is
    superseded, so the recap has to leave those out of its lists and counts.
    """
    root = _research_project(tmp_path, monkeypatch)
    cfg = config.load_config(root)
    _ok(["research", "user", "set", "--text", BROKER])
    _ok(["research", "destination", "set", "build", "--reason", "the user wants a tool"])
    _ok(["research", "destination", "set", "inform", "--reason", "a report is enough for now"])
    _ok(["research", "session", "close", "--summary", "Read the act", "--next", "Ask about renewals"])
    _ok(["research", "question", "add", "--text", "Which market first?", "--by", "user"])
    _ok(["research", "question", "add", "--text", "Which database?", "--by", "developer",
         "--default", "sqlite"])
    _ok(["research", "question", "add", "--text", "How many buildings?", "--by", "user"])
    _ok(["research", "question", "resolve", "Q-03", "--resolution", "Forty."])
    ledger.SOURCES.add(root, cfg, "thing", "notes.md", fields=SOURCE_FIELDS, today=TODAY)
    _ok(["research", "finding", "add", "--text", "Rent rose 4% in 2025.", "--kind", "fact",
         "--status", "supported", "--cites", "S-01"])
    _ok(["research", "finding", "add", "--text", "Rent rose 5% in 2025.", "--kind", "fact",
         "--status", "supported", "--cites", "S-01", "--supersedes", "RF-01"])
    _ok(["research", "term", "set", "--term", "Cap rate", "--definition", "net income over price"])
    return root


# --- the recap of a new project -------------------------------------------------


def test_recap_of_a_new_project_prints_each_empty_state_and_the_privacy_rule(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    before = _tree(root)

    out = _ok(RECAP).output

    assert out.isascii()
    assert "Destination: unknown" in out
    assert "not set yet" in out
    assert "Usual user: not recorded yet" in out
    assert "no session closed yet" in out
    assert "for the user: none" in out
    assert "for a developer: none" in out
    assert "sources 0, findings 0, terms 0" in out
    assert "Spin-offs on replaced entries: none" in out
    assert recap.PRIVACY_RULE in out
    assert "Never put names, private figures or text from the project's sources" in out
    assert "outgoing web search" in out
    assert _tree(root) == before


def test_recap_json_of_a_new_project_carries_each_empty_value(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    before = _tree(root)

    payload = json.loads(_ok([*RECAP, "--json"]).output)

    assert payload["destination"] == document.UNKNOWN_DESTINATION
    assert payload["reason"] is None
    assert payload["usual_user"] is None
    assert payload["last_session"] is None
    assert payload["open_questions"] == {"user": [], "developer": []}
    assert payload["counts"] == {"sources": 0, "findings": 0, "terms": 0}
    assert payload["spinoffs_on_replaced"] == []
    assert payload["privacy_rule"] == recap.PRIVACY_RULE
    assert _tree(root) == before


# --- the recap of a filled project ----------------------------------------------


def test_recap_shows_the_latest_destination_user_session_questions_and_counts(
    tmp_path, monkeypatch
):
    root = _filled_project(tmp_path, monkeypatch)
    before = _tree(root)

    out = _ok(RECAP).output

    assert out.isascii()
    assert "Destination: inform" in out
    assert "why: a report is enough for now" in out
    assert "the user wants a tool" not in out
    assert "build" not in out.split("Open questions")[0].replace("Destination", "")
    assert f"Usual user: {BROKER}" in out
    assert f"Last session: {TODAY}" in out
    assert "summary: Read the act" in out
    assert "next: Ask about renewals" in out
    assert "Q-01 Which market first?" in out
    assert "Q-02 Which database?" in out
    assert "Q-03" not in out and "How many buildings?" not in out
    user_block = out.split("for the user:")[1].split("for a developer:")[0]
    developer_block = out.split("for a developer:")[1].split("Counts")[0]
    assert "Q-01" in user_block and "Q-02" not in user_block
    assert "Q-02" in developer_block and "Q-01" not in developer_block
    assert "sources 1, findings 1, terms 1" in out
    assert "Spin-offs on replaced entries: none" in out
    assert recap.PRIVACY_RULE in out
    assert out.rstrip("\n").endswith(recap.PRIVACY_RULE)
    assert _tree(root) == before


def test_recap_json_carries_the_same_values(tmp_path, monkeypatch):
    root = _filled_project(tmp_path, monkeypatch)
    before = _tree(root)

    payload = json.loads(_ok([*RECAP, "--json"]).output)

    assert payload["destination"] == "inform"
    assert payload["reason"] == "a report is enough for now"
    assert payload["usual_user"] == BROKER
    assert payload["last_session"] == {
        "date": TODAY, "summary": "Read the act", "next_steps": "Ask about renewals",
        "present": BROKER,
    }
    assert payload["open_questions"] == {
        "user": [{"id": "Q-01", "text": "Which market first?"}],
        "developer": [{"id": "Q-02", "text": "Which database?"}],
    }
    assert payload["counts"] == {"sources": 1, "findings": 1, "terms": 1}
    assert payload["spinoffs_on_replaced"] == []
    assert payload["privacy_rule"] == recap.PRIVACY_RULE
    assert _tree(root) == before


def test_recap_text_blocks_come_in_the_fixed_order(tmp_path, monkeypatch):
    _filled_project(tmp_path, monkeypatch)

    out = _ok(RECAP).output

    marks = ["Destination:", "Usual user:", "Last session:", "Open questions",
             "for the user:", "for a developer:", "Counts:", "Spin-offs on replaced entries:",
             recap.PRIVACY_RULE]
    positions = [out.index(mark) for mark in marks]
    assert positions == sorted(positions), marks


# --- which project the command works on ---------------------------------------


def test_recap_refuses_a_project_without_a_research_document(tmp_path, monkeypatch):
    root = _checkout(tmp_path / "local", monkeypatch)
    _ok(["new", "Plain"])
    before = _tree(root)

    shown = _refused(RECAP)

    assert "plain" in shown and document.RESEARCH_FILENAME in shown
    assert _tree(root) == before


def test_recap_needs_a_checkout_and_an_active_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert "specflo init" in _refused(RECAP)

    root = _checkout(tmp_path / "local", monkeypatch)
    before = _tree(root)
    assert "No active project" in _refused(RECAP)
    assert _tree(root) == before


def test_recap_refuses_a_project_whose_ledger_is_missing_and_writes_nothing(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)
    ledger.QUESTIONS.path(root, config.load_config(root), "thing").unlink()
    before = _tree(root)

    shown = _refused(RECAP)

    assert "questions" in shown
    assert _tree(root) == before


# --- the module on its own -------------------------------------------------------


def test_render_recap_of_a_hand_built_payload():
    payload = {
        "destination": "learn",
        "reason": "curiosity",
        "usual_user": "A student.\nSecond year.",
        "last_session": {
            "date": "2026-10-15", "summary": "Read two papers", "next_steps": "Compare them",
            "present": "A student.",
        },
        "open_questions": {
            "user": [{"id": "Q-01", "text": "Which journal?"}, {"id": "Q-04", "text": "Why now?"}],
            "developer": [],
        },
        "counts": {"sources": 2, "findings": 3, "terms": 0},
        "spinoffs_on_replaced": [],
        "privacy_rule": recap.PRIVACY_RULE,
    }

    text = recap.render_recap(payload)

    assert text.isascii()
    assert text == (
        "Destination: learn\n"
        "  why: curiosity\n"
        "Usual user: A student.\n"
        "  Second year.\n"
        "Last session: 2026-10-15\n"
        "  summary: Read two papers\n"
        "  next: Compare them\n"
        "  present: A student.\n"
        "Open questions\n"
        "  for the user:\n"
        "    Q-01 Which journal?\n"
        "    Q-04 Why now?\n"
        "  for a developer: none\n"
        "Counts: sources 2, findings 3, terms 0\n"
        "Spin-offs on replaced entries: none\n"
        f"Privacy: {recap.PRIVACY_RULE}\n"
    )


def test_render_recap_of_an_empty_payload_names_each_empty_state():
    payload = {
        "destination": "unknown",
        "reason": None,
        "usual_user": None,
        "last_session": None,
        "open_questions": {"user": [], "developer": []},
        "counts": {"sources": 0, "findings": 0, "terms": 0},
        "spinoffs_on_replaced": [],
        "privacy_rule": recap.PRIVACY_RULE,
    }

    text = recap.render_recap(payload)

    assert text == (
        "Destination: unknown\n"
        "  why: not set yet\n"
        "Usual user: not recorded yet\n"
        "Last session: no session closed yet\n"
        "Open questions\n"
        "  for the user: none\n"
        "  for a developer: none\n"
        "Counts: sources 0, findings 0, terms 0\n"
        "Spin-offs on replaced entries: none\n"
        f"Privacy: {recap.PRIVACY_RULE}\n"
    )


def test_build_recap_returns_the_payload_the_command_prints(tmp_path, monkeypatch):
    root = _filled_project(tmp_path, monkeypatch)
    cfg = config.load_config(root)

    payload = recap.build_recap(root, cfg, "thing")

    assert payload == json.loads(_ok([*RECAP, "--json"]).output)
    assert set(payload) == {
        "destination", "reason", "usual_user", "last_session", "open_questions", "counts",
        "spinoffs_on_replaced", "privacy_rule",
    }
    assert recap.render_recap(payload) == _ok(RECAP).output


def test_the_privacy_rule_is_one_ascii_sentence():
    assert recap.PRIVACY_RULE.isascii()
    assert "\n" not in recap.PRIVACY_RULE
    assert recap.PRIVACY_RULE.endswith(".")
    assert recap.PRIVACY_RULE.count(".") == 1


# --- the guide ------------------------------------------------------------------


def test_recap_has_a_guide_row_right_after_term_set():
    rows = {entry["name"]: entry for entry in guide.COMMANDS if entry["group"] == "research"}

    row = rows["research recap"]
    assert row["args"] == "[--json]"
    assert row["summary"].isascii() and row["args"].isascii()
    names = [entry["name"] for entry in guide.COMMANDS]
    assert names.index("research term set") + 1 == names.index("research recap")


def test_the_guide_text_shows_the_recap_row_after_term_set(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    out = _ok(["guide"]).output

    assert "\n    research recap " in out
    assert out.index("\n    research term set ") < out.index("\n    research recap ")
    assert out.index("\n    research recap ") < out.index("\n    agent start ")

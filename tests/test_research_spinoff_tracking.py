"""Flag a spin-off that rests on a replaced entry.

A spin-off's handoff records the entries it was seeded with. When the research
later supersedes one of them, two readers learn of it without any text
parsing of their own: ``specflo research recap`` in the research project lists
the spin-off with each replaced ID and its replacement, and ``specflo status``
in the spun-off project prints one line naming the IDs and the research
project. Before the supersede neither shows anything. Both are read-only.
"""

import datetime
import json
import shutil

import pytest
from typer.testing import CliRunner

from specflo import config
from specflo.cli import app
from specflo.errors import SpecfloError
from specflo.research import ledger, spinoff

runner = CliRunner()

TODAY = datetime.date.today().isoformat()
RECAP = ["research", "recap"]
SPINOFF_Y = ["research", "spinoff", "Y", "--level", "fast", "--use", "RF-01,Q-01"]
SPINOFF_Z = ["research", "spinoff", "Z", "--level", "quick", "--use", "Q-01"]
SUPERSEDE_RF_01 = ["research", "finding", "add", "--text", "Rent rose 5% in 2025.",
                   "--kind", "inference", "--status", "unsure", "--supersedes", "RF-01"]
SUPERSEDE_Q_01 = ["research", "question", "add", "--text", "Which city first?", "--by", "user",
                  "--supersedes", "Q-01"]
NOTICE_HEAD = "Handoff from research project"
NOTICE_RF_01 = (
    "Handoff from research project 'thing': RF-01 was replaced by RF-03;"
    " see specflo research recap there."
)
RECAP_HEAD = "Spin-offs on replaced entries"
RECAP_Y = f"  y (fast, {TODAY}): RF-01 replaced by RF-03"


def _ok(args):
    result = runner.invoke(app, args)
    assert result.exit_code == 0, (args, result.output)
    return result


def _checkout(path, monkeypatch):
    path.mkdir()
    config.init_config(path)
    monkeypatch.chdir(path)
    return path


def _tree(directory):
    """Every file under ``directory`` with its bytes, keyed by relative path."""
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _seeded_project(tmp_path, monkeypatch):
    """A research project ``thing`` with findings RF-01 and RF-02 and question Q-01."""
    root = _checkout(tmp_path / "local", monkeypatch)
    _ok(["new", "Thing", "--level", "research"])
    _ok(["research", "finding", "add", "--text", "Rent rose 4% in 2025.", "--kind", "inference",
         "--status", "unsure"])
    _ok(["research", "finding", "add", "--text", "Vacancy is falling.", "--kind", "inference",
         "--status", "unsure"])
    _ok(["research", "question", "add", "--text", "Which market first?", "--by", "user"])
    return root


def _spun_off(tmp_path, monkeypatch):
    """The seeded project after ``y`` was spun off on RF-01 and Q-01; ``thing`` is active."""
    root = _seeded_project(tmp_path, monkeypatch)
    _ok(SPINOFF_Y)
    _ok(["switch", "thing"])
    return root


def _notice_lines(output):
    return [line for line in output.splitlines() if line.startswith(NOTICE_HEAD)]


def _recap_block(output):
    """The spin-off block of a recap: its heading line and the lines indented under it."""
    lines = output.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(RECAP_HEAD))
    block = [lines[start]]
    for line in lines[start + 1:]:
        if not line.startswith("  "):
            break
        block.append(line)
    return block


# --- before any supersede --------------------------------------------------------


def test_before_a_supersede_status_in_the_spinoff_prints_no_handoff_line(tmp_path, monkeypatch):
    _seeded_project(tmp_path, monkeypatch)
    _ok(SPINOFF_Y)

    out = _ok(["status"]).output

    assert "Project: Y (y)" in out
    assert _notice_lines(out) == []
    assert NOTICE_HEAD not in out


def test_before_a_supersede_the_recap_lists_no_spinoff(tmp_path, monkeypatch):
    _spun_off(tmp_path, monkeypatch)

    out = _ok(RECAP).output
    payload = json.loads(_ok([*RECAP, "--json"]).output)

    assert _recap_block(out) == ["Spin-offs on replaced entries: none"]
    assert payload["spinoffs_on_replaced"] == []


# --- after the supersede ---------------------------------------------------------


def test_after_a_supersede_the_recap_names_the_spinoff_and_the_replaced_entry(
    tmp_path, monkeypatch
):
    root = _spun_off(tmp_path, monkeypatch)
    _ok(SUPERSEDE_RF_01)
    before = _tree(root)

    out = _ok(RECAP).output
    payload = json.loads(_ok([*RECAP, "--json"]).output)

    assert out.isascii()
    assert _recap_block(out) == ["Spin-offs on replaced entries:", RECAP_Y]
    assert "Q-01" not in RECAP_Y
    assert payload["spinoffs_on_replaced"] == [
        {"slug": "y", "level": "fast", "date": TODAY,
         "replaced": [{"id": "RF-01", "by": "RF-03"}]},
    ]
    assert _tree(root) == before


def test_after_a_supersede_status_in_the_spinoff_prints_one_handoff_line(tmp_path, monkeypatch):
    root = _spun_off(tmp_path, monkeypatch)
    _ok(SUPERSEDE_RF_01)
    _ok(["switch", "y"])
    before = _tree(root)

    out = _ok(["status"]).output

    assert out.isascii()
    assert "Project: Y (y)" in out
    assert _notice_lines(out) == [NOTICE_RF_01]
    assert out.endswith(NOTICE_RF_01 + "\n")
    assert "Q-01" not in NOTICE_RF_01
    assert _tree(root) == before


def test_status_json_in_the_spinoff_stays_as_it_was(tmp_path, monkeypatch):
    _spun_off(tmp_path, monkeypatch)
    _ok(SUPERSEDE_RF_01)
    _ok(["switch", "y"])

    info = json.loads(_ok(["status", "--json"]).output)

    assert info["active_project"] == "y"
    assert "handoff_notice" not in info


def test_a_second_supersede_adds_its_id_to_the_recap_line_and_the_notice(tmp_path, monkeypatch):
    root = _spun_off(tmp_path, monkeypatch)
    _ok(SUPERSEDE_RF_01)
    _ok(SUPERSEDE_Q_01)

    out = _ok(RECAP).output
    _ok(["switch", "y"])
    notice = _notice_lines(_ok(["status"]).output)

    assert _recap_block(out) == [
        "Spin-offs on replaced entries:",
        f"  y (fast, {TODAY}): RF-01 replaced by RF-03, Q-01 replaced by Q-02",
    ]
    assert notice == [
        "Handoff from research project 'thing': RF-01 was replaced by RF-03,"
        " Q-01 was replaced by Q-02; see specflo research recap there."
    ]
    assert spinoff.spinoffs_on_replaced(root, config.load_config(root), "thing")[0]["replaced"] == [
        {"id": "RF-01", "by": "RF-03"}, {"id": "Q-01", "by": "Q-02"},
    ]


# --- what is not flagged ---------------------------------------------------------


def test_a_spinoff_whose_entries_all_stand_is_not_listed(tmp_path, monkeypatch):
    _spun_off(tmp_path, monkeypatch)
    _ok(SPINOFF_Z)
    _ok(["switch", "thing"])
    _ok(SUPERSEDE_RF_01)

    out = _ok(RECAP).output
    payload = json.loads(_ok([*RECAP, "--json"]).output)
    _ok(["switch", "z"])
    status_z = _ok(["status"]).output

    assert _recap_block(out) == ["Spin-offs on replaced entries:", RECAP_Y]
    assert [item["slug"] for item in payload["spinoffs_on_replaced"]] == ["y"]
    assert _notice_lines(status_z) == []


def test_a_project_with_no_handoff_prints_no_line(tmp_path, monkeypatch):
    _spun_off(tmp_path, monkeypatch)
    _ok(SUPERSEDE_RF_01)

    research_status = _ok(["status"]).output
    _ok(["new", "Plain"])
    plain_status = _ok(["status"]).output

    assert "Project: Thing (thing)" in research_status
    assert _notice_lines(research_status) == []
    assert "Project: Plain (plain)" in plain_status
    assert _notice_lines(plain_status) == []


def test_a_handoff_whose_research_project_is_gone_prints_no_line(tmp_path, monkeypatch):
    root = _spun_off(tmp_path, monkeypatch)
    _ok(SUPERSEDE_RF_01)
    _ok(["switch", "y"])
    assert _notice_lines(_ok(["status"]).output) == [NOTICE_RF_01]

    shutil.rmtree(root / "docs" / "projects" / "thing")
    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
    assert "Project: Y (y)" in result.output
    assert _notice_lines(result.output) == []


def test_a_handoff_whose_research_document_is_gone_prints_no_line(tmp_path, monkeypatch):
    root = _spun_off(tmp_path, monkeypatch)
    _ok(SUPERSEDE_RF_01)
    _ok(["switch", "y"])

    (root / "docs" / "projects" / "thing" / "research.md").unlink()
    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0, result.output
    assert _notice_lines(result.output) == []


def test_a_malformed_handoff_never_breaks_status(tmp_path, monkeypatch):
    root = _spun_off(tmp_path, monkeypatch)
    _ok(["switch", "y"])
    spinoff.handoff_path(root, config.load_config(root), "y").write_text("# No frontmatter\n")

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
    assert "Project: Y (y)" in result.output
    assert _notice_lines(result.output) == []


# --- the module on its own -------------------------------------------------------


def test_replaced_entries_names_only_the_replaced_ids_with_their_replacements(
    tmp_path, monkeypatch
):
    root = _seeded_project(tmp_path, monkeypatch)
    cfg = config.load_config(root)
    asked = ["RF-01", "Q-01", "RF-02"]
    assert spinoff.replaced_entries(root, cfg, "thing", asked) == []

    _ok(SUPERSEDE_RF_01)

    assert spinoff.replaced_entries(root, cfg, "thing", asked) == [("RF-01", "RF-03")]
    assert spinoff.replaced_entries(root, cfg, "thing", []) == []


def test_replaced_entries_skips_an_unknown_id_a_term_and_a_missing_ledger(tmp_path, monkeypatch):
    root = _seeded_project(tmp_path, monkeypatch)
    cfg = config.load_config(root)
    _ok(SUPERSEDE_RF_01)
    ledger.QUESTIONS.path(root, cfg, "thing").unlink()

    found = spinoff.replaced_entries(
        root, cfg, "thing", ["RF-99", "X-01", "Cap rate", "Q-01", "RF-01"]
    )

    assert found == [("RF-01", "RF-03")]


def test_spinoffs_on_replaced_lists_each_flagged_spinoff_oldest_first(tmp_path, monkeypatch):
    root = _spun_off(tmp_path, monkeypatch)
    cfg = config.load_config(root)
    _ok(SPINOFF_Z)
    _ok(["switch", "thing"])
    assert spinoff.spinoffs_on_replaced(root, cfg, "thing") == []

    _ok(SUPERSEDE_Q_01)

    assert spinoff.spinoffs_on_replaced(root, cfg, "thing") == [
        {"slug": "y", "level": "fast", "date": TODAY,
         "replaced": [{"id": "Q-01", "by": "Q-02"}]},
        {"slug": "z", "level": "quick", "date": TODAY,
         "replaced": [{"id": "Q-01", "by": "Q-02"}]},
    ]


def test_handoff_notice_is_none_until_an_entry_is_replaced(tmp_path, monkeypatch):
    root = _spun_off(tmp_path, monkeypatch)
    cfg = config.load_config(root)
    assert spinoff.handoff_notice(root, cfg, "y") is None
    assert spinoff.handoff_notice(root, cfg, "thing") is None

    _ok(SUPERSEDE_RF_01)

    assert spinoff.handoff_notice(root, cfg, "y") == NOTICE_RF_01
    assert spinoff.handoff_notice(root, cfg, "thing") is None


def test_handoff_notice_is_none_when_the_research_project_is_gone(tmp_path, monkeypatch):
    root = _spun_off(tmp_path, monkeypatch)
    cfg = config.load_config(root)
    _ok(SUPERSEDE_RF_01)
    before = _tree(root / "docs" / "projects" / "y")

    (root / "docs" / "projects" / "thing" / "research.md").unlink()
    assert spinoff.handoff_notice(root, cfg, "y") is None
    shutil.rmtree(root / "docs" / "projects" / "thing")
    assert spinoff.handoff_notice(root, cfg, "y") is None

    assert _tree(root / "docs" / "projects" / "y") == before


def test_handoff_notice_refuses_a_malformed_handoff(tmp_path, monkeypatch):
    root = _spun_off(tmp_path, monkeypatch)
    cfg = config.load_config(root)
    spinoff.handoff_path(root, cfg, "y").write_text("# No frontmatter\n")

    with pytest.raises(SpecfloError):
        spinoff.handoff_notice(root, cfg, "y")

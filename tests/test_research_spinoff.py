"""Spin a seeded project off the research: ``specflo research spinoff``.

The spin-off creates a new project the way ``specflo new <name> --level
<level>`` does (same files, same active pointer, same checkpoint) and adds a
``handoff.md`` holding each handed-off entry's ID and full text, the research
project's name and the date. The research project keeps its phase and status;
its log gets one ``spinoff`` entry naming the new project and the IDs. Every
refusal happens before anything is created and leaves the checkout
byte-identical.
"""

import datetime
import json

import pytest
from typer.testing import CliRunner

from specflo import config, guide, projects
from specflo.cli import app
from specflo.errors import SpecfloError
from specflo.research import document, ledger, spinoff

runner = CliRunner()

TODAY = datetime.date.today().isoformat()
SOURCE_FIELDS = [
    ("Kind", "document"), ("Date", "2026-10-16"), ("Format", "md"), ("Sha256", "def"),
    ("Original", "research/sources/S-01/notes.md"), ("Text", "research/sources/S-01/text.md"),
]
SPINOFF_Y = ["research", "spinoff", "Y", "--level", "fast", "--use", "RF-01,Q-02"]


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


def _tree(directory):
    """Every file under ``directory`` with its bytes, keyed by relative path."""
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _seeded_project(tmp_path, monkeypatch):
    """A research project ``thing`` with one source, two findings, two questions and a term."""
    root = _checkout(tmp_path / "local", monkeypatch)
    _ok(["new", "Thing", "--level", "research"])
    cfg = config.load_config(root)
    ledger.SOURCES.add(root, cfg, "thing", "notes.md", fields=SOURCE_FIELDS, today=TODAY)
    _ok(["research", "finding", "add", "--text", "Rent rose 4% in 2025.", "--kind", "fact",
         "--status", "supported", "--cites", "S-01"])
    _ok(["research", "finding", "add", "--text", "Vacancy is falling.", "--kind", "inference",
         "--status", "unsure"])
    _ok(["research", "question", "add", "--text", "Which market first?", "--by", "user",
         "--option", "Toronto", "--option", "Calgary"])
    _ok(["research", "question", "add", "--text", "Which database?", "--by", "developer",
         "--default", "sqlite", "--why", "the tool needs one"])
    _ok(["research", "term", "set", "--term", "Cap rate", "--definition", "net income over price"])
    return root


def _fresh_new(tmp_path, monkeypatch, name, level):
    """The files ``specflo new <name> --level <level>`` makes, in a checkout of its own."""
    root = _checkout(tmp_path / f"fresh-{level}", monkeypatch)
    _ok(["new", name, "--level", level])
    return _tree(root / "docs" / "projects" / projects.slugify(name))


def _project_files(root, slug):
    return _tree(root / "docs" / "projects" / slug)


def _spinoff_log(root, slug="thing"):
    parsed = document.load(root, config.load_config(root), slug)
    return [entry for entry in parsed.log if entry.kind == spinoff.SPINOFF_KIND]


# --- the spin-off creates the project as new would ----------------------------------


def test_spinoff_creates_the_project_as_new_would_and_makes_it_active(tmp_path, monkeypatch):
    root = _seeded_project(tmp_path, monkeypatch)

    out = _ok(SPINOFF_Y).output

    assert "Created project 'y' (now active). Phase: brainstorm." in out
    assert "y/handoff" in out
    assert "RF-01, Q-02" in out
    assert "specflo switch thing" in out
    assert "no longer active" in out
    assert out.isascii()
    cfg = config.load_config(root)
    assert cfg.active_project == "y"
    project = projects.load_project(root, cfg, "y")
    assert project.name == "Y"
    assert project.level == "fast"
    assert project.phase == "brainstorm"
    assert project.status == projects.INITIAL_STATUS
    created = _project_files(root, "y")
    assert {"project.md", "brainstorm.md", "checkpoint.md"} <= set(created)
    assert spinoff.HANDOFF_FILENAME in created


def test_spinoff_makes_the_same_files_as_new_plus_the_handoff(tmp_path, monkeypatch):
    fresh = _fresh_new(tmp_path, monkeypatch, "Y", "fast")
    root = _seeded_project(tmp_path, monkeypatch)

    _ok(SPINOFF_Y)

    created = _project_files(root, "y")
    assert set(created) == set(fresh) | {spinoff.HANDOFF_FILENAME}
    assert created["project.md"] == fresh["project.md"]
    assert created["brainstorm.md"] == fresh["brainstorm.md"]


def test_the_handoff_holds_the_research_name_the_date_and_each_entry_in_full(
    tmp_path, monkeypatch
):
    root = _seeded_project(tmp_path, monkeypatch)

    _ok(SPINOFF_Y)

    cfg = config.load_config(root)
    path = spinoff.handoff_path(root, cfg, "y")
    assert path == root / "docs" / "projects" / "y" / "handoff.md"
    text = path.read_text()
    assert text.isascii()
    assert text.startswith("---\n")
    head, body = text.split("---\n", 2)[1:]
    assert "research: thing\n" in head
    assert "research_name: Thing\n" in head
    assert f"date: {TODAY}\n" in head
    assert "entries: [RF-01, Q-02]\n" in head
    assert "# Handoff from Thing\n" in body
    assert "## RF-01 - Rent rose 4% in 2025.\n" in body
    assert "- Kind: fact\n" in body
    assert "- Status: supported\n" in body
    assert "- Cites: S-01\n" in body
    assert "## Q-02 - Which database?\n" in body
    assert "- By: developer\n" in body
    assert "- Default: sqlite\n" in body
    assert "- Why: the tool needs one\n" in body
    assert "- Status: open\n" in body
    assert body.index("## RF-01") < body.index("## Q-02")
    # The entries not handed off stay out.
    assert "RF-02" not in body and "Q-01" not in body and "Cap rate" not in body


def test_the_handoff_keeps_the_order_given_and_every_field_line(tmp_path, monkeypatch):
    root = _seeded_project(tmp_path, monkeypatch)

    _ok(["research", "spinoff", "Y", "--level", "full", "--use", "Q-01", "--use", "S-01,RF-02"])

    text = spinoff.handoff_path(root, config.load_config(root), "y").read_text()
    assert "entries: [Q-01, S-01, RF-02]\n" in text
    positions = [text.index(mark) for mark in ("## Q-01 - ", "## S-01 - notes.md", "## RF-02 - ")]
    assert positions == sorted(positions)
    q_block = text.split("## Q-01 - Which market first?\n")[1].split("## S-01")[0]
    assert q_block == "- By: user\n- Option: Toronto\n- Option: Calgary\n- Status: open\n\n"
    s_block = text.split("## S-01 - notes.md\n")[1].split("## RF-02")[0]
    assert s_block == "".join(f"- {name}: {value}\n" for name, value in SOURCE_FIELDS) + "\n"
    assert projects.load_project(root, config.load_config(root), "y").level == "full"


def test_a_superseded_entry_is_accepted_and_recorded_as_asked_for(tmp_path, monkeypatch):
    root = _seeded_project(tmp_path, monkeypatch)
    _ok(["research", "finding", "add", "--text", "Rent rose 5% in 2025.", "--kind", "fact",
         "--status", "supported", "--cites", "S-01", "--supersedes", "RF-01"])

    _ok(["research", "spinoff", "Y", "--level", "fast", "--use", "RF-01"])

    text = spinoff.handoff_path(root, config.load_config(root), "y").read_text()
    assert "entries: [RF-01]\n" in text
    assert "## RF-01 - Rent rose 4% in 2025.\n" in text
    assert "- Superseded by: RF-03\n" in text
    assert "RF-03 -" not in text


# --- the research project stays as it was -----------------------------------------


def test_the_research_project_keeps_its_phase_and_status_and_logs_the_spinoff(
    tmp_path, monkeypatch
):
    root = _seeded_project(tmp_path, monkeypatch)
    before = _project_files(root, "thing")

    _ok(SPINOFF_Y)

    after = _project_files(root, "thing")
    assert after["project.md"] == before["project.md"]
    unchanged = {name for name in before if name != document.RESEARCH_FILENAME}
    assert {name: after[name] for name in unchanged} == {name: before[name] for name in unchanged}
    assert set(after) == set(before)
    entries = _spinoff_log(root)
    assert len(entries) == 1
    assert entries[0].date == TODAY
    assert entries[0].text == "Spun off y at fast: RF-01, Q-02"
    old_log = before[document.RESEARCH_FILENAME].decode()
    new_log = after[document.RESEARCH_FILENAME].decode()
    assert new_log.count("spinoff") == old_log.count("spinoff") + 1

    _ok(["switch", "thing"])
    info = json.loads(_ok(["status", "--json"]).output)
    assert info["active_project"] == "thing"
    assert info["phase"] == "research"
    assert info["level"] == "research"
    assert info["status"] == projects.INITIAL_STATUS


# --- a second spin-off ---------------------------------------------------------------


def test_a_second_spinoff_succeeds_beside_the_first(tmp_path, monkeypatch):
    fresh = _fresh_new(tmp_path, monkeypatch, "Z", "quick")
    root = _seeded_project(tmp_path, monkeypatch)
    _ok(SPINOFF_Y)
    # The spin-off is active now; the research commands work on the research project.
    _ok(["switch", "thing"])

    out = _ok(["research", "spinoff", "Z", "--level", "quick", "--use", "S-01"]).output

    assert "Created project 'z' (now active). Phase:" in out
    cfg = config.load_config(root)
    assert cfg.active_project == "z"
    assert projects.load_project(root, cfg, "y").level == "fast"
    assert projects.load_project(root, cfg, "z").level == "quick"
    created = _project_files(root, "z")
    assert set(created) == set(fresh) | {spinoff.HANDOFF_FILENAME}
    assert "brief.md" in created
    assert "## S-01 - notes.md\n" in created[spinoff.HANDOFF_FILENAME].decode()
    assert [entry.text for entry in _spinoff_log(root)] == [
        "Spun off y at fast: RF-01, Q-02",
        "Spun off z at quick: S-01",
    ]
    assert projects.load_project(root, cfg, "thing").phase == "research"


# --- refusals ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "args, said",
    [
        (["--level", "fast", "--use", "RF-99"], "RF-99"),
        (["--level", "fast", "--use", "Cap rate"], "sources, questions and findings"),
        (["--level", "fast", "--use", "RF-01,RF-01"], "RF-01"),
        (["--level", "fast", "--use", "RF-01", "--use", "RF-01"], "RF-01"),
        (["--level", "fast", "--use", "X-01"], "X-01"),
        (["--level", "fast", "--use", ""], "--use"),
        (["--level", "research", "--use", "RF-01"], "research"),
        (["--level", "harden", "--use", "RF-01"], "harden"),
        (["--level", "fast"], "--use"),
        (["--use", "RF-01"], "--level"),
    ],
)
def test_a_bad_id_level_or_missing_option_is_refused_and_nothing_is_created(
    tmp_path, monkeypatch, args, said
):
    root = _seeded_project(tmp_path, monkeypatch)
    before = _tree(root)

    shown = _refused(["research", "spinoff", "Y", *args])

    assert said in shown
    assert _tree(root) == before
    assert not (root / "docs" / "projects" / "y").exists()
    assert config.load_config(root).active_project == "thing"
    assert _spinoff_log(root) == []


def test_a_duplicate_id_names_the_repeat(tmp_path, monkeypatch):
    _seeded_project(tmp_path, monkeypatch)

    shown = _refused(["research", "spinoff", "Y", "--level", "fast", "--use", "Q-02,RF-01,Q-02"])

    assert "Q-02" in shown and "twice" in shown


def test_a_glossary_term_is_refused_with_what_the_handoff_takes(tmp_path, monkeypatch):
    _seeded_project(tmp_path, monkeypatch)

    shown = _refused(["research", "spinoff", "Y", "--level", "fast", "--use", "RF-01,Cap rate"])

    assert "'Cap rate'" in shown
    assert "glossary" in shown
    assert "sources, questions and findings" in shown


def test_a_name_that_already_exists_is_refused_and_nothing_changes(tmp_path, monkeypatch):
    root = _seeded_project(tmp_path, monkeypatch)
    _ok(SPINOFF_Y)
    _ok(["switch", "thing"])
    before = _tree(root)

    for name in ("Y", "Thing"):
        shown = _refused(["research", "spinoff", name, "--level", "quick", "--use", "S-01"])
        assert "already exists" in shown
        assert _tree(root) == before

    assert len(_spinoff_log(root)) == 1
    assert config.load_config(root).active_project == "thing"


def test_spinoff_refuses_a_project_without_a_research_document(tmp_path, monkeypatch):
    root = _checkout(tmp_path / "local", monkeypatch)
    _ok(["new", "Plain"])
    before = _tree(root)

    shown = _refused(SPINOFF_Y)

    assert "plain" in shown and document.RESEARCH_FILENAME in shown
    assert _tree(root) == before


def test_spinoff_needs_a_checkout_and_an_active_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert "specflo init" in _refused(SPINOFF_Y)

    root = _checkout(tmp_path / "local", monkeypatch)
    before = _tree(root)
    assert "No active project" in _refused(SPINOFF_Y)
    assert _tree(root) == before


# --- the module on its own ---------------------------------------------------------


def test_parse_handoff_round_trips_the_written_document(tmp_path, monkeypatch):
    root = _seeded_project(tmp_path, monkeypatch)
    cfg = config.load_config(root)

    result = spinoff.spin_off(root, cfg, "thing", "Hand built", "fast", ["Q-01", "RF-02"], today=TODAY)

    assert result.slug == "hand-built"
    assert result.level == "fast"
    assert result.entries == ("Q-01", "RF-02")
    assert result.handoff_path == spinoff.handoff_path(root, cfg, "hand-built")
    parsed = spinoff.parse_handoff(result.handoff_path.read_text())
    assert parsed == spinoff.Handoff(
        research="thing", research_name="Thing", date=TODAY, entries=("Q-01", "RF-02")
    )
    assert config.load_config(root).active_project == "hand-built"


def test_parse_handoff_refuses_a_document_without_its_frontmatter():
    with pytest.raises(SpecfloError):
        spinoff.parse_handoff("# Handoff from nowhere\n")
    with pytest.raises(SpecfloError):
        spinoff.parse_handoff("---\nresearch: thing\n---\n# Handoff\n")


def test_render_handoff_quotes_a_name_yaml_would_misread():
    entry = ledger.Entry(id="RF-01", title="A claim.", fields=(("Kind", "fact"),))

    text = spinoff.render_handoff("thing", "Thing: the sequel", "2026-10-16", [entry])

    parsed = spinoff.parse_handoff(text)
    assert parsed.research_name == "Thing: the sequel"
    assert parsed.entries == ("RF-01",)
    assert "# Handoff from Thing: the sequel\n" in text
    assert "## RF-01 - A claim.\n- Kind: fact\n" in text


def test_spinoffs_of_lists_each_spinoff_with_its_ids(tmp_path, monkeypatch):
    root = _seeded_project(tmp_path, monkeypatch)
    cfg = config.load_config(root)
    assert spinoff.spinoffs_of(root, cfg, "thing") == []
    _ok(SPINOFF_Y)
    _ok(["switch", "thing"])
    _ok(["research", "spinoff", "Z", "--level", "quick", "--use", "S-01"])

    listed = spinoff.spinoffs_of(root, cfg, "thing")

    assert listed == [
        spinoff.Spinoff(slug="y", level="fast", entries=("RF-01", "Q-02"), date=TODAY),
        spinoff.Spinoff(slug="z", level="quick", entries=("S-01",), date=TODAY),
    ]


def test_spinoffs_of_refuses_a_malformed_spinoff_entry(tmp_path, monkeypatch):
    root = _seeded_project(tmp_path, monkeypatch)
    cfg = config.load_config(root)
    document.append_log(root, cfg, "thing", "not the shape", kind=spinoff.SPINOFF_KIND)

    with pytest.raises(SpecfloError):
        spinoff.spinoffs_of(root, cfg, "thing")


def test_spin_off_checks_everything_before_it_creates_anything(tmp_path, monkeypatch):
    root = _seeded_project(tmp_path, monkeypatch)
    cfg = config.load_config(root)
    before = _tree(root)

    for level, use in (("fast", []), ("fast", ["RF-99"]), ("research", ["RF-01"]), ("fast", ["Cap rate"])):
        with pytest.raises(SpecfloError):
            spinoff.spin_off(root, cfg, "thing", "Y", level, use)
        assert _tree(root) == before


# --- the guide ----------------------------------------------------------------------


def test_spinoff_has_a_guide_row_right_after_recap():
    rows = {entry["name"]: entry for entry in guide.COMMANDS if entry["group"] == "research"}

    row = rows["research spinoff"]
    assert row["args"].startswith("<name> --level quick|fast|full --use ")
    assert row["summary"].isascii() and row["args"].isascii()
    assert "handoff" in row["summary"]
    names = [entry["name"] for entry in guide.COMMANDS]
    assert names.index("research recap") + 1 == names.index("research spinoff")


def test_the_guide_text_shows_the_spinoff_row_after_recap(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    out = _ok(["guide"]).output

    assert "\n    research spinoff " in out
    assert out.index("\n    research recap ") < out.index("\n    research spinoff ")
    assert out.index("\n    research spinoff ") < out.index("\n    agent start ")

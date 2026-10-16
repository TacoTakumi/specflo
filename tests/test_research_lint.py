"""Research lint: each finding's figures checked against the text of its sources.

``specflo research lint`` reads the active findings that cite a source and
looks for every figure of each one - its numbers, dates and double-quoted
strings - word for word in the stored text of at least one cited source, and
within the cited page when the citation names one. A missing figure is one
line naming the finding, the figure and every cited source; the command exits
1 with any problem and 0 without. It reads only: every file stays
byte-identical, and the research project completes whether or not it passes.
"""

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from specflo import config, guide
from specflo.cli import app
from specflo.errors import SpecfloError
from specflo.research import document, lint

from test_level_research import record_destination, record_session, record_user

runner = CliRunner()

LINT = ["research", "lint"]
INGEST = ["research", "ingest"]
FINDING_ADD = ["research", "finding", "add"]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "research"
PDF = FIXTURES / "three-pages.pdf"
# The markdown source, S-01: a figure of each kind. The PDF fixture is S-02:
# page one holds "4 percent", page two "five years", page three neither.
NOTES = 'Rent rose 4% in 2025 and the "renewal clause" runs five years.\n'


def _ok(args):
    result = runner.invoke(app, args)
    assert result.exit_code == 0, (args, result.output)
    return result


def _refused(args):
    result = runner.invoke(app, args)
    assert result.exit_code != 0, (args, result.output)
    assert "Traceback" not in result.output, (args, result.output)
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


def _with_sources(tmp_path, monkeypatch):
    """A research project holding the notes as S-01 and the three-page PDF as S-02."""
    root = _research_project(tmp_path, monkeypatch)
    notes = tmp_path / "notes.md"
    notes.write_text(NOTES, encoding="utf-8")
    _ok([*INGEST, str(notes)])
    pdf = tmp_path / PDF.name
    shutil.copyfile(PDF, pdf)
    _ok([*INGEST, str(pdf)])
    return root


def _finding(text, *cites, kind="fact", status="supported", supersedes=None):
    args = [*FINDING_ADD, "--text", text, "--kind", kind, "--status", status]
    for cite in cites:
        args += ["--cites", cite]
    if supersedes:
        args += ["--supersedes", supersedes]
    return _ok(args)


def _lint():
    return runner.invoke(app, LINT)


# --- passing and failing findings ----------------------------------------------------


def test_a_finding_whose_figures_are_in_the_cited_source_passes(tmp_path, monkeypatch):
    _with_sources(tmp_path, monkeypatch)
    _finding("Rent rose 4% in 2025.", "S-01")

    result = _lint()

    assert result.exit_code == 0, result.output
    assert result.output == "All 1 findings with citations are grounded.\n"


def test_a_missing_figure_fails_naming_the_figure_and_the_source(tmp_path, monkeypatch):
    _with_sources(tmp_path, monkeypatch)
    _finding("Rent rose 7% in 2025.", "S-01")

    result = _lint()

    assert result.exit_code == 1, result.output
    assert 'RF-01: "7%" not found in S-01\n' in result.output
    assert "2025" not in result.output.replace("RF-01", "")
    assert result.output.rstrip("\n").splitlines()[-1] == "1 problem in 1 of 1 findings with citations."


def test_a_number_is_found_with_or_without_thousands_separators(tmp_path, monkeypatch):
    root = _with_sources(tmp_path, monkeypatch)
    text = root / "docs" / "projects" / "thing" / "research" / "sources" / "S-01" / "text.md"
    text.write_text("The estate has 1,000 flats, 2000 houses and 30000 plots.\n", encoding="utf-8")
    _finding("The estate has 1000 flats.", "S-01")
    _finding("The estate has 2,000 houses.", "S-01")
    _finding("The estate has 3000 plots.", "S-01")

    result = _lint()

    assert result.exit_code == 1, result.output
    assert result.output == (
        'RF-03: "3000" not found in S-01\n'
        "1 problem in 1 of 3 findings with citations.\n"
    )


def test_a_page_citation_fails_when_the_figure_is_only_on_another_page(tmp_path, monkeypatch):
    _with_sources(tmp_path, monkeypatch)
    _finding("Rent rose 4 percent last year.", "S-02:p2")
    _finding("Rent rose 4 percent last year.", "S-02:p1")

    result = _lint()

    assert result.exit_code == 1, result.output
    assert result.output == (
        'RF-01: "4" not found in S-02:p2\n'
        "1 problem in 1 of 2 findings with citations.\n"
    )


def test_a_citation_without_a_page_finds_a_figure_on_any_page(tmp_path, monkeypatch):
    _with_sources(tmp_path, monkeypatch)
    _finding("Rent rose 4 percent last year.", "S-02")

    result = _lint()

    assert result.exit_code == 0, result.output


def test_a_quoted_string_present_passes_and_one_absent_fails(tmp_path, monkeypatch):
    _with_sources(tmp_path, monkeypatch)
    _finding('The lease has a "renewal clause".', "S-01")
    _finding('The lease has a "break clause".', "S-01")
    # Quoted strings are compared as written, case included.
    _finding('The lease has a "Renewal clause".', "S-01")

    result = _lint()

    assert result.exit_code == 1, result.output
    assert result.output == (
        'RF-02: "break clause" not found in S-01\n'
        'RF-03: "Renewal clause" not found in S-01\n'
        "2 problems in 2 of 3 findings with citations.\n"
    )


def test_a_figure_in_any_one_of_several_cited_sources_passes_and_a_missing_one_names_them_all(
    tmp_path, monkeypatch
):
    _with_sources(tmp_path, monkeypatch)
    # 2025 is in S-01 only, 4 in both: each figure has a source.
    _finding("Rent rose 4% in 2025.", "S-02:p1", "S-01")
    _finding("Rent rose 4% in 2024.", "S-01", "S-02:p1")

    result = _lint()

    assert result.exit_code == 1, result.output
    assert result.output == (
        'RF-02: "2024" not found in S-01, S-02:p1\n'
        "1 problem in 1 of 2 findings with citations.\n"
    )


def test_each_missing_figure_of_a_finding_is_one_line(tmp_path, monkeypatch):
    _with_sources(tmp_path, monkeypatch)
    _finding('Rent rose 7% in 2024 under the "break clause".', "S-01")

    result = _lint()

    assert result.exit_code == 1, result.output
    assert result.output == (
        'RF-01: "7%" not found in S-01\n'
        'RF-01: "2024" not found in S-01\n'
        'RF-01: "break clause" not found in S-01\n'
        "3 problems in 1 of 1 findings with citations.\n"
    )


# --- what is skipped -------------------------------------------------------------------


def test_an_inference_with_no_citation_is_skipped(tmp_path, monkeypatch):
    _with_sources(tmp_path, monkeypatch)
    _finding("Rents will rise 7% in 2027.", kind="inference", status="unsure")

    result = _lint()

    assert result.exit_code == 0, result.output
    assert result.output == "No findings with citations to check.\n"

    _finding("Rent rose 4% in 2025.", "S-01")
    assert _lint().output == "All 1 findings with citations are grounded.\n"


def test_a_superseded_finding_is_skipped(tmp_path, monkeypatch):
    _with_sources(tmp_path, monkeypatch)
    _finding("Rent rose 7% in 2025.", "S-01")
    assert _lint().exit_code == 1
    _finding("Rent rose 4% in 2025.", "S-01", supersedes="RF-01")

    result = _lint()

    assert result.exit_code == 0, result.output
    assert "RF-01" not in result.output
    assert result.output == "All 1 findings with citations are grounded.\n"


def test_a_finding_without_figures_is_grounded(tmp_path, monkeypatch):
    _with_sources(tmp_path, monkeypatch)
    _finding("The act names the tenant.", "S-02:p3")

    result = _lint()

    assert result.exit_code == 0, result.output


# --- read only, and no gate ------------------------------------------------------------


def test_a_run_leaves_every_file_byte_identical(tmp_path, monkeypatch):
    root = _with_sources(tmp_path, monkeypatch)
    _finding("Rent rose 7% in 2025.", "S-01")
    _finding("Rent rose 4% in 2025.", "S-01")
    before = _tree(root)

    assert _lint().exit_code == 1
    assert _tree(root) == before

    _finding("Rent rose 4% in 2025.", "S-01", supersedes="RF-01")
    before = _tree(root)

    assert _lint().exit_code == 0
    assert _tree(root) == before


def test_advance_completes_the_research_project_while_lint_fails(tmp_path, monkeypatch):
    root = _with_sources(tmp_path, monkeypatch)
    cfg = config.load_config(root)
    _finding("Rent rose 7% in 2025.", "S-01")
    record_destination(root, cfg)
    record_user(root, cfg)
    record_session(root, cfg)
    assert _lint().exit_code == 1

    result = _ok(["advance"])

    assert result.output.splitlines()[0] == "Completed project 'thing'."
    assert _lint().exit_code == 1


def test_the_command_refuses_a_project_without_a_research_document(tmp_path, monkeypatch):
    root = _checkout(tmp_path / "local", monkeypatch)
    _ok(["new", "Plain"])
    before = _tree(root)

    shown = _refused(LINT)

    assert "plain" in shown and document.RESEARCH_FILENAME in shown
    assert _tree(root) == before


# --- the module on its own ---------------------------------------------------------------


def test_figures_of_finds_numbers_with_a_glued_percent_sign_or_unit():
    assert lint.figures_of("Rent rose 4% in 2025.") == ["4%", "2025"]
    assert lint.figures_of("Rent rose 4 percent.") == ["4"]
    assert lint.figures_of("A 3.5% rise over 12 months, 1,200 units at 10km.") == [
        "3.5%", "12", "1,200", "10km",
    ]
    assert lint.figures_of("The 2nd floor of 3 is let.") == ["2nd", "3"]


def test_figures_of_finds_dates_whole_and_not_their_parts():
    assert lint.figures_of("Signed on 2026-10-16.") == ["2026-10-16"]
    assert lint.figures_of("Signed in October 2026, filed 16 October 2026.") == [
        "October 2026", "16 October 2026",
    ]
    assert lint.figures_of("Filed October 16, 2026 and Oct 2026.") == ["October 16, 2026", "Oct 2026"]


def test_figures_of_finds_quoted_strings_and_nothing_inside_them():
    assert lint.figures_of('The "renewal clause" runs five years.') == ["renewal clause"]
    assert lint.figures_of('It says "4 percent in 2025" and 7%.') == ["4 percent in 2025", "7%"]
    assert lint.figures_of('An empty "" quote and an open " quote.') == []


def test_figures_of_keeps_document_order_and_drops_repeats():
    assert lint.figures_of('In 2025 the "cap" was 4%; 4% again in 2025.') == ["2025", "cap", "4%"]


def test_figures_of_leaves_words_and_identifiers_alone():
    assert lint.figures_of("Brokers renew most leases early.") == []
    assert lint.figures_of("Version v2 of form A4 applies.") == []
    assert lint.figures_of("") == []


def _holds(source, finding):
    """True when the one figure of ``finding`` is held by the text ``source``."""
    (figure,) = lint.figures(finding)
    return lint._holds(source, figure)


def test_a_number_matches_its_grouped_and_ungrouped_forms_both_ways():
    assert _holds("Stock stands at 1,000 units.", "1000 units")
    assert _holds("Stock stands at 1000 units.", "1,000 units")
    assert _holds("Total: 1,234,567.", "1234567")
    assert _holds("Total: 1234567.", "1,234,567")
    assert _holds("Rent is 1,234.5 each.", "1234.5 each")
    assert _holds("Rent is 1234.5 each.", "1,234.5 each")
    assert _holds("A 4% rise to 1,200 units.", "1200")
    assert not _holds("Stock stands at 1,000 units.", "100 units")
    assert not _holds("Stock stands at 1,000 units.", "1,001 units")


def test_a_number_is_found_only_as_a_whole_number_in_either_form():
    longer = ("10000", "21000", "1000.5", "1,0001", "11,000", "1,000.5", "1,000,000", "1,001,000")
    for source in longer:
        assert not _holds(f"Stock stands at {source} units.", "1000 units"), source
        assert not _holds(f"Stock stands at {source} units.", "1,000 units"), source


def test_render_lists_each_problem_then_the_counts():
    problems = [
        lint.Problem("RF-01", "7%", ["S-01"], [None]),
        lint.Problem("RF-03", "4", ["S-02", "S-01"], [2, None]),
    ]

    assert lint.render(problems, 3) == (
        'RF-01: "7%" not found in S-01\n'
        'RF-03: "4" not found in S-02:p2, S-01\n'
        "2 problems in 2 of 3 findings with citations."
    )
    assert lint.render([], 3) == "All 3 findings with citations are grounded."
    assert lint.render([], 0) == "No findings with citations to check."


def test_check_finding_matches_whitespace_normalised_and_pages_by_section(tmp_path, monkeypatch):
    root = _with_sources(tmp_path, monkeypatch)
    cfg = config.load_config(root)
    text = root / "docs" / "projects" / "thing" / "research" / "sources" / "S-01" / "text.md"
    text.write_text('Rent rose 4% in\n2025 and the "renewal\n  clause" runs five years.\n')
    _finding('Rent rose 4% in 2025 under the "renewal clause".', "S-01")
    _finding("Rent rose 4 percent.", "S-02:p1")
    _finding("Rent rose 4 percent.", "S-02:p2")
    first, second, third = lint.cited_findings(root, cfg, "thing")

    assert lint.check_finding(root, cfg, "thing", first) == []
    assert lint.check_finding(root, cfg, "thing", second) == []
    assert lint.check_finding(root, cfg, "thing", third) == [
        lint.Problem("RF-03", "4", ["S-02"], [2]),
    ]
    assert lint.lint(root, cfg, "thing") == [lint.Problem("RF-03", "4", ["S-02"], [2])]


def test_a_cited_source_without_stored_text_is_refused(tmp_path, monkeypatch):
    root = _with_sources(tmp_path, monkeypatch)
    cfg = config.load_config(root)
    _finding("Rent rose 4% in 2025.", "S-01")
    (root / "docs" / "projects" / "thing" / "research" / "sources" / "S-01" / "text.md").unlink()

    with pytest.raises(SpecfloError, match="S-01"):
        lint.lint(root, cfg, "thing")
    shown = _refused(LINT)
    assert "S-01" in shown


# --- the help and the guide ----------------------------------------------------------------


def test_the_lint_help_and_module_docstring_name_the_thousands_separator_rule(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, [*LINT, "--help"], terminal_width=200)

    assert result.exit_code == 0, result.output
    assert "1,000" in result.output and "1000" in result.output, result.output
    assert "1,000" in lint.__doc__ and "1000" in lint.__doc__


def test_the_guide_has_a_row_for_lint_after_list(tmp_path, monkeypatch):
    rows = [entry for entry in guide.COMMANDS if entry["group"] == "research"]
    names = [row["name"] for row in rows]
    assert names.index("research list") + 1 == names.index("research lint")
    assert names.index("research lint") < names.index("research session close")
    row = next(row for row in rows if row["name"] == "research lint")
    assert row["summary"].isascii() and row["args"].isascii()
    assert "exit" in row["summary"] and "writes nothing" in row["summary"]

    monkeypatch.chdir(tmp_path)
    out = _ok(["guide"]).output
    assert "\n    research lint " in out
    assert out.index("\n    research list ") < out.index("\n    research lint ")
    assert out.index("\n    research lint ") < out.index("\n    research session close ")


def test_a_short_number_is_not_found_inside_a_grouped_neighbour():
    assert not _holds("Rent is 1,100 a month.", "100 a month")
    assert not _holds("Stock stands at 1,000 units.", "1 unit")
    assert not _holds("Sales hit 12,000 last year.", "12 last year")
    assert not _holds("The count was 1,234,567.", "567 in all")
    assert not _holds("The count was 1,234,567.", "234 in all")
    assert _holds("There are 100 flats.", "100 flats")
    assert _holds("There are 1,100 flats.", "1,100 flats")
    assert _holds("There are 1,100 flats.", "1100 flats")


def test_a_comma_that_is_not_a_thousands_separator_does_not_block_a_number():
    assert _holds("See Figures 2,3 for the trend.", "Figure 3")
    assert _holds("The sections 1,2,3 cover it.", "section 2")
    assert _holds("On pp. 12,13 the act names the tenant.", "page 13")
    assert _holds("Rooms 10,11 face the street.", "room 11")
    csv = "Year,Units 2024,1500 2025,1600"
    assert _holds(csv, "2024 units")
    assert _holds(csv, "1500 units")
    assert _holds(csv, "1600 units")
    assert not _holds("Rent is 1,100 a month.", "100 a month")
    assert not _holds("Sales hit 100,000 last year.", "100 last year")
    assert not _holds("Stock stands at 1,000 units.", "1 unit")

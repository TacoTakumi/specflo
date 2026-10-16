"""The research findings: one fact or inference per entry, with checked citations.

``specflo research finding add`` records a finding in the findings ledger.
A citation names a source of the sources ledger, with a page on a PDF only,
and a fact needs at least one. Every refusal happens before any write and
leaves the checkout byte-identical.
"""

import datetime

import pytest
from typer.testing import CliRunner

from specflo import config, guide
from specflo.cli import app
from specflo.errors import SpecfloError
from specflo.research import document, findings, ledger

runner = CliRunner()

TODAY = datetime.date.today().isoformat()
FINDING_ADD = ["research", "finding", "add"]
PDF_FIELDS = [
    ("Kind", "document"), ("Date", "2026-10-16"), ("Format", "pdf"), ("Pages", "10"),
    ("Sha256", "abc"), ("Original", "research/sources/S-01/paper.pdf"),
    ("Text", "research/sources/S-01/text.md"),
]
MD_FIELDS = [
    ("Kind", "document"), ("Date", "2026-10-16"), ("Format", "md"), ("Sha256", "def"),
    ("Original", "research/sources/S-02/notes.md"), ("Text", "research/sources/S-02/text.md"),
]


def _ok(args, stdin=None):
    result = runner.invoke(app, args, input=stdin)
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


def _with_sources(tmp_path, monkeypatch):
    """A research project holding a 10-page PDF as S-01 and a markdown file as S-02."""
    root = _research_project(tmp_path, monkeypatch)
    cfg = config.load_config(root)
    ledger.SOURCES.add(root, cfg, "thing", "paper.pdf", fields=PDF_FIELDS, today=TODAY)
    ledger.SOURCES.add(root, cfg, "thing", "notes.md", fields=MD_FIELDS, today=TODAY)
    return root, cfg


def _findings(root):
    return ledger.FINDINGS.entries(root, config.load_config(root), "thing")


def _ledger_text(root):
    return ledger.FINDINGS.path(root, config.load_config(root), "thing").read_text()


# --- recording ------------------------------------------------------------------


def test_an_inference_with_no_citation_is_recorded(tmp_path, monkeypatch):
    root = _research_project(tmp_path, monkeypatch)

    shown = _ok([
        *FINDING_ADD, "--text", "Brokers renew most leases early.", "--kind", "inference",
        "--status", "unsure",
    ]).output

    (entry,) = _findings(root)
    assert entry.id == "RF-01"
    assert entry.title == "Brokers renew most leases early."
    assert entry.fields == (("Kind", "inference"), ("Status", "unsure"))
    assert entry.all("Cites") == []
    assert "RF-01" in shown and "thing" in shown
    assert "### RF-01 - Brokers renew most leases early.\n- Kind: inference\n- Status: unsure\n" in _ledger_text(root)


def test_a_fact_with_a_citation_is_recorded_with_its_fields_in_order(tmp_path, monkeypatch):
    root, _ = _with_sources(tmp_path, monkeypatch)

    shown = _ok([
        *FINDING_ADD, "--text", "Rent rose 4% in 2025.", "--kind", "fact",
        "--status", "supported", "--cites", "S-01",
    ]).output

    (entry,) = _findings(root)
    assert entry.id == "RF-01"
    assert entry.fields == (("Kind", "fact"), ("Status", "supported"), ("Cites", "S-01"))
    assert entry.status == "supported"
    assert "RF-01" in shown
    assert "### RF-01 - Rent rose 4% in 2025.\n- Kind: fact\n- Status: supported\n- Cites: S-01\n" in _ledger_text(root)


def test_a_page_citation_within_the_pdf_is_accepted_and_several_citations_repeat_the_field(
    tmp_path, monkeypatch
):
    root, _ = _with_sources(tmp_path, monkeypatch)

    _ok([
        *FINDING_ADD, "--text", "The act caps increases.", "--kind", "fact", "--status", "supported",
        "--cites", "S-01:p3", "--cites", "S-02",
    ])

    (entry,) = _findings(root)
    assert entry.all("Cites") == ["S-01:p3", "S-02"]
    assert "- Cites: S-01:p3\n- Cites: S-02\n" in _ledger_text(root)


@pytest.mark.parametrize("status", findings.STATUSES)
def test_each_status_is_accepted(tmp_path, monkeypatch, status):
    root, _ = _with_sources(tmp_path, monkeypatch)

    _ok([*FINDING_ADD, "--text", f"A {status} claim.", "--kind", "fact", "--status", status, "--cites", "S-02"])

    assert _findings(root)[0].status == status


def test_supersedes_marks_the_old_finding_and_links_the_new_one(tmp_path, monkeypatch):
    root, _ = _with_sources(tmp_path, monkeypatch)
    _ok([*FINDING_ADD, "--text", "Rent rose 4%.", "--kind", "fact", "--status", "supported", "--cites", "S-01:p3"])

    shown = _ok([
        *FINDING_ADD, "--text", "Rent rose 4.5%.", "--kind", "fact", "--status", "supported",
        "--cites", "S-01:p4", "--supersedes", "RF-01",
    ]).output

    old, new = _findings(root)
    assert (old.id, old.superseded_by, old.active, old.status) == ("RF-01", "RF-02", False, "supported")
    assert (new.id, new.supersedes, new.active) == ("RF-02", "RF-01", True)
    assert new.all("Cites") == ["S-01:p4"]
    assert "RF-02" in shown and "RF-01" in shown
    assert (
        "### RF-01 - Rent rose 4%.\n- Kind: fact\n- Status: supported\n- Cites: S-01:p3\n- Superseded by: RF-02\n\n"
        "### RF-02 - Rent rose 4.5%.\n- Kind: fact\n- Status: supported\n- Cites: S-01:p4\n- Supersedes: RF-01\n"
    ) in _ledger_text(root)


# --- refusals -------------------------------------------------------------------


def test_a_fact_with_no_citation_is_refused_and_nothing_is_written(tmp_path, monkeypatch):
    root, _ = _with_sources(tmp_path, monkeypatch)
    before = _tree(root)

    shown = _refused([*FINDING_ADD, "--text", "Rent rose 4%.", "--kind", "fact", "--status", "supported"])

    assert "fact" in shown and "citation" in shown
    assert _tree(root) == before


def test_a_citation_to_an_unknown_source_is_refused(tmp_path, monkeypatch):
    root, _ = _with_sources(tmp_path, monkeypatch)
    before = _tree(root)

    shown = _refused([
        *FINDING_ADD, "--text", "Rent rose 4%.", "--kind", "fact", "--status", "supported", "--cites", "S-99",
    ])

    assert "S-99" in shown and "unknown source" in shown
    assert _tree(root) == before


def test_a_page_citation_on_a_source_that_is_not_a_pdf_is_refused(tmp_path, monkeypatch):
    root, _ = _with_sources(tmp_path, monkeypatch)
    before = _tree(root)

    shown = _refused([
        *FINDING_ADD, "--text", "Rent rose 4%.", "--kind", "fact", "--status", "supported", "--cites", "S-02:p2",
    ])

    assert "S-02:p2" in shown and "no pages" in shown and "md" in shown
    assert _tree(root) == before


def test_a_page_beyond_the_pdf_page_count_is_refused(tmp_path, monkeypatch):
    root, _ = _with_sources(tmp_path, monkeypatch)
    before = _tree(root)

    shown = _refused([
        *FINDING_ADD, "--text", "Rent rose 4%.", "--kind", "fact", "--status", "supported", "--cites", "S-01:p40",
    ])

    assert "S-01:p40" in shown and "page 40" in shown and "10 pages" in shown
    assert _tree(root) == before


def test_other_bad_input_is_refused_before_anything_is_written(tmp_path, monkeypatch):
    root, _ = _with_sources(tmp_path, monkeypatch)
    before = _tree(root)

    for args in (
        [*FINDING_ADD, "--kind", "fact", "--status", "supported", "--cites", "S-01"],
        [*FINDING_ADD, "--text", "", "--kind", "fact", "--status", "supported", "--cites", "S-01"],
        [*FINDING_ADD, "--text", "   ", "--kind", "inference", "--status", "unsure"],
        [*FINDING_ADD, "--text", "one\ntwo", "--kind", "inference", "--status", "unsure"],
        [*FINDING_ADD, "--text", "ok", "--status", "unsure"],
        [*FINDING_ADD, "--text", "ok", "--kind", "inference"],
        [*FINDING_ADD, "--text", "ok", "--kind", "guess", "--status", "unsure"],
        [*FINDING_ADD, "--text", "ok", "--kind", "Fact", "--status", "unsure", "--cites", "S-01"],
        [*FINDING_ADD, "--text", "ok", "--kind", "inference", "--status", "maybe"],
        [*FINDING_ADD, "--text", "ok", "--kind", "fact", "--status", "supported", "--cites", "S-01:p0"],
        [*FINDING_ADD, "--text", "ok", "--kind", "fact", "--status", "supported", "--cites", "S-01:3"],
        [*FINDING_ADD, "--text", "ok", "--kind", "fact", "--status", "supported", "--cites", "S01"],
        [*FINDING_ADD, "--text", "ok", "--kind", "fact", "--status", "supported", "--cites", "RF-01"],
        [*FINDING_ADD, "--text", "ok", "--kind", "fact", "--status", "supported", "--cites", "S-01:pX"],
        [*FINDING_ADD, "--text", "ok", "--kind", "fact", "--status", "supported", "--cites", ""],
        # A good citation beside a bad one still writes nothing.
        [*FINDING_ADD, "--text", "ok", "--kind", "fact", "--status", "supported", "--cites", "S-01", "--cites", "S-99"],
        [*FINDING_ADD, "--text", "ok", "--kind", "inference", "--status", "unsure", "--supersedes", "RF-01"],
        [*FINDING_ADD, "--text", "ok", "--kind", "inference", "--status", "unsure", "--supersedes", "S-01"],
    ):
        _refused(args)
    assert _tree(root) == before


def test_the_command_refuses_a_project_without_a_research_document(tmp_path, monkeypatch):
    root = _checkout(tmp_path / "local", monkeypatch)
    _ok(["new", "Plain"])
    before = _tree(root)

    shown = _refused([*FINDING_ADD, "--text", "ok", "--kind", "inference", "--status", "unsure"])

    assert "plain" in shown and document.RESEARCH_FILENAME in shown
    assert _tree(root) == before


# --- the module on its own --------------------------------------------------------


def test_parse_citation_reads_a_source_and_an_optional_page():
    assert findings.parse_citation("S-01") == ("S-01", None)
    assert findings.parse_citation("S-12:p7") == ("S-12", 7)
    assert findings.parse_citation(" S-01:p10 ") == ("S-01", 10)
    for bad in ("", "S01", "S-1x", "S-01:3", "S-01:p", "S-01:p0", "S-01:pX", "RF-01", "S-01:p1:p2", "s-01"):
        with pytest.raises(SpecfloError, match="citation"):
            findings.parse_citation(bad)


def test_add_finding_validates_before_it_touches_the_ledger(tmp_path, monkeypatch):
    root, cfg = _with_sources(tmp_path, monkeypatch)
    path = ledger.FINDINGS.path(root, cfg, "thing")
    before = path.read_bytes()

    with pytest.raises(SpecfloError, match="citation"):
        findings.add_finding(root, cfg, "thing", "A fact.", "fact", "supported", today=TODAY)
    with pytest.raises(SpecfloError, match="S-99"):
        findings.add_finding(root, cfg, "thing", "A fact.", "fact", "supported", cites=["S-99"], today=TODAY)
    with pytest.raises(SpecfloError, match="no pages"):
        findings.add_finding(root, cfg, "thing", "A fact.", "fact", "supported", cites=["S-02:p1"], today=TODAY)
    with pytest.raises(SpecfloError, match="page 11 beyond 10 pages"):
        findings.add_finding(root, cfg, "thing", "A fact.", "fact", "supported", cites=["S-01:p11"], today=TODAY)
    with pytest.raises(SpecfloError, match="kind"):
        findings.add_finding(root, cfg, "thing", "A fact.", "guess", "supported", cites=["S-01"], today=TODAY)
    with pytest.raises(SpecfloError, match="status"):
        findings.add_finding(root, cfg, "thing", "A fact.", "fact", "maybe", cites=["S-01"], today=TODAY)
    with pytest.raises(SpecfloError):
        findings.add_finding(root, cfg, "thing", "", "inference", "unsure", today=TODAY)
    assert path.read_bytes() == before

    entry = findings.add_finding(
        root, cfg, "thing", "A fact.", "fact", "supported", cites=["S-01:p10", "S-02"], today="2026-10-17",
    )
    assert entry.id == "RF-01"
    assert entry.fields == (("Kind", "fact"), ("Status", "supported"), ("Cites", "S-01:p10"), ("Cites", "S-02"))
    assert "updated: 2026-10-17\n" in path.read_text()


# --- the guide ------------------------------------------------------------------


def test_the_guide_has_a_row_for_finding_add_between_destination_set_and_session_close(
    tmp_path, monkeypatch
):
    rows = [entry for entry in guide.COMMANDS if entry["group"] == "research"]
    names = [row["name"] for row in rows]
    assert names.index("research destination set") < names.index("research finding add") < names.index(
        "research session close"
    )
    row = next(row for row in rows if row["name"] == "research finding add")
    assert "--text <text> --kind fact|inference --status supported|contradicted|unsure" in row["args"]
    assert "[--cites S-NN[:pN]]..." in row["args"] and "[--supersedes RF-NN]" in row["args"]
    assert row["summary"].isascii() and row["args"].isascii()

    monkeypatch.chdir(tmp_path)
    out = _ok(["guide"]).output
    assert "\n    research finding add " in out
    assert out.index("\n    research destination set ") < out.index("\n    research finding add ")
    assert out.index("\n    research finding add ") < out.index("\n    research session close ")

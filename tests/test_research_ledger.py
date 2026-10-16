"""The research document and ledgers: the format every research command shares.

A research project keeps ``research.md`` (Destination, Usual user, Decisions,
Log) and four ledgers under ``research/``. The ledger code mints S-NN, Q-NN
and RF-NN under the project's research lock, never reuses an ID, offers no
delete, and supersedes an entry by marking it with the ID that replaces it.
Every path the package returns is inside the project folder.
"""

import multiprocessing
import re

import pytest

from specflo import brainstorm, config, markdown, projects
from specflo.errors import SpecfloError
from specflo.research import document, ledger, scaffold

TODAY = "2026-10-16"
EM_DASH = "\u2014"


def _checkout(tmp_path, *names):
    """A checkout holding one project per name, with the research files scaffolded."""
    cfg = config.init_config(tmp_path)
    for name in names:
        projects.create_project(tmp_path, cfg, name, created=TODAY)
        scaffold(tmp_path, cfg, projects.slugify(name), today=TODAY)
    return tmp_path, cfg


def _project(root, slug):
    return root / "docs" / "projects" / slug


def _inside(path, folder) -> bool:
    return path.resolve().is_relative_to(folder.resolve())


# --- paths --------------------------------------------------------------------


def test_every_path_helper_points_inside_the_project_folder(tmp_path):
    root, cfg = _checkout(tmp_path, "Alpha")
    folder = _project(root, "alpha")

    paths = [
        document.research_path(root, cfg, "alpha"),
        document.research_dir(root, cfg, "alpha"),
        ledger.source_dir(root, cfg, "alpha", "S-07"),
        *(each.path(root, cfg, "alpha") for each in ledger.LEDGERS),
    ]

    assert all(_inside(path, folder) for path in paths), paths
    assert document.research_path(root, cfg, "alpha") == folder / "research.md"
    assert document.research_dir(root, cfg, "alpha") == folder / "research"
    assert ledger.source_dir(root, cfg, "alpha", "S-07") == folder / "research" / "sources" / "S-07"
    assert [each.path(root, cfg, "alpha").name for each in ledger.LEDGERS] == [
        "sources.md", "questions.md", "findings.md", "glossary.md",
    ]


@pytest.mark.parametrize("source_id", ["../x", "S-1x", "RF-01", "S-", "", "S-01/.."])
def test_source_dir_refuses_anything_but_a_source_id(tmp_path, source_id):
    root, cfg = _checkout(tmp_path, "Alpha")

    with pytest.raises(SpecfloError):
        ledger.source_dir(root, cfg, "alpha", source_id)


@pytest.mark.parametrize("slug", ["../alpha", "Alpha", "", "a/b", ".alpha"])
def test_path_helpers_refuse_a_slug_that_is_not_one(tmp_path, slug):
    root, cfg = _checkout(tmp_path, "Alpha")

    for helper in (document.research_path, document.research_dir, ledger.SOURCES.path):
        with pytest.raises(SpecfloError):
            helper(root, cfg, slug)


# --- research.md ----------------------------------------------------------------


def test_start_document_renders_the_four_sections_and_never_clobbers(tmp_path):
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Alpha Beta", created=TODAY)

    path, created = document.start_document(tmp_path, cfg, "alpha-beta", today=TODAY)

    assert created and path == _project(tmp_path, "alpha-beta") / "research.md"
    doc = path.read_text()
    assert doc.startswith(f"---\nproject: alpha-beta\ncreated: {TODAY}\nupdated: {TODAY}\n---\n")
    assert "# Research: Alpha Beta\n" in doc
    assert markdown.section_headers(doc) == [
        "# Research: Alpha Beta", "## Destination", "## Usual user", "## Decisions", "## Log",
    ]
    assert doc.isascii()

    path.write_text(doc + "hand-written\n")
    again, created_again = document.start_document(tmp_path, cfg, "alpha-beta", today=TODAY)
    assert (again, created_again) == (path, False)
    assert path.read_text().endswith("hand-written\n")


def test_start_document_refuses_a_project_that_does_not_exist(tmp_path):
    cfg = config.init_config(tmp_path)

    with pytest.raises(SpecfloError):
        document.start_document(tmp_path, cfg, "nowhere", today=TODAY)


def test_a_new_document_parses_as_unknown_destination_no_user_and_an_empty_log(tmp_path):
    root, cfg = _checkout(tmp_path, "Alpha")

    parsed = document.load(root, cfg, "alpha")

    assert parsed.destination == document.UNKNOWN_DESTINATION == "unknown"
    assert parsed.reason is None
    assert parsed.usual_user == ""
    assert parsed.log == ()
    assert document.DESTINATIONS == ("build", "inform", "learn", "unknown")


def test_parse_reads_the_destination_its_reason_and_the_usual_user():
    doc = (
        "---\nproject: alpha\ncreated: d\nupdated: d\n---\n\n# Research: Alpha\n\n"
        "## Destination\n<!-- note -->\n- Destination: build\n- Reason: the user wants a tool\n\n"
        "## Usual user\n<!-- note -->\nA broker with twenty years in commercial real estate.\n\n"
        "## Decisions\n\n"
        "## Log\n"
    )

    parsed = document.parse(doc)

    assert parsed.destination == "build"
    assert parsed.reason == "the user wants a tool"
    assert parsed.usual_user == "A broker with twenty years in commercial real estate."


def test_parse_refuses_a_document_missing_one_of_its_sections():
    doc = "---\nproject: alpha\n---\n\n# Research: Alpha\n\n## Destination\n\n## Log\n"

    with pytest.raises(SpecfloError, match="Usual user"):
        document.parse(doc)


def test_append_log_adds_dated_entries_in_order_with_an_optional_kind(tmp_path):
    root, cfg = _checkout(tmp_path, "Alpha")

    first = document.append_log(root, cfg, "alpha", "Destination unknown -> build: a tool", today=TODAY)
    second = document.append_log(
        root, cfg, "alpha", "Summary: looked at leases. Next: read the act.", kind="session",
        today="2026-10-17",
    )

    assert first == document.LogEntry(date=TODAY, kind=None, text="Destination unknown -> build: a tool")
    assert second.kind == "session"
    doc = document.research_path(root, cfg, "alpha").read_text()
    assert markdown.section_body(doc, "## Log").endswith(
        f"- {TODAY}: Destination unknown -> build: a tool\n"
        "- 2026-10-17 session: Summary: looked at leases. Next: read the act.\n"
    )
    assert "updated: 2026-10-17\n" in doc
    assert document.parse(doc).log == (first, second)


def test_append_log_refusals_write_nothing(tmp_path):
    root, cfg = _checkout(tmp_path, "Alpha")
    path = document.research_path(root, cfg, "alpha")
    before = path.read_bytes()

    for text, kind in (("", None), ("  ", None), ("one\ntwo", None), ("ok", "Not A Kind"), ("ok", "x y")):
        with pytest.raises(SpecfloError):
            document.append_log(root, cfg, "alpha", text, kind=kind, today=TODAY)
        assert path.read_bytes() == before, (text, kind)

    path.unlink()
    with pytest.raises(SpecfloError, match="research"):
        document.append_log(root, cfg, "alpha", "late", today=TODAY)
    assert not path.exists()


def test_the_decisions_section_takes_a_decision_in_the_brainstorm_format(tmp_path):
    """The brainstorm's decision code is what writes and reads decisions here later,
    so the section carries the exact header it looks for and an entry it writes
    parses back, with the Log behind it left alone."""
    root, cfg = _checkout(tmp_path, "Alpha")
    path = document.research_path(root, cfg, "alpha")
    document.append_log(root, cfg, "alpha", "before the decision", today=TODAY)
    doc = path.read_text()
    assert document.DECISIONS_HEADER == "## Decisions"
    assert "## Decisions" in markdown.section_headers(doc)

    decision_id = markdown.next_id(doc, "D-")
    entry = f"### {decision_id} {EM_DASH} Keep the glossary\n- Rationale: the expert's words\n- Status: active\n"
    doc = markdown.append_to_section(doc, document.DECISIONS_HEADER, entry)

    decisions = brainstorm._decision_entries(doc, "research")
    assert [(d.id, d.text, d.rationale, d.status) for d in decisions] == [
        (decision_id, "Keep the glossary", "the expert's words", "active"),
    ]
    assert markdown.next_id(doc, "D-") != decision_id
    assert markdown.section_body(doc, "## Log").strip().endswith("before the decision")
    assert [e.text for e in document.parse(doc).log] == ["before the decision"]


# --- the ledgers --------------------------------------------------------------


def test_the_four_ledgers_have_their_names_prefixes_and_files():
    assert [(l.name, l.prefix) for l in ledger.LEDGERS] == [
        ("sources", "S"), ("questions", "Q"), ("findings", "RF"), ("glossary", None),
    ]
    assert ledger.SOURCES is ledger.LEDGERS[0]
    assert ledger.QUESTIONS is ledger.LEDGERS[1]
    assert ledger.FINDINGS is ledger.LEDGERS[2]
    assert ledger.GLOSSARY is ledger.LEDGERS[3]
    assert ledger.by_name("findings") is ledger.FINDINGS
    with pytest.raises(SpecfloError):
        ledger.by_name("notes")


def test_scaffold_creates_the_document_and_the_four_ledgers_once(tmp_path):
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Alpha", created=TODAY)
    folder = _project(tmp_path, "alpha")

    created = scaffold(tmp_path, cfg, "alpha", today=TODAY)

    assert created == [
        folder / "research.md",
        folder / "research" / "sources.md",
        folder / "research" / "questions.md",
        folder / "research" / "findings.md",
        folder / "research" / "glossary.md",
    ]
    assert all(path.is_file() for path in created)
    before = {path: path.read_bytes() for path in created}
    assert scaffold(tmp_path, cfg, "alpha", today="2027-01-01") == []
    assert {path: path.read_bytes() for path in created} == before
    for path in created[1:]:
        assert path.read_text().isascii()


def test_a_new_ledger_has_a_frontmatter_a_title_and_its_section(tmp_path):
    root, cfg = _checkout(tmp_path, "Alpha")

    doc = ledger.FINDINGS.path(root, cfg, "alpha").read_text()

    assert doc.startswith(f"---\nproject: alpha\nledger: findings\ncreated: {TODAY}\nupdated: {TODAY}\n---\n")
    assert markdown.section_headers(doc) == ["# Findings: alpha", "## Findings"]
    assert ledger.FINDINGS.entries(root, cfg, "alpha") == []


def test_add_mints_ids_in_order_and_renders_the_heading_and_fields(tmp_path):
    root, cfg = _checkout(tmp_path, "Alpha")

    first = ledger.SOURCES.add(
        root, cfg, "alpha", "Lease act 2024",
        [("Kind", "document"), ("Date", TODAY), ("Sha256", "abc")], today=TODAY,
    )
    second = ledger.SOURCES.add(root, cfg, "alpha", "Interview 1", [("Kind", "interview")], today=TODAY)

    assert (first.id, second.id) == ("S-01", "S-02")
    assert first.title == "Lease act 2024"
    assert first.get("Kind") == "document" and first.get("Sha256") == "abc"
    assert first.get("Missing") is None and first.all("Missing") == []
    doc = ledger.SOURCES.path(root, cfg, "alpha").read_text()
    assert (
        "### S-01 - Lease act 2024\n- Kind: document\n- Date: 2026-10-16\n- Sha256: abc\n\n"
        "### S-02 - Interview 1\n- Kind: interview\n"
    ) in doc
    assert markdown.section_body(doc, "## Sources").count("### ") == 2
    assert ledger.SOURCES.entries(root, cfg, "alpha") == [first, second]


def test_each_ledger_counts_on_its_own_and_ids_run_past_99_with_more_digits(tmp_path):
    root, cfg = _checkout(tmp_path, "Alpha")
    ledger.SOURCES.add(root, cfg, "alpha", "A source", today=TODAY)
    path = ledger.FINDINGS.path(root, cfg, "alpha")
    path.write_text(path.read_text() + "### RF-99 - Hand-written\n- Kind: fact\n- Status: unsure\n\n")

    question = ledger.QUESTIONS.add(root, cfg, "alpha", "Who signs?", [("By", "user")], today=TODAY)
    finding = ledger.FINDINGS.add(root, cfg, "alpha", "Rent rose 4%", [("Kind", "fact")], today=TODAY)

    assert question.id == "Q-01"
    assert finding.id == "RF-100"
    assert ledger.FINDINGS.add(root, cfg, "alpha", "Next", today=TODAY).id == "RF-101"


def test_a_mention_of_a_higher_id_in_the_ledger_is_never_handed_out_again(tmp_path):
    root, cfg = _checkout(tmp_path, "Alpha")
    ledger.FINDINGS.add(root, cfg, "alpha", "One", [("Note", "see RF-07 later")], today=TODAY)

    assert ledger.FINDINGS.add(root, cfg, "alpha", "Two", today=TODAY).id == "RF-08"


def test_entries_parses_every_entry_with_its_fields_status_and_repeated_fields(tmp_path):
    root, cfg = _checkout(tmp_path, "Alpha")
    ledger.QUESTIONS.add(
        root, cfg, "alpha", "Which market first?",
        [("By", "developer"), ("Option", "Toronto"), ("Option", "Calgary"),
         ("Default", "Toronto"), ("Why", "the user lives there"), ("Status", "open")],
        today=TODAY,
    )
    path = ledger.QUESTIONS.path(root, cfg, "alpha")
    # Free text and a fenced example inside an entry are not fields or entries.
    path.write_text(
        path.read_text()
        + "### Q-02 - Hand-written\nA note between fields.\n- By: user\n```\n### Q-09 - quoted\n- Status: closed\n```\n- Status: open\n"
    )

    entries = ledger.QUESTIONS.entries(root, cfg, "alpha")

    assert [e.id for e in entries] == ["Q-01", "Q-02"]
    first, second = entries
    assert first.title == "Which market first?"
    assert first.all("Option") == ["Toronto", "Calgary"]
    assert first.get("Option") == "Toronto"
    assert first.status == "open"
    assert first.supersedes is None and first.superseded_by is None and first.active
    assert first.fields == (
        ("By", "developer"), ("Option", "Toronto"), ("Option", "Calgary"),
        ("Default", "Toronto"), ("Why", "the user lives there"), ("Status", "open"),
    )
    assert second.fields == (("By", "user"), ("Status", "open"))
    assert ledger.QUESTIONS.show(root, cfg, "alpha", "Q-02") == second
    with pytest.raises(SpecfloError, match="Q-09"):
        ledger.QUESTIONS.show(root, cfg, "alpha", "Q-09")


def test_an_entry_without_a_status_line_has_no_status(tmp_path):
    root, cfg = _checkout(tmp_path, "Alpha")

    source = ledger.SOURCES.add(root, cfg, "alpha", "A source", [("Kind", "web")], today=TODAY)

    assert source.status is None


def test_add_with_supersedes_marks_the_old_entry_with_the_new_id(tmp_path):
    root, cfg = _checkout(tmp_path, "Alpha")
    old = ledger.FINDINGS.add(
        root, cfg, "alpha", "Rent rose 4%", [("Kind", "fact"), ("Status", "supported")], today=TODAY,
    )

    new = ledger.FINDINGS.add(
        root, cfg, "alpha", "Rent rose 4.5%", [("Kind", "fact"), ("Status", "supported")],
        supersedes="RF-01", today="2026-10-17",
    )

    assert (old.id, new.id, new.supersedes) == ("RF-01", "RF-02", "RF-01")
    doc = ledger.FINDINGS.path(root, cfg, "alpha").read_text()
    assert (
        "### RF-01 - Rent rose 4%\n- Kind: fact\n- Status: supported\n- Superseded by: RF-02\n\n"
        "### RF-02 - Rent rose 4.5%\n- Kind: fact\n- Status: supported\n- Supersedes: RF-01\n"
    ) in doc
    assert "updated: 2026-10-17\n" in doc
    entries = ledger.FINDINGS.entries(root, cfg, "alpha")
    assert [(e.id, e.status, e.superseded_by, e.active) for e in entries] == [
        ("RF-01", "supported", "RF-02", False), ("RF-02", "supported", None, True),
    ]
    assert [e.id for e in ledger.FINDINGS.entries(root, cfg, "alpha", include_superseded=False)] == ["RF-02"]
    # The replaced entry stays, and its number is never handed out again.
    assert ledger.FINDINGS.add(root, cfg, "alpha", "Three", today=TODAY).id == "RF-03"


def test_supersede_marks_an_existing_pair_both_ways(tmp_path):
    root, cfg = _checkout(tmp_path, "Alpha")
    ledger.QUESTIONS.add(root, cfg, "alpha", "Old", [("By", "user"), ("Status", "open")], today=TODAY)
    ledger.QUESTIONS.add(root, cfg, "alpha", "New", [("By", "user"), ("Status", "open")], today=TODAY)

    marked = ledger.QUESTIONS.supersede(root, cfg, "alpha", "Q-01", "Q-02", today="2026-10-17")

    assert marked.id == "Q-01" and marked.superseded_by == "Q-02"
    entries = {e.id: e for e in ledger.QUESTIONS.entries(root, cfg, "alpha")}
    assert entries["Q-01"].superseded_by == "Q-02" and entries["Q-01"].status == "open"
    assert entries["Q-02"].supersedes == "Q-01"
    assert "updated: 2026-10-17\n" in ledger.QUESTIONS.path(root, cfg, "alpha").read_text()


def test_supersede_refusals_write_nothing(tmp_path):
    root, cfg = _checkout(tmp_path, "Alpha")
    for title in ("One", "Two", "Three"):
        ledger.FINDINGS.add(root, cfg, "alpha", title, [("Kind", "fact")], today=TODAY)
    ledger.FINDINGS.supersede(root, cfg, "alpha", "RF-01", "RF-02", today=TODAY)
    path = ledger.FINDINGS.path(root, cfg, "alpha")
    before = path.read_bytes()

    for old, new in (("RF-01", "RF-03"), ("RF-09", "RF-03"), ("RF-03", "RF-09"), ("RF-03", "RF-03"), ("S-01", "RF-03")):
        with pytest.raises(SpecfloError):
            ledger.FINDINGS.supersede(root, cfg, "alpha", old, new, today=TODAY)
        assert path.read_bytes() == before, (old, new)
    for supersedes in ("RF-01", "RF-09", "Q-01", "RF-1x"):
        with pytest.raises(SpecfloError):
            ledger.FINDINGS.add(root, cfg, "alpha", "Four", supersedes=supersedes, today=TODAY)
        assert path.read_bytes() == before, supersedes


def test_add_refusals_write_nothing(tmp_path):
    root, cfg = _checkout(tmp_path, "Alpha")
    path = ledger.SOURCES.path(root, cfg, "alpha")
    before = path.read_bytes()

    bad = [
        ("", []),
        ("  ", []),
        ("one\ntwo", []),
        ("ok", [("Kind", "a\nb")]),
        ("ok", [("Kind", "a\x0c- Status: closed")]),
        ("ok", [("", "x")]),
        ("ok", [("Kind: more", "x")]),
        ("ok", [("1st", "x")]),
        ("ok", [("Supersedes", "S-01")]),
        ("ok", [("superseded by", "S-01")]),
    ]
    for title, fields in bad:
        with pytest.raises(SpecfloError):
            ledger.SOURCES.add(root, cfg, "alpha", title, fields, today=TODAY)
        assert path.read_bytes() == before, (title, fields)

    with pytest.raises(SpecfloError):
        ledger.GLOSSARY.add(root, cfg, "alpha", "Cap rate", [("Definition", "x")], today=TODAY)
    assert ledger.GLOSSARY.entries(root, cfg, "alpha") == []

    path.unlink()
    with pytest.raises(SpecfloError, match="sources"):
        ledger.SOURCES.add(root, cfg, "alpha", "Late", today=TODAY)
    assert not path.exists()


def test_a_ledger_without_its_section_is_refused_by_name(tmp_path):
    root, cfg = _checkout(tmp_path, "Alpha")
    path = ledger.SOURCES.path(root, cfg, "alpha")
    path.write_text("# Sources: alpha\n\nno section here\n")

    with pytest.raises(SpecfloError, match="## Sources"):
        ledger.SOURCES.add(root, cfg, "alpha", "Late", today=TODAY)
    assert path.read_text() == "# Sources: alpha\n\nno section here\n"


def test_set_fields_replaces_single_and_repeated_fields_in_place(tmp_path):
    root, cfg = _checkout(tmp_path, "Alpha")
    ledger.QUESTIONS.add(
        root, cfg, "alpha", "Which market?",
        [("By", "developer"), ("Option", "Toronto"), ("Option", "Calgary"), ("Status", "open")],
        today=TODAY,
    )
    ledger.QUESTIONS.add(root, cfg, "alpha", "Second", [("By", "user"), ("Status", "open")], today=TODAY)

    resolved = ledger.QUESTIONS.set_fields(
        root, cfg, "alpha", "Q-01",
        [("Status", "resolved"), ("Resolution", "Toronto"), ("Resolved by", "Rob"),
         ("Option", "Toronto only")],
        today="2026-10-17",
    )

    assert resolved.id == "Q-01"
    assert resolved.fields == (
        ("By", "developer"), ("Option", "Toronto only"), ("Status", "resolved"),
        ("Resolution", "Toronto"), ("Resolved by", "Rob"),
    )
    doc = ledger.QUESTIONS.path(root, cfg, "alpha").read_text()
    assert (
        "### Q-01 - Which market?\n- By: developer\n- Option: Toronto only\n- Status: resolved\n"
        "- Resolution: Toronto\n- Resolved by: Rob\n\n### Q-02 - Second\n- By: user\n- Status: open\n"
    ) in doc
    assert "updated: 2026-10-17\n" in doc
    before = doc
    for entry_id, fields in (("Q-09", [("Status", "x")]), ("Q-01", [("Superseded by", "Q-02")]),
                             ("Q-01", [("Status", "a\nb")])):
        with pytest.raises(SpecfloError):
            ledger.QUESTIONS.set_fields(root, cfg, "alpha", entry_id, fields, today=TODAY)
        assert ledger.QUESTIONS.path(root, cfg, "alpha").read_text() == before


def test_the_pure_helpers_work_on_a_term_keyed_ledger():
    doc = (
        "## Glossary\n\n"
        "### Cap rate\n- Definition: net income over price\n- Avoid: yield\n\n"
        "### Cap\n- Definition: a ceiling\n"
    )

    entries = ledger.parse_entries(doc, prefix=None)
    assert [(e.id, e.title, e.get("Definition")) for e in entries] == [
        ("Cap rate", "", "net income over price"), ("Cap", "", "a ceiling"),
    ]
    changed = ledger.set_fields(doc, "Cap", [("Definition", "an upper limit"), ("Avoid", "lid")], prefix=None)
    assert changed.endswith("### Cap\n- Definition: an upper limit\n- Avoid: lid\n")
    assert "- Definition: net income over price\n- Avoid: yield\n" in changed
    rendered = ledger.render_entry("Cap", "", [("Definition", "x")])
    assert rendered == "### Cap\n- Definition: x\n"
    assert ledger.render_entry("S-01", "A title", []) == "### S-01 - A title\n"


def _add_source(args):
    root, i = args
    cfg = config.load_config(root)
    return ledger.SOURCES.add(root, cfg, "alpha", f"source {i}", [("Kind", "web")]).id


def test_concurrent_adds_get_distinct_sequential_ids(tmp_path):
    root, cfg = _checkout(tmp_path, "Alpha")
    jobs = [(root, i) for i in range(8)]

    with multiprocessing.get_context("fork").Pool(len(jobs)) as pool:
        ids = pool.map(_add_source, jobs)

    assert sorted(ids) == [f"S-{n:02d}" for n in range(1, 9)]
    doc = ledger.SOURCES.path(root, cfg, "alpha").read_text()
    for i in range(8):
        assert doc.count(f" - source {i}\n") == 1
    assert [e.id for e in ledger.SOURCES.entries(root, cfg, "alpha")] == sorted(ids)


def test_the_research_package_offers_no_delete():
    """An entry is replaced by supersede, never removed: no public name of the
    package deletes, removes or drops anything."""
    import specflo.research as package

    names = [
        f"{owner}.{name}"
        for owner, module in (("research", package), ("document", document), ("ledger", ledger),
                              ("Ledger", ledger.Ledger), ("Entry", ledger.Entry))
        for name in dir(module)
        if not name.startswith("_")
    ]
    assert not [n for n in names if re.search(r"delete|remove|drop|unlink|clear", n, re.I)], names

from specflo import markdown


def test_next_id_mints_with_a_custom_prefix():
    assert markdown.next_id("", "REQ-") == "REQ-01"
    doc = "### REQ-01 — a\n### REQ-02 — b\n"
    assert markdown.next_id(doc, "REQ-") == "REQ-03"


def test_next_id_ignores_fenced_headers():
    doc = "```\n### REQ-09 — fake\n```\n### REQ-01 — real\n"
    assert markdown.next_id(doc, "REQ-") == "REQ-02"  # the fenced REQ-09 must not count


def test_mark_superseded_flips_the_named_entry_status():
    doc = "### REQ-01 — a\n- Acceptance: x\n- Status: active\n"
    out = markdown.mark_superseded(doc, "REQ-01", "REQ-02")
    assert "- Status: superseded by REQ-02" in out


def test_placeholder_issues_flags_words_not_substrings():
    assert markdown.placeholder_issues("we still need TODO here") == [
        "placeholder text found: TODO"
    ]
    assert markdown.placeholder_issues("the AUTODOCK pipeline") == []  # substring, not a word
    assert markdown.placeholder_issues("unknown ???") == ["placeholder text found: ???"]


def test_section_body_is_level_aware():
    doc = (
        "## Boundaries\n"
        "### In scope\n"
        "- the CLI\n"
        "### Out of scope\n"
        "- the GUI\n"
        "## Open questions\n"
        "none\n"
    )
    # H3 'In scope' stops at the sibling H3 'Out of scope'
    assert markdown.section_body(doc, "### In scope").strip() == "- the CLI"
    # H3 'Out of scope' stops at the next H2
    assert markdown.section_body(doc, "### Out of scope").strip() == "- the GUI"
    # H2 'Boundaries' spans its H3 children
    boundaries = markdown.section_body(doc, "## Boundaries")
    assert "### In scope" in boundaries and "### Out of scope" in boundaries

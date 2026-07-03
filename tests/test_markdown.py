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


_ENTRY = (
    "## Tasks\n\n"
    "### T-01 — first\n"
    "- Acceptance: passes\n"
    "- Progress: pending\n"
    "- Status: active\n\n"
    "### T-02 — second\n"
    "- Progress: pending\n"
    "- Status: active\n"
)


def test_set_entry_field_updates_existing_line():
    out = markdown.set_entry_field(_ENTRY, "T-01", "Progress", "done")
    assert "### T-01 — first\n- Acceptance: passes\n- Progress: done\n" in out
    # only the named entry changes
    assert "### T-02 — second\n- Progress: pending\n" in out


def test_set_entry_field_inserts_when_missing():
    out = markdown.set_entry_field(_ENTRY, "T-01", "Blocked", "waiting on API")
    assert "- Blocked: waiting on API\n" in out
    # inserted within the T-01 block, before T-02
    assert out.index("- Blocked: waiting on API") < out.index("### T-02")


def test_clear_entry_field_removes_line():
    blocked = markdown.set_entry_field(_ENTRY, "T-01", "Blocked", "x")
    out = markdown.clear_entry_field(blocked, "T-01", "Blocked")
    assert "Blocked" not in out


def test_mark_superseded_still_flips_status():
    out = markdown.mark_superseded(_ENTRY, "T-01", "T-02")
    assert "### T-01 — first" in out
    assert "- Status: superseded by T-02\n" in out


def test_count_entry_field_counts_repeated_fields():
    doc = ("### T-01 — a\n- Milestone: M-01\n- Milestone: M-02\n- Status: active\n"
           "### T-02 — b\n- Milestone: M-01\n")
    assert markdown.count_entry_field(doc, "T-01", "Milestone") == 2   # hand-edited duplicate
    assert markdown.count_entry_field(doc, "T-02", "Milestone") == 1   # well-formed single
    assert markdown.count_entry_field(doc, "T-02", "Depends on") == 0  # field absent
    assert markdown.count_entry_field(doc, "T-99", "Milestone") == 0   # entry not present

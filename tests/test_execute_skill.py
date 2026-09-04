from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / "skills" / "specflo-execute" / "SKILL.md"


def test_skill_file_exists():
    assert SKILL.is_file()


def test_skill_has_the_anatomy_sections():
    text = SKILL.read_text().lower()
    for heading in ["when to use", "hard-gate", "process", "rationalization",
                    "red flags", "verification"]:
        assert heading in text, f"missing section: {heading}"


def test_skill_references_the_real_commands():
    text = SKILL.read_text()
    for cmd in ["specflo task show", "specflo task start", "specflo task done",
                "specflo validate execute", "specflo advance", "specflo checkpoint"]:
        assert cmd in text, f"missing command reference: {cmd}"


def test_skill_gives_thin_milestone_guidance():
    # REQ-17: the executor honours the soft boundary verify beat and the
    # working-ahead label, deferring the mechanics to the milestone commands.
    text = SKILL.read_text()
    assert "specflo milestone" in text      # reference the commands, don't reimplement
    low = text.lower()
    assert "boundary" in low                # the soft milestone-boundary verify beat
    assert "exit checklist" in low          # surfaced/verified at the boundary
    assert "working ahead" in low or "working-ahead" in low


def test_skill_names_the_review_round_commands(tmp_path=None):
    # review-rounds REQ-18: the final whole-branch review is recorded through the
    # CLI, so the readiness step and the checklist both name the commands.
    text = SKILL.read_text()
    readiness = text.split("**Readiness**", 1)[1].split("## ", 1)[0]
    checklist = text.split("## Verification", 1)[1]
    for section in (readiness, checklist):
        assert "specflo review start" in section
        assert "specflo review done" in section


def test_skill_readiness_trigger_is_not_made_unsatisfiable_by_the_gate():
    # review-rounds round 2, G4: the review gate lives inside `validate execute`,
    # so that command cannot be clean before the review it is meant to trigger.
    # The trigger is the task state; the failing validator is the reminder.
    text = SKILL.read_text()
    readiness = text.split("**Readiness**", 1)[1].split("## ", 1)[0]
    checklist = text.split("## Verification", 1)[1]

    assert "`specflo validate execute` is clean" not in readiness
    assert "no actionable task" in readiness.lower()          # the real trigger
    for section in (readiness, checklist):
        assert "until the round is closed" in section.lower()


def _fan_out_section():
    text = SKILL.read_text()
    assert "## Fan-out" in text, "missing '## Fan-out' section"
    return text.split("## Fan-out", 1)[1].split("\n## ", 1)[0]


def test_skill_fan_out_section_reads_the_mode_and_keeps_linear_unchanged():
    # fan-out-plans REQ-15: the orchestrator reads the mode from status and the
    # linear loop is untouched.
    section = _fan_out_section()
    assert "specflo status" in section
    low = section.lower()
    assert "linear" in low and "unchanged" in low


def test_skill_fan_out_section_describes_the_dispatch_protocol():
    section = _fan_out_section()
    low = section.lower()
    # the frontier
    assert "task list --json" in section and "ready" in low
    # per ready task: start, then one subagent with the brief and its pool member
    assert "specflo task start" in section
    assert "one subagent" in low
    assert "specflo task show" in section
    assert "pool" in low and "member" in low
    # subagent discipline
    assert "never run" in low and "git" in low and "specflo" in low
    assert "files" in low and "new test files" in low
    # orchestrator closes the task
    assert "re-run" in low and "verify" in low
    assert "by path" in low
    assert "one task per commit" in low
    assert "specflo task done" in section
    # long tasks, the user pool, and slot recovery
    assert "background" in low
    assert "never delegated" in low and "user" in low
    assert "specflo task reopen" in section
    assert "dead" in low and "slot" in low


def test_skill_process_steps_are_kept_verbatim():
    # The fan-out section is additive: the existing per-task loop still reads
    # exactly as before.
    text = SKILL.read_text()
    for phrase in [
        "1. `specflo task start T-NN` (→ in_progress).",
        "Run the task's **Verify** step; capture the passing evidence.",
        "**Commit** one atomic commit for the task — stage only the files it",
        "6. `specflo task done T-NN` (it refuses unless the task is in_progress).",
        "7. Next: `specflo task show` again.",
    ]:
        assert phrase in text, f"process step changed: {phrase!r}"


def _edit_and_note_section():
    text = SKILL.read_text()
    assert "## Correcting and annotating tasks" in text
    return text.split("## Correcting and annotating tasks", 1)[1].split("\n## ", 1)[0]


def test_skill_directs_corrections_to_task_edit_and_task_note():
    # task-edit-and-task-note REQ-13: the two commands, the edit-before-done
    # rule, and the closed label set.
    section = _edit_and_note_section()
    assert "specflo task edit" in section
    assert "specflo task note" in section
    assert "specflo task add --supersedes" in section
    low = section.lower()
    assert "--force" in section and "[edit]" in low
    assert "superseded" in low and "frozen" in low
    for label in ("Note", "Design", "Resolution", "Descoped", "Edit"):
        assert label in section, f"missing note label: {label}"
    assert "--note" in section  # the ride-along on task done / task reopen


def test_skill_forbids_hand_editing_plan_md():
    text = SKILL.read_text().lower()
    assert "never hand-edit `plan.md`" in text or "never hand-edit plan.md" in text


def _self_review_step():
    # Step 2.4 of the per-task loop: from "4. **Self-review**" up to the commit step.
    text = SKILL.read_text()
    assert "4. **Self-review**" in text
    return text.split("4. **Self-review**", 1)[1].split("5. **Commit**", 1)[0]


def test_skill_keeps_record_keywords_out_of_shipped_work():
    # The rule that record vocabulary never enters anything that ships, stated
    # once in the self-review step, with task note as the place for traceability.
    step = " ".join(_self_review_step().split())
    for family in ("REQ-", "T-", "D-", "M-", "review round", "finding", "probe", "project slug"):
        assert family in step, f"missing keyword family: {family}"
    low = step.lower()
    for surface in ("code", "comments", "docstrings", "tests", "string literals", "commit messages"):
        assert surface in low, f"missing surface: {surface}"
    assert "write the reason" in low and "leave it out" in low
    assert "specflo task note" in step
    assert "task done --note" in step

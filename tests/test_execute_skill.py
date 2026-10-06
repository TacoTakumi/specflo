import re
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


def _flat(section: str) -> str:
    # Prose wraps; a phrase is looked for in the text with its line breaks folded.
    return " ".join(section.split())


def test_skill_fan_out_takes_a_lease_before_a_daemon_pool_task_and_releases_it_after():
    # A hosted plan's Needs line may name a pool the daemon holds. The member is
    # then taken for the task and given back when the task is over, however it
    # went: a lease that is kept holds a slot other orchestrators wait for.
    section = _flat(_fan_out_section())
    low = section.lower()
    assert "hosted mode" in low
    assert "daemon pool" in low
    assert "`specflo lease request <pool>`" in section
    assert "`specflo lease release <lease>`" in section
    assert section.index("specflo lease request") < section.index("specflo lease release")
    assert "before the task" in low
    assert "after the task" in low
    assert "success or failure" in low
    # the failure paths are named, so none reads as an exception to the rule
    assert "subagent" in low and "fails" in low
    assert "abandon" in low


def test_skill_fan_out_keeps_the_lease_step_out_of_local_mode():
    # The lease verbs need a daemon; a local plan has none and keeps its own pools.
    low = _flat(_fan_out_section()).lower()
    assert "hosted mode only" in low
    assert "local" in low and "refused" in low


def test_skill_fan_out_says_the_ready_set_counts_the_daemons_capacity():
    # The orchestrator does not second-guess the frontier: a task the CLI lists
    # as blocked on a daemon pool is not dispatched to wait on the lease verb.
    low = _flat(_fan_out_section()).lower()
    assert "daemon's capacity" in low
    assert "whatever the plan's own" in low
    assert "blocked" in low and "not dispatched" in low


def test_skill_fan_out_points_at_the_agent_skill_for_the_lease_verbs():
    section = _flat(_fan_out_section())
    assert "specflo-agent" in section
    assert "--wait" in section            # a request can block; the bound is the orchestrator's
    assert "specflo lease renew" not in section


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


# --- the converging review loop -------------------------------------------------------

AUTO_SKILL = SKILL.parents[1] / "specflo-auto" / "SKILL.md"


def _readiness():
    return " ".join(SKILL.read_text().split("**Readiness**", 1)[1].split("\n## ", 1)[0].split())


def test_readiness_describes_the_converging_review_loop():
    step = _readiness()
    for phrase in (
        "whole test suite once, before round 1",
        "specflo review start",
        "specflo review prompt",
        "specflo review finding add",
        "specflo review finding check",
        "specflo review done",
        "sets the verdict",
        "never the nits",
        "delta round",
        "specflo review start --over-budget",
        "specflo review waive --reason",
        "whole test suite again before completion",
    ):
        assert phrase in step, phrase
    # The prompt is handed over after the round opens.
    assert step.index("specflo review start") < step.index("specflo review prompt")


def _readiness_bullet(phrase):
    # The one readiness bullet that holds `phrase`, with its line breaks folded.
    raw = SKILL.read_text().split("**Readiness**", 1)[1].split("\n## ", 1)[0]
    bullets = [" ".join(b.split()) for b in re.split(r"\n\s+- ", raw)]
    hits = [b for b in bullets if phrase in b]
    assert len(hits) == 1, f"{phrase!r} is in {len(hits)} readiness bullets"
    return hits[0]


def test_readiness_records_a_finding_with_its_location():
    bullet = _readiness_bullet("specflo review finding add")
    assert "--at <file>:<line>[-<line>]" in bullet
    assert "blocker" in bullet and "should-fix" in bullet and "required" in bullet


def test_readiness_fixes_each_open_item_with_a_fix_task():
    bullet = _readiness_bullet("On changes-requested")
    low = bullet.lower()
    assert "`specflo task add --fixes F-NN`" in bullet
    # the fix task covers every path to the defect, each with a test
    assert "every path" in low and "a test for each" in low
    # its verify names the pin test, which goes red then green
    assert "pin test" in low
    assert "red then green" in low
    assert "fails before the fix and passes after" in low
    # the next round opens only once the fix tasks are done
    assert bullet.index("task add --fixes") < bullet.index("specflo review start")
    assert "no done fix task" in low
    assert "never the nits" in low
    # the bare fix-and-commit step the CLI now refuses is gone
    assert "fix the blocker and should-fix items, never the nits, commit" not in bullet


def test_readiness_defers_or_rejects_an_item_only_on_the_users_say():
    bullet = _readiness_bullet("specflo review finding reject")
    assert '`specflo review finding defer F-NN --do "<what>"`' in bullet
    assert '`specflo review finding reject F-NN --reason "<why>"`' in bullet
    assert "only on the user's say" in bullet
    low = bullet.lower()
    assert "follow-up" in low and "hosted" in low
    assert "settled" in low and "completion gate" in low


def test_readiness_offers_hardening():
    bullet = _readiness_bullet("specflo review start --harden")
    low = bullet.lower()
    assert "whole branch" in low and "outside the round budget" in low
    assert "hardened" in low and "fix task" in low
    assert "latest gate round" in low
    assert "two quiet harden rounds" in low and "the user's call" in low


def test_readiness_no_longer_withholds_earlier_findings_from_the_reviewer():
    text = SKILL.read_text()
    assert "Hand it the diff alone" not in text
    assert "not the fresh reviewer's to inherit" not in text


def test_auto_skill_names_the_review_budget_stop():
    text = " ".join(AUTO_SKILL.read_text().split())
    assert "review-budget" in text
    assert "--over-budget" in text and "specflo review waive" in text


# --- ad hoc work: briefs ------------------------------------------------------------


def _briefs_section():
    text = SKILL.read_text()
    assert "## Ad hoc work: briefs" in text
    return " ".join(text.split("## Ad hoc work: briefs", 1)[1].split("\n## ", 1)[0].split())


def test_skill_names_the_brief_commands():
    section = _briefs_section()
    for cmd in ["specflo brief add", "specflo brief set", "specflo decision add --brief B-NN",
                "specflo task add --from B-NN", "specflo review start --brief B-NN",
                "specflo decision list --diverges", "specflo task edit T-NN --implements B-NN"]:
        assert cmd in section, f"missing command reference: {cmd}"
    for flag in ["--acceptance-file", "--verify-file", "--text-file", "--diverges"]:
        assert flag in section, f"missing flag: {flag}"


def test_skill_sizes_the_ask_between_a_bare_task_and_a_brief():
    low = _briefs_section().lower()
    assert "a bare task is enough" in low
    assert "use a brief" in low
    for trigger in ["recon is needed", "pick to make", "more than one task", "changes a decision"]:
        assert trigger in low, f"missing brief trigger: {trigger}"
    assert "review" in low and "offer" in low

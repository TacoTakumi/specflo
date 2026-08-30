from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / "skills" / "specflo-plan" / "SKILL.md"


def test_skill_file_exists():
    assert SKILL.is_file()


def test_skill_has_the_anatomy_sections():
    text = SKILL.read_text().lower()
    for heading in ["when to use", "hard-gate", "process", "rationalization",
                    "red flags", "verification"]:
        assert heading in text, f"missing section: {heading}"


def test_skill_references_the_real_commands():
    text = SKILL.read_text()
    for cmd in ["specflo plan start", "specflo task add", "specflo validate plan",
                "specflo advance"]:
        assert cmd in text, f"missing command reference: {cmd}"


def test_skill_gives_thin_milestone_guidance():
    # REQ-17: the planner authors milestones with Exit checklists and assigns
    # every task to one — via the milestone commands, deferring mechanics to CLI.
    text = SKILL.read_text()
    for cmd in ["specflo milestone add", "specflo task set-milestone"]:
        assert cmd in text, f"missing milestone command reference: {cmd}"
    low = text.lower()
    assert "exit checklist" in low          # Exit checklists authored per milestone
    assert "--milestone" in text            # every task assigned to a milestone


def test_skill_has_fan_out_capable_decomposition_rules():
    # fan-out-plans REQ-16: every plan is decomposed so an orchestrator could
    # fan it out, regardless of the project's execution mode.
    text = SKILL.read_text()
    assert "Decomposition rules" in text
    rules = text.split("Decomposition rules", 1)[1].split("\n## ", 1)[0]
    low = rules.lower()
    assert "files" in low                                    # list every file edited
    assert "share a file" in low or "same file" in low       # edge between sharers
    assert "validate plan" in low                            # the warning names the pair
    assert "merge task" in low                               # parallel producers + one merge
    assert "contract" in low                                 # contract-first ordering
    assert "--needs" in rules and "pool add" in rules        # hardware / shared envs
    assert "--needs user" in rules                           # user-in-the-loop tasks
    assert "regardless of" in low                            # every plan, every mode

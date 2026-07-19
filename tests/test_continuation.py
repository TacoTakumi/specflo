"""The shared clear-point-and-continue builder (clear-and-continue project).

One builder renders the continuation text emitted at both CLI seams (`task done`,
`advance`) and inside the auto handoff payload, so the three cannot drift apart.

Everything here asserts *structurally* — fixed markers and substrings, never
verbatim whole-string equality — so the wording can grow in place without a test
rewrite (spec: "Structural assertions", REQ-01's acceptance).
"""

from __future__ import annotations

import re

from conftest import executable_identifiers

from specflo import continuation
from specflo.workflow import PHASES


SENTINEL = "SENTINEL NEXT ACTION"

# Trigger names and invocation strings belonging to outer harnesses. specflo owns
# the payload, never the trigger (auto-mode D-03/REQ-05): the emitted wording must
# name specflo commands only, so each harness can map the clear-point onto its own
# trigger.
HARNESS_TRIGGERS = ["clearthen", "clearanddo", "claude", "pi", "hermes", "codex"]


def _names_a_trigger(text: str) -> str | None:
    """The first harness trigger named in ``text``, or None.

    Word-boundary matched so a short trigger name cannot false-positive inside an
    ordinary word (``pi`` in "pipeline", ``codex`` in a longer identifier).
    """
    lowered = text.lower()
    for trigger in HARNESS_TRIGGERS:
        if re.search(rf"\b{re.escape(trigger)}\b", lowered):
            return trigger
    return None


# Ambient state the builder must never consult. Reading any of these would make
# the continuation vary with auto-run state, which REQ-03 forbids. Lower-case:
# `executable_identifiers` folds case, so an upper-case needle would never match.
AUTO_STATE_READS = [
    "auto-run",
    "auto_run_state_filename",
    "load_run_state",
    "run_state",
    "killed",
    "kill_switch",
    "autonomy",
]


def test_builder_carries_the_passed_next_step_hint_verbatim():
    # The hint is derived by the caller (workflow.next_step / build_checkpoint)
    # and passed in, so it provably cannot drift from what status and checkpoint
    # report for the same state (REQ-05 is enforced at the seams; here we only
    # guarantee the builder round-trips it untouched).
    for phase in PHASES:
        text = continuation.build_continuation(phase, SENTINEL)
        assert SENTINEL in text, f"next-step hint dropped at {phase}"


def test_builder_names_the_phase_skill_for_every_phase():
    # REQ-06: a resumed session must learn which specflo phase skill carries the
    # phase it landed in.
    for phase in PHASES:
        text = continuation.build_continuation(phase, SENTINEL)
        skill = continuation.PHASE_SKILLS[phase]
        assert skill in text, f"{phase} continuation does not name its skill"


def test_builder_names_both_resume_paths():
    # REQ-11 / D-02: name the manual reseed and, parenthetically, the auto
    # command — so an agent in an auto run sees the right command without the
    # CLI ever detecting mode.
    for phase in PHASES:
        text = continuation.build_continuation(phase, SENTINEL)
        assert "`specflo checkpoint`" in text, f"manual resume path missing at {phase}"
        assert "`specflo auto`" in text, f"auto resume path missing at {phase}"


def test_builder_emits_the_clear_point_marker():
    # The clear-point is the load-bearing signal an outer harness keys on; it is
    # a fixed marker so that keying stays stable as the sentence around it grows.
    for phase in PHASES:
        text = continuation.build_continuation(phase, SENTINEL)
        assert continuation.CLEAR_POINT_MARKER in text, f"no clear-point at {phase}"


def test_terminal_variant_emits_a_clear_point():
    # REQ-07: completing the project is still a clean place to clear context.
    text = continuation.build_continuation("execute", SENTINEL, complete=True)
    assert continuation.CLEAR_POINT_MARKER in text


def test_terminal_variant_names_neither_resume_command():
    # REQ-07, asserted by *required absence*: a complete project has nothing to
    # continue to, and naming the auto command here would invite an auto loop -
    # which halts on the CLI's completion signal - to start another pass.
    text = continuation.build_continuation("execute", SENTINEL, complete=True)
    assert "specflo checkpoint" not in text
    assert "specflo auto" not in text


def test_terminal_variant_omits_the_continue_instruction():
    # A clear-point *without* a continue-instruction: no phase skill to carry on
    # with, since there is no next phase. Keyed on the pointer sentence rather
    # than the skill name, which at the terminal phase is also the phase name.
    text = continuation.build_continuation("execute", SENTINEL, complete=True)
    assert "phase skill" not in text


def test_non_terminal_variant_is_unchanged_by_the_terminal_flag_default():
    # The flag defaults off, so every existing caller keeps the full continuation.
    for phase in PHASES:
        assert continuation.build_continuation(phase, SENTINEL) == (
            continuation.build_continuation(phase, SENTINEL, complete=False)
        )


def test_output_is_identical_with_and_without_auto_run_state(tmp_path, monkeypatch):
    # REQ-03: the CLI stays mode-agnostic. Writing the auto-run state file into
    # the working tree must not change a single byte of the emitted continuation
    # - the wording is one shape, and in an auto run the self-propagating auto
    # bootstrap supersedes it (D-01).
    from specflo import auto

    monkeypatch.chdir(tmp_path)
    before = {
        (phase, complete): continuation.build_continuation(phase, SENTINEL, complete=complete)
        for phase in PHASES
        for complete in (False, True)
    }

    state = tmp_path / auto.AUTO_RUN_STATE_FILENAME
    state.write_text('{"passes": 3, "killed": true}')
    assert state.is_file()

    after = {
        (phase, complete): continuation.build_continuation(phase, SENTINEL, complete=complete)
        for phase in PHASES
        for complete in (False, True)
    }
    assert before == after


def test_builder_source_reads_no_auto_run_state():
    # The stronger form of REQ-03, asserted by source scan rather than behaviour:
    # even a *latent* read of auto-run state, the kill-switch flag or an autonomy
    # config value is a defect, because it would make mode-awareness reachable
    # without the deliberate design change D-01 defers.
    code = executable_identifiers(continuation)
    for needle in AUTO_STATE_READS:
        assert needle not in code, f"builder consults auto-run state: {needle!r}"


def test_continuation_names_no_harness_trigger():
    # REQ-09: harness-neutral at every phase and in both variants. The outer
    # harness maps this clear-point onto its own trigger; specflo never names one.
    for phase in PHASES:
        for complete in (False, True):
            text = continuation.build_continuation(phase, SENTINEL, complete=complete)
            named = _names_a_trigger(text)
            assert named is None, (
                f"harness trigger {named!r} leaked into the {phase} continuation"
            )


def test_builder_emitted_strings_name_no_harness_trigger():
    # Absence across every string the module can actually emit, so a trigger name
    # cannot hide in an unreached branch.
    named = _names_a_trigger(executable_identifiers(continuation))
    assert named is None, f"harness trigger {named!r} reachable in builder output"


def test_source_scan_helper_handles_undecorated_functions():
    # The scan helper underpins every absence test here and in test_cli/test_auto.
    # It must work on a plain indented-body function, not only on a decorated one
    # whose source happens to start at column 0 - otherwise the absence tests
    # would error rather than fail, and could be silently narrowed.
    for func in (
        continuation.build_continuation,
        continuation.phase_skill,
        continuation._continue_line,
    ):
        scanned = executable_identifiers(func)
        assert scanned, f"no identifiers scanned for {func.__name__}"

    # ...and it still sees what it is supposed to see: the fragment helpers the
    # builder actually calls.
    scanned = executable_identifiers(continuation.build_continuation)
    assert "_skill_pointer_line" in scanned
    assert "_continue_line" in scanned


def test_builder_is_a_pure_function_of_its_arguments():
    # Same inputs -> byte-identical output, with no ambient state consulted. This
    # is what makes the mode-agnosticism of REQ-03 checkable by construction.
    for phase in PHASES:
        first = continuation.build_continuation(phase, SENTINEL)
        second = continuation.build_continuation(phase, SENTINEL)
        assert first == second

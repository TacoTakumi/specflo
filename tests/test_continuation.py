"""The shared clear-point-and-continue builder (clear-and-continue project).

One builder renders the continuation text emitted at both CLI seams (`task done`,
`advance`) and inside the auto handoff payload, so the three cannot drift apart.

Everything here asserts *structurally* — fixed markers and substrings, never
verbatim whole-string equality — so the wording can grow in place without a test
rewrite (spec: "Structural assertions", REQ-01's acceptance).
"""

from __future__ import annotations

from specflo import continuation
from specflo.workflow import PHASES


SENTINEL = "SENTINEL NEXT ACTION"


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


def test_builder_is_a_pure_function_of_its_arguments():
    # Same inputs -> byte-identical output, with no ambient state consulted. This
    # is what makes the mode-agnosticism of REQ-03 checkable by construction.
    for phase in PHASES:
        first = continuation.build_continuation(phase, SENTINEL)
        second = continuation.build_continuation(phase, SENTINEL)
        assert first == second

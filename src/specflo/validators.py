"""The shared phase->validator map.

The generation/advance path (``cli``) and the read path (``checkpoint`` /
``status``) both need to run a phase's *real* validator: ``cli`` to gate
``validate``/``advance``, the read path to derive honest doneness. Defining the
map here — a neutral module that imports only the phase modules — lets all three
share one definition without ``checkpoint``/``status`` importing ``cli`` (which
would form a cycle: ``cli`` imports both of them). REQ-12.

Each validator has the signature ``(root, cfg, slug) -> list[str]`` and returns
the phase's outstanding issues (empty when the artifact is ready).
"""

from __future__ import annotations

from . import brainstorm, plan, review, spec

# Phase -> its real validator. Defined once; ``cli``'s validate/advance and the
# read-path doneness derivation both reference this same object.
def execute_issues(root, cfg, slug) -> list[str]:
    """Everything blocking project completion from execute.

    The plan's own reconcile issues first (the plan must validate and every
    active task be done), and only once those are clear, the review gate
    (review-rounds REQ-21) - an unfinished plan is what to fix first, and the
    review issue should not crowd out the task list.

    This composition is why the review gate binds exactly one boundary: the
    earlier phases keep their own validators, so advancing brainstorm -> spec,
    spec -> plan and plan -> execute never asks about rounds (REQ-17).
    """
    issues = plan.reconcile_issues(root, cfg, slug)
    if issues:
        return issues
    return review.completion_issues(root, cfg, slug)


VALIDATORS = {
    "brainstorm": brainstorm.validate_brainstorm,
    "spec": spec.validate_spec,
    "plan": plan.validate_plan,
    "execute": execute_issues,
}

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

from . import brainstorm, brief, plan, projects, review, spec

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
    # A quick project is worked from its brief: the brief's own gate is the
    # whole of it, with no plan to reconcile and no review round.
    if projects.load_project(root, cfg, slug).level == projects.QUICK_LEVEL:
        return brief.validate_brief(root, cfg, slug)
    issues = plan.reconcile_issues(root, cfg, slug)
    if issues:
        return issues
    return review.completion_issues(root, cfg, slug)


VALIDATORS = {
    "brainstorm": brainstorm.validate_brainstorm,
    "spec": spec.validate_spec,
    "plan": plan.validate_plan,
    "execute": execute_issues,
    "brief": brief.validate_brief,
}


def outgrown(root, cfg, project, unattended: bool | None = None) -> str | None:
    """Why ``project`` is over its level's cap, or None while it fits.

    Derived from the documents each time, so it clears as soon as the work is
    cut back under the cap or the project moves up. Full level has no cap. A
    cap never pushes the project up a level: quick cuts down to one check, and
    fast, where no verb lowers the count of decisions or tasks, only warns.
    Only an attended project is reminded that `specflo level full` exists;
    ``unattended`` defaults to whether an auto run is live.
    """
    if project.level == projects.QUICK_LEVEL:
        path = brief.brief_path(root, cfg, project.slug)
        checks = brief.check_count(path.read_text()) if path.is_file() else 0
        if checks > 1:
            return (
                f"Over quick level's cap: {checks} checks in Done when; quick level"
                " allows 1. Keep one check and move the rest to the brief's Deferred"
                " section."
            )
    elif project.level == projects.FAST_LEVEL:
        base = projects.project_dir(root, cfg, project.slug)
        bs_path = base / brainstorm.BRAINSTORM_FILENAME
        decisions = (
            len(brainstorm.active_decision_ids(bs_path.read_text())) if bs_path.is_file() else 0
        )
        tasks = (
            len(plan.list_tasks(root, cfg, project.slug))
            if (base / plan.PLAN_FILENAME).is_file() else 0
        )
        over = []
        if decisions > projects.FAST_MAX_DECISIONS:
            over.append(f"{decisions} active decisions (cap {projects.FAST_MAX_DECISIONS})")
        if tasks > projects.FAST_MAX_TASKS:
            over.append(f"{tasks} active tasks (cap {projects.FAST_MAX_TASKS})")
        if over:
            if unattended is None:
                from . import auto

                unattended = auto.run_under_way(root, cfg, project)
            return (
                f"Over fast level's cap: {' and '.join(over)}. The cap only warns:"
                " finish the work already recorded, add no more, and put new work in"
                " the brainstorm's Out of scope / Deferred section."
                + ("" if unattended else " `specflo level full` is there if you want full level.")
            )
    return None

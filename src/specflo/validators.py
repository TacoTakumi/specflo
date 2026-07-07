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

from . import brainstorm, plan, spec

# Phase -> its real validator. Defined once; ``cli``'s validate/advance and the
# read-path doneness derivation both reference this same object.
VALIDATORS = {
    "brainstorm": brainstorm.validate_brainstorm,
    "spec": spec.validate_spec,
    "plan": plan.validate_plan,
    "execute": plan.reconcile_issues,
}

"""Dialog auto-answer policy (REQ-10).

The host answers pi extension_ui_request dialogs without human involvement:
confirm affirmed, select answered with the first policy-safe option,
input/editor answered with a policy nudge, and any dialog whose text matches
the danger pattern cancelled. The per-run flood threshold lives on the policy;
the host enforces it (needs-attention state, auto-answering stops).

Patterns are proven in ember/tools/rpc_driver.py; everything is configurable
per DialogPolicy instance.

Stdlib only - the agent subsystem imports nothing from pipeline code (REQ-15).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

DIALOG_METHODS = ("select", "confirm", "input", "editor")

DEFAULT_DANGER = r"push|email|send|post|publish|deploy|delete|credential|secret|spend|pay"
DEFAULT_SAFE_OPTION = r"continue|proceed|yes|allow|ok|default"
DEFAULT_NUDGE = (
    "Auto-policy reminder: decision authority is delegated. Take the "
    "best-judgment option and keep going; do not wait for a human."
)
DEFAULT_FLOOD_THRESHOLD = 25


@dataclass
class DialogPolicy:
    danger_pattern: str = DEFAULT_DANGER
    safe_option_pattern: str = DEFAULT_SAFE_OPTION
    nudge_text: str = DEFAULT_NUDGE
    flood_threshold: int = DEFAULT_FLOOD_THRESHOLD
    _danger: re.Pattern = field(init=False, repr=False)
    _safe: re.Pattern = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._danger = re.compile(self.danger_pattern, re.IGNORECASE)
        self._safe = re.compile(self.safe_option_pattern, re.IGNORECASE)

    def answer_for(self, request: dict[str, Any]) -> dict[str, Any] | None:
        """The extension_ui_response frame for one dialog, or None for
        fire-and-forget methods (notify, setStatus, ...)."""
        method = request.get("method")
        if method not in DIALOG_METHODS:
            return None
        request_id = request.get("id")
        text = f"{request.get('title', '')} {request.get('message', '')}"
        if self._danger.search(text):
            return {
                "type": "extension_ui_response",
                "id": request_id,
                "cancelled": True,
            }
        if method == "confirm":
            return {
                "type": "extension_ui_response",
                "id": request_id,
                "confirmed": True,
            }
        if method == "select":
            options = [str(o) for o in request.get("options", [])]
            candidates = [o for o in options if not self._danger.search(o)]
            pick = next(
                (o for o in candidates if self._safe.search(o)),
                candidates[0] if candidates else None,
            )
            if pick is None:
                return {
                    "type": "extension_ui_response",
                    "id": request_id,
                    "cancelled": True,
                }
            return {"type": "extension_ui_response", "id": request_id, "value": pick}
        # input / editor
        return {
            "type": "extension_ui_response",
            "id": request_id,
            "value": self.nudge_text,
        }

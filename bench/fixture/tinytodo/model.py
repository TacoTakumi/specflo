"""The task record and its validation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

PRIORITIES = ("low", "normal", "high")


@dataclass
class Task:
    """One to-do item. The title is stripped; an empty title is an error."""

    id: int
    title: str
    priority: str = "normal"
    done: bool = False

    def __post_init__(self) -> None:
        self.title = validate_title(self.title)
        self.priority = validate_priority(self.priority)

    def to_dict(self) -> dict[str, Any]:
        """Return the task as a JSON-ready dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Task:
        """Build a task from a dict read from the task file."""
        return cls(
            id=int(data["id"]),
            title=data["title"],
            priority=data.get("priority", "normal"),
            done=bool(data.get("done", False)),
        )


def validate_title(title: str) -> str:
    """Return the stripped title, or raise ValueError if nothing is left."""
    cleaned = title.strip()
    if not cleaned:
        raise ValueError("title must not be empty")
    return cleaned


def validate_priority(priority: str) -> str:
    """Return the priority if it is known, else raise ValueError."""
    if priority not in PRIORITIES:
        raise ValueError(f"unknown priority: {priority}")
    return priority

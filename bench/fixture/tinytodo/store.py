"""Load and save tasks in a JSON file."""

from __future__ import annotations

import json
from pathlib import Path

from tinytodo.model import Task

FORMAT_VERSION = 1


class TaskNotFound(LookupError):
    """No task has the requested id."""

    def __init__(self, task_id: int) -> None:
        super().__init__(f"no task with id {task_id}")
        self.task_id = task_id


class TaskStore:
    """The tasks in one JSON file. Changes stay in memory until save()."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.tasks: list[Task] = []
        self.next_id = 1
        self.load()

    def load(self) -> None:
        """Read the file. A missing file is an empty store."""
        if not self.path.exists():
            self.tasks = []
            self.next_id = 1
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.tasks = [Task.from_dict(item) for item in data.get("tasks", [])]
        highest = max((t.id for t in self.tasks), default=0)
        self.next_id = max(int(data.get("next_id", 1)), highest + 1)

    def save(self) -> None:
        """Write the store to its file, creating parent directories."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": FORMAT_VERSION,
            "next_id": self.next_id,
            "tasks": [t.to_dict() for t in self.tasks],
        }
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def add(self, title: str, priority: str = "normal") -> Task:
        """Add a pending task and return it. Ids are never reused."""
        task = Task(id=self.next_id, title=title, priority=priority)
        self.tasks.append(task)
        self.next_id += 1
        return task

    def get(self, task_id: int) -> Task:
        """Return the task with this id, or raise TaskNotFound."""
        for task in self.tasks:
            if task.id == task_id:
                return task
        raise TaskNotFound(task_id)

    def complete(self, task_id: int) -> Task:
        """Mark a task done and return it."""
        task = self.get(task_id)
        task.done = True
        return task

    def remove(self, task_id: int) -> Task:
        """Delete a task and return it."""
        task = self.get(task_id)
        self.tasks.remove(task)
        return task

    def list(self, include_done: bool = False) -> list[Task]:
        """Return tasks in id order; done tasks only when include_done."""
        tasks = sorted(self.tasks, key=lambda t: t.id)
        if include_done:
            return tasks
        return [t for t in tasks if not t.done]

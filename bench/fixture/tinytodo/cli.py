"""The tinytodo command line."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

from tinytodo.model import PRIORITIES, Task
from tinytodo.store import TaskNotFound, TaskStore

DEFAULT_FILE = "todo.json"


def format_task(task: Task) -> str:
    """Return the one-line form of a task, as `list` prints it."""
    mark = "x" if task.done else " "
    line = f"[{mark}] {task.id} {task.title}"
    if task.priority != "normal":
        line += f" ({task.priority})"
    return line


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser for all commands."""
    parser = argparse.ArgumentParser(prog="tinytodo", description="A small to-do list.")
    parser.add_argument(
        "--file",
        default=None,
        help=f"task file (default: $TINYTODO_FILE or {DEFAULT_FILE})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="add a task")
    add.add_argument("title")
    add.add_argument("--priority", choices=PRIORITIES, default="normal")

    lst = sub.add_parser("list", help="list tasks")
    lst.add_argument("--all", action="store_true", help="include done tasks")

    done = sub.add_parser("done", help="mark a task done")
    done.add_argument("id", type=int)

    remove = sub.add_parser("remove", help="delete a task")
    remove.add_argument("id", type=int)
    return parser


def task_file(arg: str | None) -> str:
    """Pick the task file: --file, then $TINYTODO_FILE, then the default."""
    return arg or os.environ.get("TINYTODO_FILE") or DEFAULT_FILE


def cmd_add(store: TaskStore, args: argparse.Namespace) -> int:
    task = store.add(args.title, priority=args.priority)
    store.save()
    print(f"Added task {task.id}: {task.title}")
    return 0


def cmd_list(store: TaskStore, args: argparse.Namespace) -> int:
    for task in store.list(include_done=args.all):
        print(format_task(task))
    return 0


def cmd_done(store: TaskStore, args: argparse.Namespace) -> int:
    task = store.complete(args.id)
    store.save()
    print(f"Completed task {task.id}: {task.title}")
    return 0


def cmd_remove(store: TaskStore, args: argparse.Namespace) -> int:
    task = store.remove(args.id)
    store.save()
    print(f"Removed task {task.id}: {task.title}")
    return 0


COMMANDS = {
    "add": cmd_add,
    "list": cmd_list,
    "done": cmd_done,
    "remove": cmd_remove,
}


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command and return the exit status."""
    args = build_parser().parse_args(argv)
    try:
        store = TaskStore(task_file(args.file))
        return COMMANDS[args.command](store, args)
    except (TaskNotFound, ValueError) as exc:
        message = exc.args[0] if exc.args else str(exc)
        print(f"error: {message}", file=sys.stderr)
        return 1

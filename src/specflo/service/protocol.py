"""The ProjectService protocol: every artifact operation the CLI performs.

A CLI command never opens a project file. It resolves a ``ProjectService``
and performs each read or write of a project artifact through one of the
operations named here, so the same command serves a project whose files
live in the checkout and one held by a daemon. The local implementation is
an in-process call on files; the remote one is an HTTP client.

Every operation names the project by slug. Which project is active is the
client's config, not the service's business. The service stamps dates
itself, so no operation takes a date from the caller.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, TypedDict, runtime_checkable

from ..brainstorm import Decision
from ..plan import Milestone, Task
from ..projects import FULL_LEVEL, LINEAR_EXECUTION, Project
from ..review import ClosedRound
from ..spec import Requirement


class ExecutionGraph(TypedDict):
    """The plan's active tasks and its milestones, as the graph view reads them."""

    tasks: list[Task]
    milestones: list[Milestone]


@runtime_checkable
class ProjectService(Protocol):
    """Every artifact operation, grouped as the CLI groups its commands."""

    # --- project lifecycle --------------------------------------------------

    def create_project(
        self,
        name: str,
        *,
        summary: str | None = None,
        execution: str = LINEAR_EXECUTION,
        work_item: int | None = None,
        piece: str | None = None,
        level: str = FULL_LEVEL,
    ) -> Project:
        """Create the project container; the caller moves the active pointer.

        ``work_item`` and ``piece`` record what a spawned project came from;
        ``level`` is how much ceremony it gets.
        """
        ...

    def load_project(self, slug: str) -> Project:
        """The project's front matter as a ``Project``; refuses a missing slug."""
        ...

    def list_projects(self) -> list[Project]:
        """Every project, sorted by slug."""
        ...

    def advance_project(self, slug: str) -> Project:
        """Move the phase pointer forward one phase."""
        ...

    def reopen_project(self, slug: str, target: str | None = None) -> Project:
        """Move the phase pointer back to ``target`` or the previous phase."""
        ...

    def complete_project(self, slug: str) -> Project:
        """Mark the project complete; idempotent."""
        ...

    def shelve_project(self, slug: str, *, reason: str | None = None) -> Project:
        """Set status to shelved, keeping the phase."""
        ...

    def resume_project(self, slug: str) -> Project:
        """Un-shelve: status back to active, the reason cleared."""
        ...

    def set_summary(self, slug: str, text: str) -> Project:
        """Set the one-line summary."""
        ...

    def set_execution(self, slug: str, mode: str) -> tuple[str, bool]:
        """Set the execution mode; ``(mode, changed)``."""
        ...

    def set_level(self, slug: str, level: str) -> tuple[Project, list[str]]:
        """Move the project up to ``level``; the project and, on a move from
        fast to full, the active decisions to review with the user."""
        ...

    def set_egress(self, slug: str, egress_class: str) -> tuple[str, bool]:
        """Pin the project's egress class; ``(egress_class, changed)``."""
        ...

    def open_gate(self, slug: str, role: str, *, note: str = "") -> Project:
        """Open a gate for ``role`` as the service's identity; refused while one is open."""
        ...

    def take_gate(self, slug: str, *, by: str | None = None) -> Project:
        """Close the open gate as the service's identity, or as ``by`` when it relays a human.

        Only the agent identity may name ``by``; refused with no gate open.
        """
        ...

    def has_artifact(self, slug: str, name: str) -> bool:
        """Whether the project has created the named artifact yet."""
        ...

    def validate_artifact(self, slug: str, artifact: str) -> list[str]:
        """The issues blocking the artifact or phase; empty means ready."""
        ...

    def complete_artifact(self, slug: str, artifact: str) -> None:
        """Mark a phase artifact complete when its phase is left."""
        ...

    def stamp_banners(self, slug: str) -> list[Path]:
        """Stamp the completion banner into the project's prose artifacts."""
        ...

    def index_exists(self) -> bool:
        """Whether the project ledger has been written."""
        ...

    def index_rule_line(self) -> str | None:
        """The prior-projects rule line, once a completed project exists."""
        ...

    def write_index(self) -> Path:
        """Regenerate the project ledger from every project."""
        ...

    def export_project(self, slug: str) -> dict[str, str]:
        """Every file of the project directory, by name."""
        ...

    def import_project(self, slug: str, files: dict[str, str]) -> dict[str, str]:
        """Create ``slug`` from ``files``; the SHA-256 of each file as written."""
        ...

    # --- brainstorm: decisions --------------------------------------------

    def start_brief(self, slug: str) -> tuple[Path, bool]:
        """Create or locate a quick project's brief; ``(path, created)``."""
        ...

    def start_brainstorm(self, slug: str) -> tuple[Path, bool]:
        """Create or locate the brainstorm; ``(path, created)``."""
        ...

    def add_decision(
        self,
        slug: str,
        text: str,
        *,
        rationale: str | None = None,
        supersedes: str | None = None,
    ) -> Decision:
        """Append a decision with the next id."""
        ...

    # --- spec: requirements -----------------------------------------------

    def start_spec(self, slug: str) -> tuple[Path, bool]:
        """Create or locate the spec; ``(path, created)``."""
        ...

    def add_requirement(
        self,
        slug: str,
        text: str,
        acceptance: str,
        *,
        derives_from: str | None = None,
        supersedes: str | None = None,
    ) -> Requirement:
        """Append a requirement with the next id."""
        ...

    # --- plan: tasks -------------------------------------------------------

    def start_plan(self, slug: str) -> tuple[Path, bool]:
        """Create or locate the plan; ``(path, created)``."""
        ...

    def plan_warnings(self, slug: str) -> list[str]:
        """Non-blocking warnings over the active tasks."""
        ...

    def resolution_notes(self, slug: str) -> list[str]:
        """Citations resolved through a supersession chain."""
        ...

    def execution_graph(self, slug: str) -> ExecutionGraph:
        """The active tasks and the milestones for the graph view."""
        ...

    def add_task(
        self,
        slug: str,
        text: str,
        acceptance: str,
        verify: str,
        implements: list[str],
        *,
        depends_on: list[str] | None = None,
        files: str | None = None,
        scope: str | None = None,
        supersedes: str | None = None,
        milestone: str | None = None,
        needs: list[str] | None = None,
    ) -> Task:
        """Append a task with the next id."""
        ...

    def active_dependents(self, slug: str, task_id: str) -> list[str]:
        """Ids of the active tasks that depend on ``task_id``."""
        ...

    def rewire_dependency(self, slug: str, from_id: str, to_id: str) -> list[str]:
        """Repoint every active dependent of ``from_id`` onto ``to_id``."""
        ...

    def set_milestone(self, slug: str, task_id: str, milestone_id: str) -> Task:
        """Assign or reassign a task's milestone."""
        ...

    def edit_task(
        self,
        slug: str,
        task_id: str,
        *,
        title: str | None = None,
        acceptance: str | None = None,
        verify: str | None = None,
        scope: str | None = None,
        files: str | None = None,
        needs: str | None = None,
        implements: str | None = None,
        add_depends_on: list[str] | None = None,
        drop_depends_on: list[str] | None = None,
        force: bool = False,
    ) -> tuple[str, list[str]]:
        """Rewrite a task's fields in place; ``(id, changed fields)``."""
        ...

    def add_note(
        self, slug: str, task_id: str, text: str, *, label: str | None = None
    ) -> dict:
        """Append a dated note to a task."""
        ...

    def start_task(self, slug: str, task_id: str) -> Task:
        """Mark a task in progress."""
        ...

    def done_task(self, slug: str, task_id: str, *, note: str | None = None) -> Task:
        """Mark a task done, recording ``note`` in the same write."""
        ...

    def block_task(self, slug: str, task_id: str, *, reason: str | None = None) -> Task:
        """Mark a task blocked."""
        ...

    def reopen_task(self, slug: str, task_id: str, *, note: str | None = None) -> Task:
        """Return a task to pending, clearing any block."""
        ...

    def list_tasks(self, slug: str, *, include_superseded: bool = False) -> list[Task]:
        """The tasks in plan order."""
        ...

    def plan_progress(self, slug: str) -> dict:
        """Done and total counts with the next actionable ids."""
        ...

    def frontier(self, slug: str) -> dict:
        """The dispatchable tasks and the pools with their holders."""
        ...

    def task_brief(self, slug: str, task_id: str | None = None) -> dict:
        """One task's brief; the next actionable task when no id is given."""
        ...

    def current_task_id(self, slug: str) -> str | None:
        """The task the project is on: the first in progress, else the next actionable."""
        ...

    # --- plan: milestones --------------------------------------------------

    def add_milestone(self, slug: str, text: str, exit_items: list[str]) -> Milestone:
        """Append a milestone with the next id and its exit checklist."""
        ...

    def milestone_progress(self, slug: str) -> dict:
        """Every milestone with its rollup and the current one."""
        ...

    def milestone_detail(self, slug: str, milestone_id: str) -> dict | None:
        """One milestone's checklist, members, rollup and requirements."""
        ...

    # --- plan: pools -------------------------------------------------------

    def add_pool(self, slug: str, name: str, size: int) -> tuple[str, int]:
        """Declare or resize a pool; ``(name, size)``."""
        ...

    def list_pools(self, slug: str) -> dict[str, int]:
        """Every declared pool with its size."""
        ...

    # --- reviews -----------------------------------------------------------

    def start_round(self, slug: str) -> tuple[Path, bool]:
        """Mint the next review round or hand back the open one."""
        ...

    def close_round(
        self,
        slug: str,
        verdict: str | None = None,
        *,
        reason: str | None = None,
        report_text: str | None = None,
    ) -> ClosedRound:
        """Close the open round with the verdict its findings give; ``report_text``
        becomes its body. An explicit verdict must be the derived one, or waived.

        The text, never a path: the caller reads its own report file, so the
        service opens nothing the caller did not send.
        """
        ...

    def add_finding(self, slug: str, severity: str, text: str) -> tuple[str, Path]:
        """Record one finding in the open round; ``(F-NN, round path)``."""
        ...

    # --- checkpoint and status ---------------------------------------------

    def build_checkpoint(self, slug: str) -> dict:
        """The resume-prompt payload derived from current state."""
        ...

    def write_checkpoint(self, slug: str) -> Path:
        """Render the checkpoint and write it into the project."""
        ...

    def build_status(self, slug: str) -> dict:
        """The status payload derived from current state."""
        ...

    # --- documents ---------------------------------------------------------

    def show_document(self, slug: str, name: str) -> str:
        """The verbatim text of one artifact."""
        ...

    def set_section(self, slug: str, name: str, section: str, body: str) -> str:
        """Replace one prose section's body; returns the section title."""
        ...

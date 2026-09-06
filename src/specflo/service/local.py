"""The local ProjectService: every operation is an in-process call on files.

Holds a checkout root and its config and hands each operation to the module
function that already owns it, so a local project behaves exactly as those
functions do. A daemon runs this same class on its own root, so a hosted
project is served by the same code on the far side of the wire.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .. import brainstorm, checkpoint, doc, index, plan, projects, review, spec, status
from ..brainstorm import Decision
from ..config import SpecfloConfig
from ..errors import SpecfloError
from ..plan import Milestone, Task
from ..projects import LINEAR_EXECUTION, Project
from ..spec import Requirement
from ..validators import VALIDATORS
from .protocol import ExecutionGraph

# Phase artifact -> the function that marks it complete when its phase is
# left. Execute has no artifact of its own, so it has no completer.
_COMPLETERS = {
    "brainstorm": brainstorm.complete_brainstorm,
    "spec": spec.complete_spec,
    "plan": plan.complete_plan,
}


class LocalProjectService:
    """A ``ProjectService`` over the projects under ``root``.

    ``actor`` is the identity every add is stamped with. A checkout runs the
    service without one, so local entries carry no Actor line; the daemon
    runs it as the identity behind each request's token.
    """

    def __init__(self, root: Path, cfg: SpecfloConfig, *, actor: str | None = None) -> None:
        self.root = root
        self.cfg = cfg
        self.actor = actor

    # --- project lifecycle --------------------------------------------------

    def create_project(
        self,
        name: str,
        *,
        summary: str | None = None,
        execution: str = LINEAR_EXECUTION,
        work_item: int | None = None,
        piece: str | None = None,
    ) -> Project:
        return projects.create_project(
            self.root, self.cfg, name, summary=summary, execution=execution,
            work_item=work_item, piece=piece,
        )

    def load_project(self, slug: str) -> Project:
        return projects.load_project(self.root, self.cfg, slug)

    def list_projects(self) -> list[Project]:
        return projects.list_projects(self.root, self.cfg)

    def advance_project(self, slug: str) -> Project:
        return projects.advance_project(self.root, self.cfg, slug)

    def reopen_project(self, slug: str, target: str | None = None) -> Project:
        return projects.reopen_project(self.root, self.cfg, slug, target)

    def complete_project(self, slug: str) -> Project:
        return projects.complete_project(self.root, self.cfg, slug)

    def shelve_project(self, slug: str, *, reason: str | None = None) -> Project:
        return projects.shelve_project(self.root, self.cfg, slug, reason=reason)

    def resume_project(self, slug: str) -> Project:
        return projects.resume_project(self.root, self.cfg, slug)

    def set_summary(self, slug: str, text: str) -> Project:
        return projects.set_summary(self.root, self.cfg, slug, text)

    def set_execution(self, slug: str, mode: str) -> tuple[str, bool]:
        return projects.set_execution(self.root, self.cfg, slug, mode)

    def has_artifact(self, slug: str, name: str) -> bool:
        return doc.artifact_path(self.root, self.cfg, slug, name).is_file()

    def validate_artifact(self, slug: str, artifact: str) -> list[str]:
        validator = VALIDATORS.get(artifact)
        if validator is None:
            known = ", ".join(sorted(VALIDATORS))
            raise SpecfloError(f"Unknown artifact {artifact!r}. Known: {known}.")
        return validator(self.root, self.cfg, slug)

    def complete_artifact(self, slug: str, artifact: str) -> None:
        completer = _COMPLETERS.get(artifact)
        if completer is None:
            known = ", ".join(_COMPLETERS)
            raise SpecfloError(
                f"Artifact {artifact!r} has no completer: expected one of {known}."
            )
        completer(self.root, self.cfg, slug)

    def stamp_banners(self, slug: str) -> list[Path]:
        project = projects.load_project(self.root, self.cfg, slug)
        return index.stamp_banners(self.root, self.cfg, project)

    def index_exists(self) -> bool:
        return index.index_path(self.root, self.cfg).is_file()

    def index_rule_line(self) -> str | None:
        return index.rule_line(self.root, self.cfg)

    def write_index(self) -> Path:
        return index.write_index(self.root, self.cfg)

    def export_project(self, slug: str) -> dict[str, str]:
        directory = projects.load_project(self.root, self.cfg, slug).path
        return {
            path.name: path.read_text()
            for path in sorted(directory.iterdir())
            if path.is_file()
        }

    def import_project(self, slug: str, files: dict[str, str]) -> dict[str, str]:
        directory = projects.project_dir(self.root, self.cfg, slug)
        if directory.exists():
            raise SpecfloError(f"Project {slug!r} already exists at {directory}.")
        for name in files:
            if not name or name.startswith(".") or "/" in name or "\\" in name:
                raise SpecfloError(f"Invalid file name {name!r}: expected a plain file name.")
        if projects.PROJECT_FILENAME not in files:
            raise SpecfloError(
                f"A project needs its {projects.PROJECT_FILENAME}; none was given."
            )
        directory.mkdir(parents=True)
        hashes = {}
        for name, text in files.items():
            (directory / name).write_text(text)
            hashes[name] = hashlib.sha256(text.encode()).hexdigest()
        return hashes

    # --- brainstorm: decisions --------------------------------------------

    def start_brainstorm(self, slug: str) -> tuple[Path, bool]:
        return brainstorm.start_brainstorm(self.root, self.cfg, slug)

    def add_decision(
        self,
        slug: str,
        text: str,
        *,
        rationale: str | None = None,
        supersedes: str | None = None,
    ) -> Decision:
        return brainstorm.add_decision(
            self.root, self.cfg, slug, text, rationale=rationale, supersedes=supersedes,
            actor=self.actor,
        )

    # --- spec: requirements -----------------------------------------------

    def start_spec(self, slug: str) -> tuple[Path, bool]:
        return spec.start_spec(self.root, self.cfg, slug)

    def add_requirement(
        self,
        slug: str,
        text: str,
        acceptance: str,
        *,
        derives_from: str | None = None,
        supersedes: str | None = None,
    ) -> Requirement:
        return spec.add_requirement(
            self.root, self.cfg, slug, text, acceptance,
            derives_from=derives_from, supersedes=supersedes, actor=self.actor,
        )

    # --- plan: tasks -------------------------------------------------------

    def start_plan(self, slug: str) -> tuple[Path, bool]:
        return plan.start_plan(self.root, self.cfg, slug)

    def plan_warnings(self, slug: str) -> list[str]:
        return plan.plan_warnings(self.root, self.cfg, slug)

    def resolution_notes(self, slug: str) -> list[str]:
        return plan.resolution_notes(self.root, self.cfg, slug)

    def execution_graph(self, slug: str) -> ExecutionGraph:
        return plan.execution_graph(self.root, self.cfg, slug)

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
        return plan.add_task(
            self.root, self.cfg, slug, text, acceptance, verify,
            implements=implements, depends_on=depends_on, files=files,
            scope=scope, supersedes=supersedes, milestone=milestone, needs=needs,
            actor=self.actor,
        )

    def active_dependents(self, slug: str, task_id: str) -> list[str]:
        return plan.active_dependents(self.root, self.cfg, slug, task_id)

    def rewire_dependency(self, slug: str, from_id: str, to_id: str) -> list[str]:
        return plan.rewire_dependency(self.root, self.cfg, slug, from_id, to_id)

    def set_milestone(self, slug: str, task_id: str, milestone_id: str) -> Task:
        return plan.set_milestone(self.root, self.cfg, slug, task_id, milestone_id)

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
        return plan.edit_task(
            self.root, self.cfg, slug, task_id, title=title, acceptance=acceptance,
            verify=verify, scope=scope, files=files, needs=needs,
            implements=implements, add_depends_on=add_depends_on,
            drop_depends_on=drop_depends_on, force=force,
        )

    def add_note(
        self, slug: str, task_id: str, text: str, *, label: str | None = None
    ) -> dict:
        return plan.add_note(self.root, self.cfg, slug, task_id, text, label=label)

    def start_task(self, slug: str, task_id: str) -> Task:
        return plan.start_task(self.root, self.cfg, slug, task_id)

    def done_task(self, slug: str, task_id: str, *, note: str | None = None) -> Task:
        return plan.done_task(self.root, self.cfg, slug, task_id, note=note)

    def block_task(self, slug: str, task_id: str, *, reason: str | None = None) -> Task:
        return plan.block_task(self.root, self.cfg, slug, task_id, reason=reason)

    def reopen_task(self, slug: str, task_id: str, *, note: str | None = None) -> Task:
        return plan.reopen_task(self.root, self.cfg, slug, task_id, note=note)

    def list_tasks(self, slug: str, *, include_superseded: bool = False) -> list[Task]:
        return plan.list_tasks(
            self.root, self.cfg, slug, include_superseded=include_superseded
        )

    def plan_progress(self, slug: str) -> dict:
        return plan.plan_progress(self.root, self.cfg, slug)

    def frontier(self, slug: str) -> dict:
        return plan.frontier(self.root, self.cfg, slug)

    def task_brief(self, slug: str, task_id: str | None = None) -> dict:
        return plan.task_brief(self.root, self.cfg, slug, task_id)

    def current_task_id(self, slug: str) -> str | None:
        return plan.current_task_id(self.root, self.cfg, slug)

    # --- plan: milestones --------------------------------------------------

    def add_milestone(self, slug: str, text: str, exit_items: list[str]) -> Milestone:
        return plan.add_milestone(
            self.root, self.cfg, slug, text, exit_items=exit_items, actor=self.actor
        )

    def milestone_progress(self, slug: str) -> dict:
        return plan.milestone_progress(self.root, self.cfg, slug)

    def milestone_detail(self, slug: str, milestone_id: str) -> dict | None:
        return plan.milestone_detail(self.root, self.cfg, slug, milestone_id)

    # --- plan: pools -------------------------------------------------------

    def add_pool(self, slug: str, name: str, size: int) -> tuple[str, int]:
        return plan.add_pool(self.root, self.cfg, slug, name, size)

    def list_pools(self, slug: str) -> dict[str, int]:
        return plan.list_pools(self.root, self.cfg, slug)

    # --- reviews -----------------------------------------------------------

    def start_round(self, slug: str) -> tuple[Path, bool]:
        return review.start_round(self.root, self.cfg, slug)

    def close_round(
        self,
        slug: str,
        verdict: str,
        *,
        reason: str | None = None,
        report: str | None = None,
    ) -> Path:
        return review.close_round(
            self.root, self.cfg, slug, verdict, reason=reason, report=report
        )

    # --- checkpoint and status ---------------------------------------------

    def build_checkpoint(self, slug: str) -> dict:
        project = projects.load_project(self.root, self.cfg, slug)
        return checkpoint.build_checkpoint(self.root, project, cfg=self.cfg)

    def write_checkpoint(self, slug: str) -> Path:
        project = projects.load_project(self.root, self.cfg, slug)
        return checkpoint.write_checkpoint(self.root, project, cfg=self.cfg)

    def build_status(self, slug: str) -> dict:
        project = projects.load_project(self.root, self.cfg, slug)
        return status.build_status(self.root, self.cfg, project)

    # --- documents ---------------------------------------------------------

    def show_document(self, slug: str, name: str) -> str:
        return doc.show_document(self.root, self.cfg, slug, name)

    def set_section(self, slug: str, name: str, section: str, body: str) -> str:
        return doc.set_section(self.root, self.cfg, slug, name, section, body)

"""The local ProjectService: every operation is an in-process call on files.

Holds a checkout root and its config and hands each operation to the module
function that already owns it, so a local project behaves exactly as those
functions do. A daemon runs this same class on its own root, so a hosted
project is served by the same code on the far side of the wire.
"""

from __future__ import annotations

import hashlib
import shutil
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

    ``hosted`` says the projects are served to clients elsewhere, as a
    daemon's are: a derived artifact then names files by locator, since the
    paths under ``root`` name nothing where it will be read.
    """

    def __init__(
        self,
        root: Path,
        cfg: SpecfloConfig,
        *,
        actor: str | None = None,
        hosted: bool = False,
    ) -> None:
        self.root = root
        self.cfg = cfg
        self.actor = actor
        self.hosted = hosted

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
        """Every file of the project directory as text, or a refusal.

        Only plain UTF-8 files travel. A subdirectory, a symlink, or a file
        that is not UTF-8 text is refused by name before anything is sent,
        because a promotion removes the whole directory once the daemon has
        confirmed what it received, and nothing may be lost on the way.
        """
        directory = projects.load_project(self.root, self.cfg, slug).path
        files = {}
        for path in sorted(directory.iterdir()):
            if path.is_symlink() or not path.is_file():
                raise SpecfloError(
                    f"Project {slug!r} holds {path.name!r}, which is not a plain file;"
                    " move it out of the project directory first, since only plain"
                    " files travel."
                )
            try:
                # The bytes as they are, not newline-translated text: what is
                # hashed and compared is what the file holds.
                files[path.name] = path.read_bytes().decode("utf-8")
            except UnicodeDecodeError:
                raise SpecfloError(
                    f"Project {slug!r} holds {path.name!r}, which is not UTF-8 text;"
                    " move it out of the project directory first, since only text"
                    " files travel."
                ) from None
        return files

    def import_project(self, slug: str, files: dict[str, str]) -> dict[str, str]:
        """Create ``slug`` from ``files``; the SHA-256 of each file as it is on disk.

        The files are written into a staging directory beside the project's
        and moved into place in one rename at the end, so a failure part way
        leaves no project behind and a retry is not refused as existing.
        """
        directory = projects.project_dir(self.root, self.cfg, projects.validate_slug(slug))
        if directory.exists():
            raise SpecfloError(f"Project {slug!r} already exists.")
        for name in files:
            if (
                not name or name.startswith(".") or "/" in name or "\\" in name
                or any(ord(char) < 32 or ord(char) == 127 for char in name)
            ):
                raise SpecfloError(f"Invalid file name {name!r}: expected a plain file name.")
        if projects.PROJECT_FILENAME not in files:
            raise SpecfloError(
                f"A project needs its {projects.PROJECT_FILENAME}; none was given."
            )
        # The project file is read before anything lands on disk: a file that
        # is not a project, or one naming another slug, would otherwise sit
        # under the root as a directory every listing has to step around.
        named = projects._parse_frontmatter(files[projects.PROJECT_FILENAME])["slug"]
        if named != slug:
            raise SpecfloError(
                f"Cannot import {slug!r}: its {projects.PROJECT_FILENAME} names {named!r}."
            )
        staging = directory.with_name(f".{directory.name}.importing")
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        try:
            hashes = {}
            for name, text in files.items():
                target = staging / name
                target.write_bytes(text.encode("utf-8"))
                hashes[name] = hashlib.sha256(target.read_bytes()).hexdigest()
            staging.rename(directory)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
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
        report_text: str | None = None,
    ) -> Path:
        return review.close_round(
            self.root, self.cfg, slug, verdict, reason=reason, report_text=report_text
        )

    # --- checkpoint and status ---------------------------------------------

    def build_checkpoint(self, slug: str) -> dict:
        project = projects.load_project(self.root, self.cfg, slug)
        return checkpoint.build_checkpoint(
            self.root, project, cfg=self.cfg, locators=self.hosted
        )

    def write_checkpoint(self, slug: str) -> Path:
        project = projects.load_project(self.root, self.cfg, slug)
        return checkpoint.write_checkpoint(
            self.root, project, cfg=self.cfg, locators=self.hosted
        )

    def build_status(self, slug: str) -> dict:
        project = projects.load_project(self.root, self.cfg, slug)
        return status.build_status(self.root, self.cfg, project)

    # --- documents ---------------------------------------------------------

    def show_document(self, slug: str, name: str) -> str:
        return doc.show_document(self.root, self.cfg, slug, name)

    def set_section(self, slug: str, name: str, section: str, body: str) -> str:
        return doc.set_section(self.root, self.cfg, slug, name, section, body)

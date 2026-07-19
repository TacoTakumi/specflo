"""The specflo command-line interface."""

from __future__ import annotations

import json
from pathlib import Path

import click
import typer
import typer.main
from typer.core import TyperGroup

from agentsquire import check_stale
from agentsquire.cli import skills_command_group
from agentsquire.sources import default_source

from . import __version__
from . import auto as auto_module
from . import brainstorm, checkpoint, config, continuation, guide as guide_module, hook, plan, projects, spec
from . import status as status_view
from . import workflow
from .errors import SpecfloError
from .validators import VALIDATORS


class DefaultHelpGroup(TyperGroup):
    """Two help tweaks:

    - On an unknown command, print the error and the full help below it,
      instead of the default bare "Try '... --help' for help." hint.
    - In the commands list, show each command's positional arguments next to
      its name (e.g. ``new <name>``).
    """

    def resolve_command(self, ctx: typer.Context, args: list[str]):
        name = args[0] if args else None
        if name is not None and not name.startswith("-") and self.get_command(ctx, name) is None:
            typer.echo(f"Error: No such command {name!r}.\n", err=True)
            typer.echo(ctx.get_help(), err=True)
            raise typer.Exit(code=2)
        return super().resolve_command(ctx, args)

    @staticmethod
    def _args_metavar(ctx: typer.Context, command) -> str:
        """The space-joined metavars of a command's positional arguments."""
        parts = []
        for param in command.get_params(ctx):
            if getattr(param, "param_type_name", None) != "argument":
                continue
            try:
                parts.append(param.make_metavar(ctx=ctx))
            except TypeError:  # older signature
                parts.append(param.make_metavar())
        return " ".join(p for p in parts if p)

    def format_help(self, ctx: typer.Context, formatter) -> None:
        # Temporarily suffix each command's display name with its arguments so
        # the commands list reads e.g. "new <name>". Restored afterwards so
        # command resolution and per-command usage are unaffected.
        restore = {}
        for name in self.list_commands(ctx):
            command = self.get_command(ctx, name)
            metavar = self._args_metavar(ctx, command) if command else ""
            if metavar:
                restore[command] = command.name
                command.name = f"{command.name} {metavar}"
        try:
            super().format_help(ctx, formatter)
        finally:
            for command, original in restore.items():
                command.name = original


app = typer.Typer(
    cls=DefaultHelpGroup,
    no_args_is_help=True,
    add_completion=False,
    help="A spec-driven software-engineering workflow.",
    epilog=(
        "Examples:  specflo init  |  "
        "specflo new 'My Project'  |  specflo status --json"
    ),
)

def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"specflo {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        None,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the specflo version and exit.",
    ),
) -> None:
    """A spec-driven software-engineering workflow."""


brainstorm_app = typer.Typer(help="Work with the brainstorm artifact.")
app.add_typer(brainstorm_app, name="brainstorm")

decision_app = typer.Typer(help="Capture brainstorm decisions.")
app.add_typer(decision_app, name="decision")

spec_app = typer.Typer(help="Work with the spec artifact.")
app.add_typer(spec_app, name="spec")

requirement_app = typer.Typer(help="Capture spec requirements.")
app.add_typer(requirement_app, name="requirement")

plan_app = typer.Typer(help="Work with the plan artifact.")
app.add_typer(plan_app, name="plan")

task_app = typer.Typer(help="Capture plan tasks and track their progress.")
app.add_typer(task_app, name="task")

milestone_app = typer.Typer(help="Group plan tasks into ordered milestones.")
app.add_typer(milestone_app, name="milestone")

hook_app = typer.Typer(help="Session-start integration (clear-and-continue).")
app.add_typer(hook_app, name="hook")


def _die(message: str) -> typer.Exit:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    return typer.Exit(code=1)


def _require_root() -> Path:
    root = config.find_root(Path.cwd())
    if root is None:
        raise _die("Not a specflo project. Run `specflo init` first.")
    return root


def _require_active(cfg: config.SpecfloConfig) -> str:
    if cfg.active_project is None:
        raise _die("No active project. Create one with `specflo new <name>`.")
    return cfg.active_project


def _refresh_checkpoint(root: Path, cfg: config.SpecfloConfig, slug: str) -> None:
    """Best-effort: rewrite the active project's checkpoint.md after a mutation.

    The checkpoint is fully derived, so this is cheap and always current. It runs
    after the triggering mutation has already succeeded and been persisted, so a
    failure here must never fail that command — swallow *any* refresh error
    (a failed load, or a write that hits a read-only FS, permissions, full disk,
    or a clobbered path) and move on.
    """
    try:
        project = projects.load_project(root, cfg, slug)
        checkpoint.write_checkpoint(root, project, cfg=cfg)
    except Exception:
        pass


# Phase/artifact registries. The phase->validator map is shared (`validators`)
# so `validate`, `advance`, and the read-path doneness derivation all agree;
# GATES pairs each shared validator with its completer for `advance`.
WARNERS = {"plan": plan.plan_warnings}
GATES = {
    "brainstorm": (VALIDATORS["brainstorm"], brainstorm.complete_brainstorm),
    "spec": (VALIDATORS["spec"], spec.complete_spec),
    "plan": (VALIDATORS["plan"], plan.complete_plan),
}


@app.command(epilog="Example: specflo init --projects-dir docs/projects")
def init(
    projects_dir: str = typer.Option(
        "docs/projects", "--projects-dir", help="Where projects are stored."
    ),
    force: bool = typer.Option(False, "--force", help="Re-initialize if already set up."),
) -> None:
    """Scaffold .specflo/config.yaml and the projects directory."""
    root = Path.cwd()
    try:
        cfg = config.init_config(root, projects_dir=projects_dir, force=force)
    except SpecfloError as exc:
        raise _die(str(exc))
    typer.echo(f"Initialized specflo in {root} (projects dir: {cfg.projects_dir}).")


@app.command(epilog="Example: specflo new 'My Project'")
def new(
    name: str = typer.Argument(
        ...,
        metavar="<name>",
        help="Project name; slugified into the project directory name.",
    ),
) -> None:
    """Create project <name> and make it active."""
    root = _require_root()
    cfg = config.load_config(root)
    try:
        project = projects.create_project(root, cfg, name)
    except SpecfloError as exc:
        raise _die(str(exc))
    cfg.active_project = project.slug
    config.save_config(root, cfg)
    # Scaffold the first artifact so a new project is immediately ready to work
    # (no separate `brainstorm start`). create_project stays container-only;
    # the scaffold is CLI orchestration over the idempotent helper.
    brainstorm_path, _ = brainstorm.start_brainstorm(root, cfg, project.slug)
    _refresh_checkpoint(root, cfg, project.slug)
    typer.echo(
        f"Created project '{project.slug}' (now active). Phase: {project.phase}."
    )
    typer.echo(f"Scaffolded {brainstorm_path} (ready to work).")


@app.command(name="list", epilog="Example: specflo list --json")
def list_(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """List all projects, marking the active one."""
    root = _require_root()
    cfg = config.load_config(root)
    items = projects.list_projects(root, cfg)

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "active_project": cfg.active_project,
                    "projects": [
                        {
                            "slug": p.slug,
                            "name": p.name,
                            "phase": p.phase,
                            "status": p.status,
                            "active": p.slug == cfg.active_project,
                        }
                        for p in items
                    ],
                }
            )
        )
        return

    if not items:
        typer.echo("No projects yet. Create one with `specflo new <name>`.")
        return

    for p in items:
        marker = "*" if p.slug == cfg.active_project else " "
        if p.status == projects.COMPLETE_STATUS:
            suffix = "  [complete]"
        elif p.status == projects.SHELVED_STATUS:
            suffix = "  [shelved]"
            if p.shelved_reason:
                suffix += f": {p.shelved_reason}"
        else:
            suffix = ""
        typer.echo(f"{marker} {p.slug}  ({p.phase}){suffix}")


@app.command(epilog="Example: specflo switch my-project")
def switch(
    name: str = typer.Argument(
        ...,
        metavar="<name>",
        help="Project to make active (its slug or name).",
    ),
) -> None:
    """Make project <name> the active project."""
    root = _require_root()
    cfg = config.load_config(root)
    try:
        project = projects.switch_project(root, cfg, name)
    except SpecfloError as exc:
        raise _die(str(exc))
    typer.echo(f"Switched to '{project.slug}' (phase: {project.phase}).")


@app.command(epilog='Example: specflo shelve --reason "not worth it"')
def shelve(
    name: str = typer.Argument(
        None,
        metavar="[<name>]",
        help="Project to shelve (its slug or name); defaults to the active one.",
    ),
    reason: str = typer.Option(
        None, "--reason", help="Why it's being shelved (optional, stored on the project)."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Shelve a project: set status to 'shelved', leaving its phase untouched.

    With no name, shelves the active project; the active_project pointer is left
    where it is (mirroring `complete`). Re-shelving updates the reason.
    """
    root = _require_root()
    cfg = config.load_config(root)
    slug = projects.slugify(name) if name else _require_active(cfg)
    try:
        existing = projects.load_project(root, cfg, slug)
    except SpecfloError as exc:
        raise _die(str(exc))
    if existing.status == projects.COMPLETE_STATUS:
        raise _die(f"Project '{slug}' is complete (terminal) - cannot shelve it.")
    try:
        project = projects.shelve_project(root, cfg, slug, reason=reason)
    except SpecfloError as exc:
        raise _die(str(exc))
    _refresh_checkpoint(root, cfg, slug)
    if json_output:
        typer.echo(json.dumps(
            {"slug": project.slug, "status": project.status,
             "reason": project.shelved_reason}))
    else:
        message = f"Shelved '{project.slug}' (phase: {project.phase})."
        if project.shelved_reason:
            message += f" Reason: {project.shelved_reason}."
        typer.echo(message)


@app.command(epilog="Example: specflo resume my-project")
def resume(
    name: str = typer.Argument(
        None,
        metavar="[<name>]",
        help="Project to resume (its slug or name); defaults to the active one.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Resume a shelved project: status -> active, clear its reason, make it active.

    With no name, resumes the active project (when it is shelved). The phase is
    left untouched, so resume returns you to where the work was paused.
    """
    root = _require_root()
    cfg = config.load_config(root)
    slug = projects.slugify(name) if name else _require_active(cfg)
    try:
        existing = projects.load_project(root, cfg, slug)
    except SpecfloError as exc:
        raise _die(str(exc))
    if existing.status != projects.SHELVED_STATUS:
        raise _die(f"Project '{slug}' is not shelved - nothing to resume.")
    try:
        project = projects.resume_project(root, cfg, slug)
    except SpecfloError as exc:
        raise _die(str(exc))
    cfg.active_project = project.slug
    config.save_config(root, cfg)
    _refresh_checkpoint(root, cfg, slug)
    if json_output:
        typer.echo(json.dumps({"slug": project.slug, "status": project.status}))
    else:
        typer.echo(f"Resumed '{project.slug}' (phase: {project.phase}). Now active.")


@app.command(epilog="Example: specflo status --json")
def status(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Show the active project, its phase, and what's next."""
    root = config.find_root(Path.cwd())
    if root is None:
        if json_output:
            typer.echo(json.dumps({"initialized": False}))
        else:
            typer.echo("Not a specflo project. Run `specflo init` to get started.")
        return

    cfg = config.load_config(root)
    if cfg.active_project is None:
        if json_output:
            typer.echo(json.dumps({"initialized": True, "active_project": None}))
        else:
            typer.echo("No active project. Create one with `specflo new <name>`.")
        return

    try:
        project = projects.load_project(root, cfg, cfg.active_project)
    except SpecfloError as exc:
        if json_output:
            typer.echo(
                json.dumps(
                    {
                        "initialized": True,
                        "active_project": cfg.active_project,
                        "error": str(exc),
                    }
                )
            )
            return
        raise _die(str(exc))

    info = status_view.build_status(root, cfg, project)
    if json_output:
        typer.echo(json.dumps(info))
    else:
        typer.echo(status_view.render_status(root, info))


def _render_pipeline(data: dict) -> str:
    current = data.get("phase")
    parts = [f"*{p}*" if p == current else p for p in data["pipeline"]]
    return " -> ".join(parts)


def _render_you_are_here(data: dict) -> list[str]:
    action = data["next_action"]
    if action == "init":
        return [
            "specflo isn't set up in this repo yet.",
            "  Run `specflo init`, then `specflo new <name>` to start a project.",
        ]
    if action == "new":
        return [
            "No active project.",
            "  Run `specflo new <name>` to start one.",
        ]
    return [
        f"Project '{data['active_project']}' | phase: {data['phase']}",
        f"  Next: {data['next_step']}",
    ]


def _render_commands(data: dict) -> list[str]:
    groups = [("setup", "Setup & navigation"), ("workflow", "Workflow")]
    width = max(len(f"{c['name']} {c['args']}".strip()) for c in data["commands"])
    lines: list[str] = []
    for key, title in groups:
        lines.append(f"  {title}")
        for c in data["commands"]:
            if c["group"] != key:
                continue
            label = f"{c['name']} {c['args']}".strip()
            lines.append(f"    {label.ljust(width)}  {c['summary']}")
    return lines


@app.command(name="guide", epilog="Example: specflo guide --json")
def guide_(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Show what specflo is, the workflow, and what to do next here.

    Runs cold - works before `specflo init` - so an agent can get oriented in any
    repo with a single command.
    """
    root = config.find_root(Path.cwd())
    cfg = config.load_config(root) if root is not None else None
    data = guide_module.build_guide(root, cfg)

    if json_output:
        typer.echo(json.dumps(data))
        return

    rule = "-" * 72
    lines = [
        "specflo - a spec-driven software-engineering workflow.",
        "",
        "Add this to your agent memory file (CLAUDE.md / AGENTS.md) once, so a "
        "fresh\nagent always knows specflo is here (static - no version to keep in sync):",
        rule,
        data["memory_snippet"],
        rule,
        "",
        f"Pipeline:  {_render_pipeline(data)}",
        "",
        "You are here:",
        *(f"  {line}" for line in _render_you_are_here(data)),
        "",
        "Commands:",
        *_render_commands(data),
        "",
        "Skills:  in a skill-capable harness the `brainstorm`, `spec`, `plan`, and "
        "`execute` skills drive\n  the conversation; these commands are the seam they call.",
    ]
    typer.echo("\n".join(lines))


@app.command(name="checkpoint", epilog="Example: specflo checkpoint --json")
def checkpoint_(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Print the resume prompt (and refresh checkpoint.md) for the active project."""
    root = _require_root()
    cfg = config.load_config(root)
    slug = _require_active(cfg)
    try:
        project = projects.load_project(root, cfg, slug)
    except SpecfloError as exc:
        raise _die(str(exc))
    payload = checkpoint.build_checkpoint(root, project, cfg=cfg)
    checkpoint.write_checkpoint(root, project, cfg=cfg)
    if json_output:
        typer.echo(json.dumps(payload))
    else:
        typer.echo(checkpoint.render_checkpoint(payload))


@app.command(name="auto", epilog="Example: specflo auto --autonomy yolo")
def auto_(
    autonomy: str = typer.Option(
        None,
        "--autonomy",
        help="Autonomy level: safe (default), autonomous, or yolo. Overrides the "
        ".specflo config default. safe/autonomous stop and hand off on any "
        "irreversible or outbound step; yolo permits them.",
    ),
    max_passes: int = typer.Option(
        None,
        "--max-passes",
        help="Iteration cap: escalate to the human after this many auto passes "
        f"(a runaway backstop). Overrides the .specflo config default "
        f"(default {config.DEFAULT_MAX_PASSES}).",
    ),
    off: bool = typer.Option(
        False,
        "--off",
        help="Set the durable auto-off kill switch for the active project and "
        "stop: the next `specflo auto` pass halts instead of continuing. Clear "
        "it with --on.",
    ),
    on: bool = typer.Option(
        False,
        "--on",
        help="Clear the auto-off kill switch, re-enabling normal auto continuation.",
    ),
) -> None:
    """Emit the auto-mode handoff payload for the active project (opt-in unattended run).

    `specflo auto` is the explicit, per-invocation opt-in that starts or continues
    an unattended run from the current phase toward project completion. It only
    prints the payload - it drives no loop, spawns no nested agent, and never
    clears context (REQ-05); the seamless clear-and-reseed trigger is the outer
    harness's job. `--autonomy` governs how far it runs unattended (REQ-08);
    `--max-passes` backstops the loop, escalating to the human at the cap (REQ-14).
    `--off`/`--on` set and clear a durable kill switch specflo respects each pass
    (REQ-16). Each pass counts in a durable per-project run-state file.
    Strictly additive: the ask-first `hook reseed` default is unchanged (REQ-02).
    Prints nothing when there is no active project.
    """
    if off and on:
        raise _die("--off and --on are mutually exclusive.")
    if off or on:
        out = auto_module.set_kill_switch(killed=off)  # --off sets; --on clears
        if out:
            typer.echo(out)
        return
    if autonomy is not None and autonomy not in auto_module.AUTONOMY_LEVELS:
        raise _die(
            f"invalid --autonomy {autonomy!r}; choose from "
            f"{', '.join(auto_module.AUTONOMY_LEVELS)}."
        )
    if max_passes is not None and max_passes < 1:
        raise _die("--max-passes must be a positive integer.")
    out = auto_module.auto_pass(autonomy=autonomy, max_passes=max_passes)
    if out:
        typer.echo(out)


@hook_app.command(
    "reseed",
    epilog="Wired into a SessionStart hook by `specflo hook print`.",
)
def hook_reseed(
    output_format: str = typer.Option(
        "text",
        "--format",
        help="'text' (portable payload) or 'claude' (SessionStart JSON: agent "
        "context + a user-visible nudge).",
    ),
) -> None:
    """Emit the clear-and-continue reseed payload for the active project.

    Default (`text`) prints the confirmation-gate directive + the verbatim
    checkpoint - portable across harnesses. `--format claude` emits Claude Code
    SessionStart JSON: the same payload as `additionalContext` plus a visible
    `systemMessage` telling the user what to type. Either way, prints nothing
    when there is no active project. Always exits 0, reads no stdin, makes no
    network calls - safe to wire into SessionStart unconditionally.
    """
    out = (
        hook.claude_session_start_output()
        if output_format == "claude"
        else hook.reseed_text()
    )
    if out:
        typer.echo(out)


@hook_app.command(
    "print",
    epilog="Example: specflo hook print --install",
)
def hook_print(
    install: bool = typer.Option(
        False, "--install", help="Merge the wiring into .claude/settings.json (idempotent)."
    ),
) -> None:
    """Print the SessionStart wiring for `hook reseed` (or --install it)."""
    if install:
        root = _require_root()
        path = hook.install_hook(root)
        typer.echo(f"Installed SessionStart hook -> {config.display_path(path, root)}")
    else:
        typer.echo(json.dumps(hook.settings_snippet(), indent=2))


@brainstorm_app.command("start", epilog="Example: specflo brainstorm start")
def brainstorm_start(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Create (or locate) the active project's brainstorm.md."""
    root = _require_root()
    cfg = config.load_config(root)
    slug = _require_active(cfg)
    try:
        path, created = brainstorm.start_brainstorm(root, cfg, slug)
    except SpecfloError as exc:
        raise _die(str(exc))
    _refresh_checkpoint(root, cfg, slug)
    if json_output:
        typer.echo(json.dumps({"path": str(path), "created": created}))
    else:
        note = "" if created else " (already started)"
        typer.echo(f"{path}{note}")


@decision_app.command(
    "add",
    epilog='Example: specflo decision add --text "Use SQLite" --rationale "simplest"',
)
def decision_add(
    text: str = typer.Option(..., "--text", help="The decision (one line)."),
    rationale: str = typer.Option(None, "--rationale", help="Why (recommended)."),
    supersedes: str = typer.Option(
        None, "--supersedes", metavar="D-NN", help="The decision this replaces."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Append a decision (D-NN) to the active project's brainstorm.md."""
    root = _require_root()
    cfg = config.load_config(root)
    slug = _require_active(cfg)
    try:
        decision = brainstorm.add_decision(
            root, cfg, slug, text, rationale=rationale, supersedes=supersedes
        )
    except SpecfloError as exc:
        raise _die(str(exc))
    _refresh_checkpoint(root, cfg, slug)
    if json_output:
        typer.echo(json.dumps({"id": decision.id, "supersedes": decision.supersedes}))
    else:
        message = f"Recorded {decision.id}."
        if decision.supersedes:
            message += f" Supersedes {decision.supersedes}."
        typer.echo(message)


@app.command(epilog="Example: specflo validate spec")
def validate(
    artifact: str = typer.Argument(
        ..., metavar="<artifact>",
        help="Artifact/phase to validate: brainstorm, spec, plan, or execute."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Lint an artifact; reports readiness and any issues."""
    root = _require_root()
    cfg = config.load_config(root)
    slug = _require_active(cfg)
    validator = VALIDATORS.get(artifact)
    if validator is None:
        known = ", ".join(sorted(VALIDATORS))
        raise _die(f"Unknown artifact {artifact!r}. Known: {known}.")
    issues = validator(root, cfg, slug)
    warner = WARNERS.get(artifact)
    warnings = warner(root, cfg, slug) if warner is not None else []
    if json_output:
        payload = {"ready": not issues, "issues": issues}
        if warner is not None:
            payload["warnings"] = warnings
        typer.echo(json.dumps(payload))
        raise typer.Exit(code=0 if not issues else 1)
    if warnings:
        typer.secho(f"{artifact} warnings:", fg=typer.colors.YELLOW, err=True)
        for w in warnings:
            typer.echo(f"  - {w}", err=True)
    if not issues:
        typer.echo(f"ok - {artifact} is ready.")
        return
    typer.secho(f"{artifact} has issues:", fg=typer.colors.YELLOW, err=True)
    for issue in issues:
        typer.echo(f"  - {issue}", err=True)
    raise typer.Exit(code=1)


@app.command(epilog="Example: specflo advance")
def advance(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Validate the current phase's artifact, then move to the next phase."""
    root = _require_root()
    cfg = config.load_config(root)
    slug = _require_active(cfg)
    try:
        project = projects.load_project(root, cfg, slug)
    except SpecfloError as exc:
        raise _die(str(exc))

    if project.status == projects.SHELVED_STATUS:
        raise _die(
            f"Project '{slug}' is shelved - run `specflo resume` first, then advance."
        )

    from_phase = project.phase
    to_phase = workflow.next_phase(from_phase)

    # Terminal phase: completing it completes the PROJECT (no phase bump).
    if to_phase is None:
        if project.status == projects.COMPLETE_STATUS:
            if json_output:
                typer.echo(json.dumps(
                    {"advanced": False, "from": from_phase, "to": None, "complete": True}))
            else:
                typer.echo(f"Project '{slug}' is already complete.")
            return
        validator = VALIDATORS.get(from_phase)
        if validator is not None:
            issues = validator(root, cfg, slug)
            if issues:
                if json_output:
                    typer.echo(json.dumps(
                        {"advanced": False, "from": from_phase, "to": None, "issues": issues}))
                    raise typer.Exit(code=1)
                typer.secho(f"cannot complete - {from_phase} is not ready:",
                            fg=typer.colors.YELLOW, err=True)
                for issue in issues:
                    typer.echo(f"  - {issue}", err=True)
                typer.echo("Fix these, then run `specflo advance` again.", err=True)
                raise typer.Exit(code=1)
        updated = projects.complete_project(root, cfg, slug)
        cp_display = config.display_path(checkpoint.write_checkpoint(root, updated, cfg=cfg), root)
        # Terminal continuation: a clear-point with no continue-instruction and
        # neither resume command named (REQ-07). Rendered once so the JSON field
        # and the prose carry the identical text (REQ-12).
        cont = continuation.build_continuation(
            from_phase,
            workflow.next_step(from_phase, complete=True),
            complete=True,
        )
        if json_output:
            typer.echo(json.dumps(
                {"advanced": True, "from": from_phase, "to": None,
                 "complete": True, "checkpoint": cp_display, "continuation": cont}))
        else:
            typer.echo(f"Completed project '{slug}'.")
            typer.echo(f"Checkpoint saved: {cp_display}")
            typer.echo(cont)
        return

    # Non-terminal: gate the leaving artifact, complete it, then bump the phase.
    gate = GATES.get(from_phase)
    if gate is not None:
        validator, completer = gate
        issues = validator(root, cfg, slug)
        if issues:
            if json_output:
                typer.echo(json.dumps(
                    {"advanced": False, "from": from_phase, "to": to_phase, "issues": issues}))
                raise typer.Exit(code=1)
            typer.secho(f"cannot advance - {from_phase} is not ready:",
                        fg=typer.colors.YELLOW, err=True)
            for issue in issues:
                typer.echo(f"  - {issue}", err=True)
            typer.echo("Fix these, then run `specflo advance` again.", err=True)
            raise typer.Exit(code=1)
        completer(root, cfg, slug)

    try:
        updated = projects.advance_project(root, cfg, slug)
    except SpecfloError as exc:
        raise _die(str(exc))

    cp_display = config.display_path(checkpoint.write_checkpoint(root, updated, cfg=cfg), root)

    # Progress-aware next step for the phase we just entered (e.g. advancing into
    # execute names the first actionable task). Non-task targets keep the static
    # hint (progress stays None), so other advances are unchanged.
    progress = None
    if updated.phase in ("plan", "execute") and plan.plan_path(root, cfg, slug).is_file():
        progress = plan.plan_progress(root, cfg, slug)
    next_step = workflow.next_step(updated.phase, progress=progress)
    # The shared continuation: the entered phase's next action, the phase skill
    # carrying it, and the clear-point naming both resume paths. Rendered once so
    # the JSON field and the prose carry the identical text (REQ-12).
    cont = continuation.build_continuation(updated.phase, next_step)

    if json_output:
        typer.echo(json.dumps(
            {"advanced": True, "from": from_phase, "to": updated.phase,
             "next_step": next_step, "checkpoint": cp_display,
             "continuation": cont}))
    else:
        typer.echo(f"Advanced '{slug}' from {from_phase} to {updated.phase}.")
        typer.echo(f"Checkpoint saved: {cp_display}")
        typer.echo(cont)


# The file each phase produces; execute has no artifact of its own (its work
# lives in plan.md). Used by `reopen` to flag downstream files that may now be
# stale after moving the pointer back.
_PHASE_ARTIFACT = {
    "brainstorm": brainstorm.BRAINSTORM_FILENAME,
    "spec": spec.SPEC_FILENAME,
    "plan": plan.PLAN_FILENAME,
}


def _stale_downstream_artifacts(project: projects.Project) -> list[str]:
    """Names of artifacts for phases *after* ``project.phase`` that exist on disk.

    A pure read of what already exists beyond the reopened phase, so `reopen` can
    warn which files may now be stale without touching any of them (REQ-10).
    """
    downstream = workflow.PHASES[workflow.PHASES.index(project.phase) + 1:]
    return [
        _PHASE_ARTIFACT[ph]
        for ph in downstream
        if ph in _PHASE_ARTIFACT and (project.path / _PHASE_ARTIFACT[ph]).is_file()
    ]


@app.command(epilog="Example: specflo reopen  |  specflo reopen brainstorm")
def reopen(
    phase: str = typer.Argument(
        None, metavar="[<phase>]",
        help="Earlier phase to reopen to; omit for the immediately previous phase.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Move the phase pointer backward (undo an advance); un-completes if complete.

    The inverse of `advance`: strictly backward. Bare reopen returns to the
    immediately previous phase; `reopen <phase>` jumps back to a named earlier
    phase. It is a pure pointer move — no downstream artifact is rewritten.
    """
    root = _require_root()
    cfg = config.load_config(root)
    slug = _require_active(cfg)
    try:
        project = projects.load_project(root, cfg, slug)
    except SpecfloError as exc:
        raise _die(str(exc))

    if project.status == projects.SHELVED_STATUS:
        raise _die(
            f"Project '{slug}' is shelved - run `specflo resume` first, then reopen."
        )

    from_phase = project.phase
    try:
        updated = projects.reopen_project(root, cfg, slug, phase)
    except SpecfloError as exc:
        raise _die(str(exc))

    stale = _stale_downstream_artifacts(updated)
    cp_display = config.display_path(checkpoint.write_checkpoint(root, updated, cfg=cfg), root)
    if json_output:
        typer.echo(json.dumps(
            {"reopened": True, "from": from_phase, "to": updated.phase,
             "stale": stale, "checkpoint": cp_display}))
    else:
        typer.echo(f"Reopened '{slug}' from {from_phase} to {updated.phase}.")
        if stale:
            typer.echo("Possibly stale downstream artifacts (unchanged on disk):")
            for name in stale:
                typer.echo(f"  - {name}")
        typer.echo(f"Checkpoint saved: {cp_display}")
        # Reopening is a clear-point too, and has been since before the shared
        # builder existed. It routes through the builder so no seam keeps its own
        # copy of the wording (REQ-04, D-06). The clear-point stays unconditional,
        # as it was before that rewiring: if the next step can't be derived, fall
        # back to the clear-point alone rather than emitting nothing (REQ-10).
        cont = _seam_continuation(root, cfg, slug)
        typer.echo(cont["continuation"] or continuation.clear_point_only())


@spec_app.command("start", epilog="Example: specflo spec start")
def spec_start(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Create (or locate) the active project's spec.md."""
    root = _require_root()
    cfg = config.load_config(root)
    slug = _require_active(cfg)
    try:
        path, created = spec.start_spec(root, cfg, slug)
    except SpecfloError as exc:
        raise _die(str(exc))
    _refresh_checkpoint(root, cfg, slug)
    if json_output:
        typer.echo(json.dumps({"path": str(path), "created": created}))
    else:
        note = "" if created else " (already started)"
        typer.echo(f"{path}{note}")


@requirement_app.command(
    "add",
    epilog='Example: specflo requirement add --text "Prints help" --acceptance "no-arg run exits 0"',
)
def requirement_add(
    text: str = typer.Option(..., "--text", help="The requirement (one line)."),
    acceptance: str = typer.Option(
        ..., "--acceptance", help="Pass/fail acceptance criterion (required)."
    ),
    from_: str = typer.Option(
        None, "--from", metavar="D-NN", help="The brainstorm decision this derives from."
    ),
    supersedes: str = typer.Option(
        None, "--supersedes", metavar="REQ-NN", help="The requirement this replaces."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Append a requirement (REQ-NN) to the active project's spec.md."""
    root = _require_root()
    cfg = config.load_config(root)
    slug = _require_active(cfg)
    try:
        requirement = spec.add_requirement(
            root, cfg, slug, text, acceptance, derives_from=from_, supersedes=supersedes
        )
    except SpecfloError as exc:
        raise _die(str(exc))
    _refresh_checkpoint(root, cfg, slug)
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "id": requirement.id,
                    "derives_from": requirement.derives_from,
                    "supersedes": requirement.supersedes,
                }
            )
        )
    else:
        message = f"Recorded {requirement.id}."
        if requirement.derives_from:
            message += f" Derives from {requirement.derives_from}."
        if requirement.supersedes:
            message += f" Supersedes {requirement.supersedes}."
        typer.echo(message)


@plan_app.command("start", epilog="Example: specflo plan start")
def plan_start(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Create (or locate) the active project's plan.md."""
    root = _require_root()
    cfg = config.load_config(root)
    slug = _require_active(cfg)
    try:
        path, created = plan.start_plan(root, cfg, slug)
    except SpecfloError as exc:
        raise _die(str(exc))
    _refresh_checkpoint(root, cfg, slug)
    if json_output:
        typer.echo(json.dumps({"path": str(path), "created": created}))
    else:
        note = "" if created else " (already started)"
        typer.echo(f"{path}{note}")


@task_app.command(
    "add",
    epilog='Example: specflo task add --text "Build X" --acceptance "X works" --verify "uv run pytest" --from REQ-01',
)
def task_add(
    text: str = typer.Option(..., "--text", help="The task title (one line)."),
    acceptance: str = typer.Option(..., "--acceptance", help="Pass/fail acceptance criterion (required)."),
    verify: str = typer.Option(..., "--verify", help="Verification command or step (required)."),
    from_: list[str] = typer.Option(
        ..., "--from", metavar="REQ-NN", help="Requirement(s) this task implements (repeatable; >=1)."
    ),
    depends_on: list[str] = typer.Option(
        None, "--depends-on", metavar="T-NN", help="Task(s) this depends on (repeatable)."
    ),
    files: str = typer.Option(None, "--files", help="Files likely touched."),
    scope: str = typer.Option(None, "--scope", help="Estimated scope (Small/Medium/Large)."),
    supersedes: str = typer.Option(None, "--supersedes", metavar="T-NN", help="The task this replaces."),
    milestone: str = typer.Option(
        None, "--milestone", metavar="M-NN", help="The milestone this task belongs to."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Append a task (T-NN) to the active project's plan.md."""
    root = _require_root()
    cfg = config.load_config(root)
    slug = _require_active(cfg)
    try:
        task = plan.add_task(
            root, cfg, slug, text, acceptance, verify,
            implements=list(from_), depends_on=list(depends_on or []),
            files=files, scope=scope, supersedes=supersedes, milestone=milestone,
        )
    except SpecfloError as exc:
        raise _die(str(exc))
    _refresh_checkpoint(root, cfg, slug)
    # Detect-and-offer: when this task supersedes another, surface any active
    # tasks still depending on the superseded one and the literal rewire command
    # to repoint them. We never modify those dependents here (that is `task rewire`).
    dependents = (
        plan.active_dependents(root, cfg, slug, task.supersedes) if task.supersedes else []
    )
    if json_output:
        typer.echo(json.dumps({
            "id": task.id, "implements": task.implements,
            "depends_on": task.depends_on, "supersedes": task.supersedes,
            "milestone": task.milestone, "dependents": dependents,
        }))
    else:
        message = f"Recorded {task.id} (implements {', '.join(task.implements)})."
        if task.supersedes:
            message += f" Supersedes {task.supersedes}."
        typer.echo(message)
        if dependents:
            typer.echo(
                f"Note: {len(dependents)} active task(s) still depend on "
                f"{task.supersedes}: {', '.join(dependents)}."
            )
            typer.echo("  To repoint them onto the replacement, run:")
            typer.echo(f"  specflo task rewire --from {task.supersedes} --to {task.id}")


@task_app.command("rewire", epilog="Example: specflo task rewire --from T-04 --to T-11")
def task_rewire(
    from_: str = typer.Option(
        ..., "--from", metavar="T-NN",
        help="The (superseded) task whose active dependents are repointed."
    ),
    to: str = typer.Option(
        ..., "--to", metavar="T-MM", help="The task to repoint those dependents onto."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Repoint every active task depending on --from to depend on --to instead."""
    root = _require_root(); cfg = config.load_config(root); slug = _require_active(cfg)
    try:
        changed = plan.rewire_dependency(root, cfg, slug, from_, to)
    except SpecfloError as exc:
        raise _die(str(exc))
    _refresh_checkpoint(root, cfg, slug)
    if json_output:
        typer.echo(json.dumps({"from": from_, "to": to, "rewired": changed}))
    elif changed:
        typer.echo(f"Rewired {', '.join(changed)} from {from_} to {to}.")
    else:
        typer.echo(f"No active tasks depended on {from_}; nothing to rewire.")


@task_app.command("set-milestone", epilog="Example: specflo task set-milestone T-01 M-02")
def task_set_milestone(
    task_id: str = typer.Argument(..., metavar="<T-NN>", help="Task to (re)assign."),
    milestone_id: str = typer.Argument(..., metavar="<M-NN>", help="Milestone to assign it to."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Assign (or reassign) a task's milestone in place."""
    root = _require_root(); cfg = config.load_config(root); slug = _require_active(cfg)
    try:
        task = plan.set_milestone(root, cfg, slug, task_id, milestone_id)
    except SpecfloError as exc:
        raise _die(str(exc))
    _refresh_checkpoint(root, cfg, slug)
    if json_output:
        typer.echo(json.dumps({"id": task.id, "milestone": task.milestone}))
    else:
        typer.echo(f"{task.id} -> milestone {task.milestone}")


# The continuation fields a seam contributes to its `--json`. Always present, so
# a harness can rely on the keys existing even when the values could not be
# derived (REQ-12); `None` says "underivable", never "absent".
_CONTINUATION_KEYS = ("next_step", "checkpoint", "continuation")


def _seam_continuation(root: Path, cfg: config.SpecfloConfig, slug: str) -> dict:
    """The continuation fields for a seam that has already mutated state.

    Used by `task done` and `reopen` — the seams whose next step is best derived
    from the project's freshly-written checkpoint rather than computed inline.

    Completing a task is a clean place to clear context, so the seam carries the
    same four-part shape `advance` does (REQ-01): the transition line (printed by
    :func:`_report_transition`), the checkpoint location, and the continuation
    block — whose next-step hint comes from ``build_checkpoint`` so it cannot
    drift from what `status` and `checkpoint` report for the same state (REQ-05).

    Rendered once and shared by the prose and `--json` paths, so the two carry
    identical text (REQ-12).

    Best-effort, mirroring :func:`_refresh_checkpoint`: the mutation has already
    succeeded and been persisted, so failing to derive the continuation must never
    fail the command — a non-zero exit after a committed state change would be
    worse than a missing line, and could send a harness into a retry.

    The catch is therefore broad, but the failure is *visible* rather than silent:
    every key stays present carrying ``None``, so a `--json` consumer reads
    "underivable" instead of hitting a missing key (REQ-12), and an advisory goes
    to stderr so a genuine bug (say a malformed ``plan.md``) is not mistaken for a
    project that simply had nothing to say.
    """
    try:
        project = projects.load_project(root, cfg, slug)
        payload = checkpoint.build_checkpoint(root, project, cfg=cfg)
        # Inside the guard: reading the payload and rendering must be covered too,
        # or the "never fail the caller" guarantee would be narrower than stated.
        return {
            "next_step": payload["do_next"],
            "checkpoint": payload["path"],
            "continuation": continuation.build_continuation(
                payload["phase"], payload["do_next"]
            ),
        }
    except Exception as exc:  # noqa: BLE001 - see docstring; never fail the caller
        typer.secho(
            f"note: could not derive the continuation ({exc.__class__.__name__}: {exc}).",
            fg=typer.colors.YELLOW,
            err=True,
        )
        return dict.fromkeys(_CONTINUATION_KEYS)


def _report_transition(
    task: plan.Task, json_output: bool, extra: dict | None = None
) -> None:
    """Print a task's state transition; ``extra`` adds fields to the JSON form.

    Only `task done` passes ``extra`` — the other verbs stay terse, since
    completing a task is the one task-level clear-point (REQ-01). The transition
    fields are applied last, so ``extra`` can never overwrite them.
    """
    if json_output:
        payload = {
            **(extra or {}),
            "id": task.id,
            "progress": task.progress,
            "blocked": task.blocked,
        }
        typer.echo(json.dumps(payload))
    else:
        line = f"{task.id} -> {task.progress}"
        if task.blocked:
            line += f" ({task.blocked})"
        typer.echo(line)


@task_app.command("start", epilog="Example: specflo task start T-01")
def task_start(
    task_id: str = typer.Argument(..., metavar="<T-NN>", help="Task to start."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Mark a task in_progress."""
    root = _require_root(); cfg = config.load_config(root); slug = _require_active(cfg)
    try:
        task = plan.start_task(root, cfg, slug, task_id)
    except SpecfloError as exc:
        raise _die(str(exc))
    _refresh_checkpoint(root, cfg, slug)
    _report_transition(task, json_output)


@task_app.command("done", epilog="Example: specflo task done T-01")
def task_done(
    task_id: str = typer.Argument(..., metavar="<T-NN>", help="Task to mark done."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Mark a task done."""
    root = _require_root(); cfg = config.load_config(root); slug = _require_active(cfg)
    try:
        task = plan.done_task(root, cfg, slug, task_id)
    except SpecfloError as exc:
        raise _die(str(exc))
    _refresh_checkpoint(root, cfg, slug)
    # Unlike the other task verbs, completing a task is a clear-point: it gets the
    # full continuation (REQ-01). start/block/reopen stay terse by design.
    cont = _seam_continuation(root, cfg, slug)
    _report_transition(task, json_output, extra=cont)
    if not json_output:
        if cont["continuation"] is None:
            # Same fallback reopen takes: a seam that is a clear-point stays one
            # even when the next step is underivable, so a harness grepping the
            # prose for the marker never silently stops resuming (REQ-01).
            typer.echo(continuation.clear_point_only())
        else:
            typer.echo(f"Checkpoint saved: {cont['checkpoint']}")
            typer.echo(cont["continuation"])


@task_app.command("block", epilog='Example: specflo task block T-01 --reason "waiting on API"')
def task_block(
    task_id: str = typer.Argument(..., metavar="<T-NN>", help="Task to block."),
    reason: str = typer.Option(None, "--reason", help="Why it's blocked."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Mark a task blocked (optionally recording a reason)."""
    root = _require_root(); cfg = config.load_config(root); slug = _require_active(cfg)
    try:
        task = plan.block_task(root, cfg, slug, task_id, reason=reason)
    except SpecfloError as exc:
        raise _die(str(exc))
    _refresh_checkpoint(root, cfg, slug)
    _report_transition(task, json_output)


@task_app.command("reopen", epilog="Example: specflo task reopen T-01")
def task_reopen(
    task_id: str = typer.Argument(..., metavar="<T-NN>", help="Task to reopen (back to pending)."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Return a task to pending (clears any block)."""
    root = _require_root(); cfg = config.load_config(root); slug = _require_active(cfg)
    try:
        task = plan.reopen_task(root, cfg, slug, task_id)
    except SpecfloError as exc:
        raise _die(str(exc))
    _refresh_checkpoint(root, cfg, slug)
    _report_transition(task, json_output)


@task_app.command("list", epilog="Example: specflo task list")
def task_list(
    all_: bool = typer.Option(False, "--all", help="Include superseded tasks."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """List tasks with progress and the deps-aware next-actionable marker."""
    root = _require_root(); cfg = config.load_config(root); slug = _require_active(cfg)
    try:
        tasks = plan.list_tasks(root, cfg, slug, include_superseded=all_)
        progress = plan.plan_progress(root, cfg, slug)
    except SpecfloError as exc:
        raise _die(str(exc))
    nexts = set(progress["next_actionable"])
    if json_output:
        typer.echo(json.dumps({
            "tasks": [
                {"id": t.id, "text": t.text, "progress": t.progress, "status": t.status,
                 "implements": t.implements, "depends_on": t.depends_on, "next": t.id in nexts}
                for t in tasks
            ],
            "progress": progress,
        }))
        return
    if not tasks:
        typer.echo("No tasks yet. Add one with `specflo task add`.")
        return
    for t in tasks:
        marker = ">" if t.id in nexts else " "
        sup = "  (superseded)" if t.status != "active" else ""
        deps = f"  deps: {', '.join(t.depends_on)}" if t.depends_on else ""
        typer.echo(f"{marker} {t.id}  [{t.progress}]  {t.text}{deps}{sup}")
    tail = ""
    if progress["next_actionable"]:
        tail = " | next: " + ", ".join(progress["next_actionable"])
    typer.echo(f"\n{progress['done']}/{progress['total']} done{tail}")


@task_app.command("show", epilog="Example: specflo task show T-01")
def task_show(
    task_id: str = typer.Argument(
        None, metavar="[<T-NN>]", help="Task to show (default: next actionable)."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Show a task's brief: acceptance, verify, its cited REQ-NN sections, and Global constraints."""
    root = _require_root(); cfg = config.load_config(root); slug = _require_active(cfg)
    try:
        brief = plan.task_brief(root, cfg, slug, task_id)
    except SpecfloError as exc:
        raise _die(str(exc))
    if json_output:
        typer.echo(json.dumps(brief))
        return
    t = brief["task"]
    if t is None:
        # Every task is done: no task to brief, just the all-complete boundary
        # beat (the final milestone's Exit checklist + a proceed prompt) (REQ-14).
        typer.echo("\n".join(
            ["All tasks done — nothing left to brief.", ""]
            + plan.boundary_beat_lines(brief["boundary"])
        ))
        return
    header = f"{t['id']} - {t['text']}  [{t['progress']}]"
    if brief.get("working_ahead"):
        header += "  — working ahead (later milestone than current)"
    lines = [
        header,
        f"  Acceptance: {t['acceptance']}",
        f"  Verify:     {t['verify']}",
        f"  Implements: {', '.join(t['implements'])}",
    ]
    if t["depends_on"]:
        lines.append(f"  Depends on: {', '.join(t['depends_on'])}")
    lines.append("")
    for req in brief["requirements"]:
        lines.append(req["section"].rstrip() if req["section"]
                     else f"### {req['id']} - (not found in spec)")
        lines.append("")
    if brief["global_constraints"]:
        lines.append("## Global constraints")
        lines.append(brief["global_constraints"])
    # Surface the soft milestone-boundary verify beat (the just-completed
    # milestone's Exit checklist) after the brief, when at a boundary (REQ-14).
    if brief.get("boundary"):
        lines.append("")
        lines.extend(plan.boundary_beat_lines(brief["boundary"]))
    typer.echo("\n".join(lines))


@milestone_app.command(
    "add",
    epilog='Example: specflo milestone add --text "Auth works" --exit "login" --exit "logout"',
)
def milestone_add(
    text: str = typer.Option(..., "--text", help="The milestone title (one line)."),
    exit_: list[str] = typer.Option(
        ..., "--exit", metavar="ITEM",
        help="Exit checklist item — an observable behaviour (repeatable; >=1).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Append a milestone (M-NN) with its Exit checklist to the active plan.md."""
    root = _require_root(); cfg = config.load_config(root); slug = _require_active(cfg)
    try:
        milestone = plan.add_milestone(root, cfg, slug, text, exit_items=list(exit_))
    except SpecfloError as exc:
        raise _die(str(exc))
    _refresh_checkpoint(root, cfg, slug)
    if json_output:
        typer.echo(json.dumps(
            {"id": milestone.id, "title": milestone.title, "exit": milestone.exit_items}))
    else:
        typer.echo(
            f"Recorded {milestone.id} ({len(milestone.exit_items)} exit item(s))."
        )


@milestone_app.command("list", epilog="Example: specflo milestone list")
def milestone_list(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """List milestones in order with done/total rollup, marking the current one."""
    root = _require_root(); cfg = config.load_config(root); slug = _require_active(cfg)
    view = plan.milestone_progress(root, cfg, slug)
    if json_output:
        typer.echo(json.dumps(view))
        return
    milestones = view["milestones"]
    if not milestones:
        typer.echo("No milestones yet. Add one with `specflo milestone add`.")
        return
    for m in milestones:
        marker = ">" if m["id"] == view["current"] else " "
        suffix = "  (complete)" if m["complete"] else ""
        typer.echo(f"{marker} {m['id']}  [{m['done']}/{m['total']}]  {m['title']}{suffix}")
    current = view["current"]
    typer.echo(f"\nCurrent: {current}" if current else
               "\nCurrent: none (all milestones complete).")


@milestone_app.command("show", epilog="Example: specflo milestone show M-01")
def milestone_show(
    milestone_id: str = typer.Argument(..., metavar="<M-NN>", help="Milestone to show."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Show a milestone: its Exit checklist, member tasks, rollup, and derived REQ set."""
    root = _require_root(); cfg = config.load_config(root); slug = _require_active(cfg)
    detail = plan.milestone_detail(root, cfg, slug, milestone_id)
    if detail is None:
        raise _die(f"No milestone {milestone_id} in this plan.")
    if json_output:
        typer.echo(json.dumps(detail))
        return
    suffix = "  (complete)" if detail["complete"] else ""
    lines = [
        f"{detail['id']} - {detail['title']}  [{detail['done']}/{detail['total']}]{suffix}",
        "",
        "Exit:",
        *(f"  - {item}" for item in detail["exit_items"]),
        "",
        "Tasks:",
    ]
    if detail["members"]:
        lines += [f"  {t['id']}  [{t['progress']}]  {t['text']}" for t in detail["members"]]
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append("Requirements: " + (", ".join(detail["reqs"]) if detail["reqs"] else "(none)"))
    typer.echo("\n".join(lines))


def build_cli():
    """The specflo click command with agentsquire's skills group mounted.

    The group is mounted onto the click command underlying the Typer app (Typer
    can't nest a ready-made click group directly). No ``source=`` is passed, so
    every verb resolves through agentsquire's zero-arg default union: Root A
    (specflo's package data, empty) plus Root B (repo-root ``skills/`` -- read
    from ``specflo/_repo_skills`` in a wheel, or via marker-walk in an editable
    checkout). ``source_package``/``source_version`` stamp installs with specflo
    provenance so status/update/uninstall attribute correctly.
    """
    cli = typer.main.get_command(app)
    cli.add_command(
        skills_command_group(
            "specflo",
            default_scope="user",
            source_package="specflo",
            source_version=__version__,
        )
    )
    return cli


def _notify_stale_skills() -> None:
    # Notice-only startup hook: one stderr line when installed specflo skills
    # have updates available (never prompts, never updates, never touches the
    # exit code). Resolved over the ZERO-ARG default union (Root A + Root B), NOT
    # a bare BundledPackageDataSource -- the union is what sees the repo-level
    # skills, so staleness is measured against what `specflo skills` installs.
    # check_stale already swallows its own errors; this try/except is
    # belt-and-suspenders so a startup notice can never break the command.
    try:
        check_stale(
            default_source("specflo"),
            prog_name="specflo",
            update_command="specflo skills update",
        )
    except Exception:
        pass


def main() -> None:
    _notify_stale_skills()
    # typer 0.26 vendors its own copy of click (typer._click), so exceptions
    # raised by the mounted agentsquire group -- which uses the real `click` --
    # are a different class than the ones typer's runner catches. On `specflo
    # skills ... --help` (and agentsquire usage errors) the real-click exception
    # escapes typer's handler; catch those foreign exceptions here so the process
    # exits cleanly instead of dumping a traceback. typer's own exits (top-level
    # --help/--version, the existing commands) are handled inside cli() and reach
    # us as SystemExit, which passes straight through.
    cli = build_cli()
    try:
        cli()
    except click.exceptions.Exit as exc:
        raise SystemExit(exc.exit_code)
    except click.exceptions.Abort:
        typer.echo("Aborted!", err=True)
        raise SystemExit(1)
    except click.exceptions.ClickException as exc:
        exc.show()
        raise SystemExit(exc.exit_code)

"""The specflo command-line interface."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from typer.core import TyperGroup

from . import brainstorm, config, projects, spec, workflow
from .errors import SpecfloError


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

brainstorm_app = typer.Typer(help="Work with the brainstorm artifact.")
app.add_typer(brainstorm_app, name="brainstorm")

decision_app = typer.Typer(help="Capture brainstorm decisions.")
app.add_typer(decision_app, name="decision")

spec_app = typer.Typer(help="Work with the spec artifact.")
app.add_typer(spec_app, name="spec")

requirement_app = typer.Typer(help="Capture spec requirements.")
app.add_typer(requirement_app, name="requirement")


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


def _project_dir_display(path: Path, root: Path) -> str:
    """The project dir relative to the repo root, or absolute if it lies outside."""
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


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
    typer.echo(
        f"Created project '{project.slug}' (now active). Phase: {project.phase}."
    )


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
        typer.echo(f"{marker} {p.slug}  ({p.phase})")


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

    info = {
        "initialized": True,
        "active_project": project.slug,
        "name": project.name,
        "dir": str(project.path),
        "phase": project.phase,
        "status": project.status,
        "next_phase": workflow.next_phase(project.phase),
        "next_step": workflow.next_step(project.phase),
    }
    if json_output:
        typer.echo(json.dumps(info))
    else:
        label = (
            project.name
            if project.name == project.slug
            else f"{project.name} ({project.slug})"
        )
        typer.echo(f"Project: {label}")
        typer.echo(f"Dir:     {_project_dir_display(project.path, root)}")
        typer.echo(f"Phase:   {project.phase}")
        typer.echo(f"Next:    {info['next_step']}")


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
    if json_output:
        typer.echo(json.dumps({"id": decision.id, "supersedes": decision.supersedes}))
    else:
        message = f"Recorded {decision.id}."
        if decision.supersedes:
            message += f" Supersedes {decision.supersedes}."
        typer.echo(message)


@app.command(epilog="Example: specflo validate brainstorm")
def validate(
    artifact: str = typer.Argument(
        ..., metavar="<artifact>", help="Artifact to validate (e.g. brainstorm)."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Lint an artifact; reports readiness and any issues."""
    root = _require_root()
    cfg = config.load_config(root)
    slug = _require_active(cfg)
    if artifact != "brainstorm":
        raise _die(f"Unknown artifact {artifact!r}. Known: brainstorm.")
    issues = brainstorm.validate_brainstorm(root, cfg, slug)
    if json_output:
        typer.echo(json.dumps({"ready": not issues, "issues": issues}))
        raise typer.Exit(code=0 if not issues else 1)
    if not issues:
        typer.echo("ok — brainstorm is ready.")
        return
    typer.secho("brainstorm has issues:", fg=typer.colors.YELLOW, err=True)
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

    from_phase = project.phase
    to_phase = workflow.next_phase(from_phase)
    if to_phase is None:
        if json_output:
            typer.echo(json.dumps({"advanced": False, "from": from_phase, "to": None}))
            raise typer.Exit(code=1)
        raise _die(f"Project '{slug}' is already at the final phase '{from_phase}'.")

    # Gate: validate the leaving artifact. Only brainstorm has a validator today;
    # other phases advance ungated until their artifacts exist.
    if from_phase == "brainstorm":
        issues = brainstorm.validate_brainstorm(root, cfg, slug)
        if issues:
            if json_output:
                typer.echo(
                    json.dumps(
                        {"advanced": False, "from": from_phase, "to": to_phase, "issues": issues}
                    )
                )
                raise typer.Exit(code=1)
            typer.secho("cannot advance — brainstorm is not ready:", fg=typer.colors.YELLOW, err=True)
            for issue in issues:
                typer.echo(f"  - {issue}", err=True)
            typer.echo("Fix these, then run `specflo advance` again.", err=True)
            raise typer.Exit(code=1)
        brainstorm.complete_brainstorm(root, cfg, slug)

    try:
        updated = projects.advance_project(root, cfg, slug)
    except SpecfloError as exc:
        raise _die(str(exc))

    if json_output:
        typer.echo(json.dumps({"advanced": True, "from": from_phase, "to": updated.phase}))
    else:
        typer.echo(f"Advanced '{slug}' from {from_phase} to {updated.phase}.")
        typer.echo(f"Next: {workflow.next_step(updated.phase)}")


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
    if json_output:
        typer.echo(json.dumps({"path": str(path), "created": created}))
    else:
        note = "" if created else " (already started)"
        typer.echo(f"{path}{note}")


def main() -> None:
    app()

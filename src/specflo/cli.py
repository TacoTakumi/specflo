"""The specflo command-line interface."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from . import config, projects, workflow
from .errors import SpecfloError

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="A spec-driven software-engineering workflow.",
)


def _die(message: str) -> typer.Exit:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    return typer.Exit(code=1)


def _require_root() -> Path:
    root = config.find_root(Path.cwd())
    if root is None:
        raise _die("Not a specflo project. Run `specflo init` first.")
    return root


@app.command()
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


@app.command()
def new(name: str) -> None:
    """Create a project and make it the active one."""
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


@app.command()
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
        "phase": project.phase,
        "status": project.status,
        "next_phase": workflow.next_phase(project.phase),
        "next_step": workflow.next_step(project.phase),
    }
    if json_output:
        typer.echo(json.dumps(info))
    else:
        typer.echo(f"Project: {project.name} ({project.slug})")
        typer.echo(f"Phase:   {project.phase}")
        typer.echo(f"Next:    {info['next_step']}")


def main() -> None:
    app()

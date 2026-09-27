"""The specflo command-line interface."""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import sys
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
from . import brainstorm, checkpoint, checkouts, config, continuation, guide as guide_module, hook, plan, projects, spec
from . import doc as doc_module
from . import followup as followup_module
from . import graph as graph_module
from . import extension_install as extension_module
from . import status as status_view
from . import workflow
from .agent import cli as agent_cli
from .agent.cli import agent_app
from .daemon.cli import serve_app
from .daemon import workitems as workitems_module
from .daemon.products import RemoteProducts
from .daemon.workitems import RemoteWorkItems
from .errors import SpecfloError
from .service import ProjectService
from .service import promote as promote_module
from .service import wire
from .service.resolve import local_service, remote_service, resolve_service
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
    ctx: typer.Context,
    version: bool = typer.Option(
        None,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the specflo version and exit.",
    ),
    directory: Path | None = typer.Option(
        None,
        "-C",
        "--directory",
        envvar="SPECFLO_DIRECTORY",
        exists=True,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        metavar="DIR",
        help=(
            "Run as if specflo had been started in DIR. DIR must exist. "
            "Relative path arguments of the subcommand resolve against DIR. "
            "The flag wins over SPECFLO_DIRECTORY, which wins over the current directory."
        ),
    ),
) -> None:
    """A spec-driven software-engineering workflow."""
    if directory is None:
        _record_checkout()
        return
    # The caller's cwd may already be gone (a shell left in a deleted directory)
    # or vanish while the command runs; -C exists to make that cwd irrelevant,
    # so neither end of the restore may fail the command.
    try:
        previous: str | None = os.getcwd()
    except OSError:
        previous = None
    os.chdir(directory)
    if previous is not None:
        ctx.call_on_close(lambda: _restore_cwd(previous))
    # Compared by name: Typer 0.26 ships its own click fork (``typer._click``)
    # and ``ctx`` reports that fork's ``ParameterSource`` members, which are
    # not the ``click.core.ParameterSource`` members this module imports.
    source = ctx.get_parameter_source("directory")
    from_env = getattr(source, "name", None) == "ENVIRONMENT"
    ctx.obj = {
        "directory": directory,
        "directory_source": "env" if from_env else "flag",
    }
    _record_checkout()


def _record_checkout() -> None:
    """Record the checkout the command runs in, so the pool can hide its
    tokens from a member that lists a directory above it."""
    with contextlib.suppress(OSError):
        checkouts.record_found(Path.cwd())


def _restore_cwd(previous: str) -> None:
    """Put the process back where ``-C`` found it; a vanished cwd is not an error."""
    with contextlib.suppress(OSError):
        os.chdir(previous)


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
pool_app = typer.Typer(help="Declare the resource pools tasks can need (fan-out).")
app.add_typer(pool_app, name="pool")

review_app = typer.Typer(help="Record end-of-execute review rounds.")
app.add_typer(review_app, name="review")

gate_app = typer.Typer(help="Hand the active project to a role and record who takes it.")
app.add_typer(gate_app, name="gate")

doc_app = typer.Typer(help="Read the active project's artifacts by name.")
app.add_typer(doc_app, name="doc")

section_app = typer.Typer(help="Write one prose section of an artifact.")
app.add_typer(section_app, name="section")

followup_app = typer.Typer(help="Record the work a project leaves for a later one (FU-NN).")
app.add_typer(followup_app, name="followup")

hook_app = typer.Typer(help="Session-start integration (clear-and-continue).")
app.add_typer(hook_app, name="hook")

extension_app = typer.Typer(help="Install the bundled pi extension.")
app.add_typer(extension_app, name="extension")

config_app = typer.Typer(help="Read and change specflo's own settings.")
app.add_typer(config_app, name="config")

# The daemon: `specflo serve` hosts projects for CLI clients. Its module
# imports no web framework, so registering it costs nothing without the
# serve extra.
app.add_typer(serve_app, name="serve")

remote_app = typer.Typer(help="Register the daemons this checkout can reach.")
app.add_typer(remote_app, name="remote")

product_app = typer.Typer(help="Manage products: the things work items and projects belong to.")
app.add_typer(product_app, name="product")
piece_app = typer.Typer(
    help="Declare the pieces a product is made of (web, admin, mobile, ...); work items may target them."
)
product_app.add_typer(piece_app, name="piece")

workitem_app = typer.Typer(
    help="Manage work items: a product's backlog entries, each with a kind, a dev path, and a status."
)
app.add_typer(workitem_app, name="workitem")

# The lease verbs are declared here and run in the pool package's lease
# module, which is loaded when one of them runs: a local command pays nothing
# for the agent pool.
lease_app = typer.Typer(help="Take and give back members of a daemon's agent pool.")
app.add_typer(lease_app, name="lease")

# The console verbs are declared and run the same way, in the pool package's
# console verbs module.
console_app = typer.Typer(
    help="Attach your own running agent to a console slot of a daemon's agent pool."
)
app.add_typer(console_app, name="console")

# Composition point only: the agent subsystem stays import-independent of
# pipeline code (REQ-15); the top-level CLI is where both meet. The callback
# bridges the pipeline `agent_space` config value into the env var the agent
# group reads, without the agent subsystem importing config.
app.add_typer(agent_app, name="agent")


@agent_app.callback()
def _agent_group() -> None:
    """Run and control pi subagents (headless pi hosts)."""
    if agent_cli.AGENT_SPACE_ENV in os.environ:
        return
    try:
        root = config.find_root(Path.cwd())
        if root is None:
            return
        value = getattr(config.load_config(root), "agent_space", None)
        if value:
            os.environ[agent_cli.AGENT_SPACE_ENV] = value
    except Exception:  # config trouble must never break agent verbs
        pass


def _die(message: str) -> typer.Exit:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    return typer.Exit(code=1)


# How long `list` waits on each daemon, in seconds: the listing never fails on
# account of one remote, and it should not stall on one either.
REMOTE_LIST_TIMEOUT = 5.0


def _service(root: Path, cfg: config.SpecfloConfig, slug: str | None = None) -> ProjectService:
    """The service holding ``slug`` (the active project by default), or a clean refusal.

    A hosted project whose remote is no longer registered is a bad registry,
    not a bug: the message names the remote and the command exits 1 like any
    other refusal, instead of a traceback.
    """
    try:
        return resolve_service(root, cfg, slug)
    except SpecfloError as exc:
        raise _die(str(exc))


def _client_checkpoint(svc: ProjectService, root: Path, slug: str) -> dict:
    """The checkpoint payload as this checkout reports it: locators for a hosted project."""
    payload = svc.build_checkpoint(slug)
    if config.hosting_remote(root, slug) is not None:
        return checkpoint.hosted_view(payload)
    return payload


def _client_status(svc: ProjectService, root: Path, slug: str) -> dict:
    """The status payload as this checkout reports it: the remote, not a directory, for a hosted project."""
    info = svc.build_status(slug)
    remote = config.hosting_remote(root, slug)
    if remote is not None:
        return status_view.hosted_view(info, remote)
    return info


def _locator(slug: str, path: Path) -> str:
    """The artifact locator ``<project>/<artifact>`` a command prints instead of a path.

    A locator names the artifact the same way wherever its bytes live, so the
    human-readable line is identical for a project in this checkout and one
    held by a daemon. ``--json`` output carries the locator and, for a local
    project, the path as a separate field.
    """
    return f"{slug}/{path.stem}"


def _artifact_report(root: Path, slug: str, path: Path) -> tuple[str, str | None]:
    """How a command reports an artifact it just made or closed: its locator, then its path.

    The human line carries the locator, so it reads the same for a project in
    this checkout and one held by a daemon. ``--json`` keeps the path for a
    project in this checkout; a hosted project's files are the daemon's, so
    its path is None rather than a directory on another machine.
    """
    if config.hosting_remote(root, slug) is not None:
        return _locator(slug, path), None
    return _locator(slug, path), str(path)


def _checkpoint_report(root: Path, slug: str, path: Path) -> tuple[str, str | None]:
    """As :func:`_artifact_report`, with the checkpoint's path shown relative to the root."""
    locator, reported = _artifact_report(root, slug, path)
    return locator, None if reported is None else config.display_path(path, root)


def _directory_override(ctx: click.Context) -> dict | None:
    """The ``-C``/``SPECFLO_DIRECTORY`` override recorded by the app callback, if any."""
    obj = ctx.find_root().obj
    return obj if isinstance(obj, dict) and "directory" in obj else None


def _require_root() -> Path:
    root = config.find_root(Path.cwd())
    if root is None:
        raise _die("Not a specflo project. Run `specflo init` first.")
    return root


def _require_active(cfg: config.SpecfloConfig) -> str:
    if cfg.active_project is None:
        raise _die(guide_module.NO_ACTIVE_PROJECT_MESSAGE)
    return cfg.active_project


def _report_unwritten_checkpoint(exc: Exception) -> None:
    """One stderr line: the checkpoint was not written, and how to regenerate it."""
    typer.secho(
        f"note: checkpoint not written ({exc.__class__.__name__}: {exc}); "
        "run `specflo checkpoint` to regenerate it.",
        fg=typer.colors.YELLOW,
        err=True,
    )


def _refresh_checkpoint(svc: ProjectService, slug: str) -> bool:
    """Best-effort: rewrite the active project's checkpoint.md after a mutation.

    The checkpoint is fully derived, so this is cheap and always current. It runs
    after the triggering mutation has already succeeded and been persisted, so a
    failure here must never fail that command — catch *any* refresh error
    (a failed load, or a write that hits a read-only FS, permissions, full disk,
    or a clobbered path), say so on stderr, and move on. Returns whether the
    checkpoint was written, so a caller never reports a save that did not happen.
    """
    try:
        svc.write_checkpoint(slug)
    except Exception as exc:  # noqa: BLE001 - see docstring; never fail the caller
        _report_unwritten_checkpoint(exc)
        return False
    return True


def _checkpoint_after(
    svc: ProjectService, root: Path, slug: str
) -> tuple[str | None, str | None]:
    """The checkpoint report for a mutation already persisted: written if it can be.

    As :func:`_checkpoint_report` when the write succeeds. When it does not, the
    failure is reported the way :func:`_refresh_checkpoint` reports it and both
    the locator and the path come back None: the command's mutation stands, and
    its output must not claim a checkpoint it does not have.
    """
    try:
        path = svc.write_checkpoint(slug)
    except Exception as exc:  # noqa: BLE001 - see docstring; never fail the caller
        _report_unwritten_checkpoint(exc)
        return None, None
    return _checkpoint_report(root, slug, path)


def _refresh_index(root: Path, cfg: config.SpecfloConfig) -> None:
    """Best-effort: regenerate specflo-index.md after a state change (REQ-03).

    The ledger is the checkout's own, listing the projects held here, so it is
    always the local service that rewrites it.

    A no-op until a first `specflo index` has created the file - adopting the
    ledger is the user's call, not a side effect. Same failure posture as
    ``_refresh_checkpoint``: the triggering mutation already succeeded, so a
    refresh error must never fail the command.
    """
    try:
        local = local_service(root, cfg)
        if local.index_exists():
            local.write_index()
    except Exception:
        pass


# Phase/artifact registries. The phase->validator map is shared (`validators`)
# so `validate`, `advance`, and the read-path doneness derivation all agree;
# the service runs it, and `validate` consults it only to name the known
# artifacts. WARNERS and NOTES name the service reads that add non-blocking
# lines to `validate`; GATED_PHASES are the phases `advance` validates and
# completes on the way out.
WARNERS = {"plan": lambda svc, slug: svc.plan_warnings(slug)}
NOTES = {
    "plan": lambda svc, slug: svc.resolution_notes(slug),
    "execute": lambda svc, slug: svc.resolution_notes(slug),
}
GATED_PHASES = ("brainstorm", "spec", "plan")


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
    summary: str = typer.Option(
        None,
        "--summary",
        help="One-line summary written to project.md; a visible placeholder otherwise.",
    ),
    execution: str = typer.Option(
        projects.LINEAR_EXECUTION,
        "--execution",
        metavar="linear|fan-out",
        help="Execution mode recorded in project.md (default: linear).",
    ),
    remote: str = typer.Option(
        None,
        "--remote",
        metavar="<name>",
        help="Create the project on this registered daemon instead of in the checkout.",
    ),
    level: str = typer.Option(
        projects.FULL_LEVEL,
        "--level",
        metavar="|".join(projects.LEVELS),
        help="How much ceremony the project gets (default: full).",
    ),
) -> None:
    """Create project <name> and make it active."""
    root = _require_root()
    cfg = config.load_config(root)
    try:
        projects.validate_level(level)
        if remote is not None and level != projects.FULL_LEVEL:
            raise SpecfloError(
                f"Level {level!r} is local only; a project on a remote is full."
            )
        slug = projects.slugify(name)
        svc = _service_for_new_project(root, cfg, slug, remote)
        # Only a light level is passed on, so a remote never sees the key.
        extra = {} if level == projects.FULL_LEVEL else {"level": level}
        project = svc.create_project(
            name, summary=summary, execution=execution, **extra
        )
        if remote is not None:
            config.record_hosted_project(root, project.slug, remote)
        cfg.active_project = project.slug
        config.save_config(root, cfg)
    except SpecfloError as exc:
        raise _die(str(exc))
    # Scaffold the first artifact so a new project is immediately ready to work
    # (no separate `brainstorm start`). create_project stays container-only;
    # the scaffold is CLI orchestration over the idempotent helper.
    # A quick project is worked from its brief and has no brainstorm.
    if project.level == projects.QUICK_LEVEL:
        first_path, _ = svc.start_brief(project.slug)
    else:
        first_path, _ = svc.start_brainstorm(project.slug)
    _refresh_checkpoint(svc, project.slug)
    _refresh_index(root, cfg)
    typer.echo(
        f"Created project '{project.slug}' (now active). Phase: {project.phase}."
    )
    typer.echo(f"Scaffolded {_locator(project.slug, first_path)} (ready to work).")
    if summary is None:
        typer.echo(
            'No summary set - add a one-liner with `specflo summary "<what this is>"`.'
        )


def _service_for_new_project(
    root: Path, cfg: config.SpecfloConfig, slug: str, remote: str | None
) -> ProjectService:
    """The service `new` creates ``slug`` through, refusing a second locality.

    A slug lives in exactly one place: a project the hosted registry already
    maps to a remote cannot be created here, and one held in the checkout
    cannot be created again on a daemon.
    """
    hosting = config.hosting_remote(root, slug)
    if hosting is not None:
        raise SpecfloError(
            f"Project {slug!r} is hosted on remote {hosting!r}; pick another name"
            f" or `specflo switch {slug}`."
        )
    local = local_service(root, cfg)
    if remote is None:
        return local
    try:
        local.load_project(slug)
    except SpecfloError:
        return remote_service(root, remote)
    raise SpecfloError(
        f"Project {slug!r} already exists in this checkout; pick another name."
    )


@app.command(epilog='Example: specflo summary "One line on what this project does"')
def summary(
    first: str = typer.Argument(
        ...,
        metavar="[<name>] <text>",
        help="The summary text; or a project name when a second argument follows.",
    ),
    second: str = typer.Argument(None, hidden=True),
) -> None:
    """Set or update a project's one-line summary (active project by default)."""
    root = _require_root()
    cfg = config.load_config(root)
    if second is None:
        slug, text = _require_active(cfg), first
    else:
        try:
            slug, text = projects.slugify(first), second
        except SpecfloError as exc:
            raise _die(str(exc))
    svc = _service(root, cfg, slug)
    try:
        project = svc.set_summary(slug, text)
    except SpecfloError as exc:
        raise _die(str(exc))
    _refresh_index(root, cfg)
    typer.echo(f"Summary for '{project.slug}': {project.summary}")


@app.command(epilog="Example: specflo execution fan-out")
def execution(
    mode: str = typer.Argument(
        ...,
        metavar="linear|fan-out",
        help="The execution mode to record on the active project.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Switch the active project's execution mode (either direction, any phase)."""
    root = _require_root()
    cfg = config.load_config(root)
    svc = _service(root, cfg)
    slug = _require_active(cfg)
    try:
        mode, changed = svc.set_execution(slug, mode)
    except SpecfloError as exc:
        raise _die(str(exc))
    if changed:
        _refresh_checkpoint(svc, slug)
    if json_output:
        typer.echo(json.dumps({"execution": mode, "changed": changed}))
    elif changed:
        typer.echo(f"Execution mode for '{slug}': {mode}")
    else:
        typer.echo(f"Execution mode for '{slug}' unchanged: {mode}")


@app.command(epilog="Example: specflo egress local")
def egress(
    egress_class: str = typer.Argument(
        ...,
        metavar="local|no-train|open",
        help="The egress class to pin on the active project.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Pin the active project's egress class (any phase)."""
    root = _require_root()
    cfg = config.load_config(root)
    svc = _service(root, cfg)
    slug = _require_active(cfg)
    try:
        egress_class, changed = svc.set_egress(slug, egress_class)
    except SpecfloError as exc:
        raise _die(str(exc))
    if json_output:
        typer.echo(json.dumps({"egress": egress_class, "changed": changed}))
    elif changed:
        typer.echo(f"Egress class for '{slug}': {egress_class}")
    else:
        typer.echo(f"Egress class for '{slug}' unchanged: {egress_class}")


@app.command(epilog="Example: specflo level full")
def level(
    target: str = typer.Argument(
        ...,
        metavar="fast|full",
        help="The level to move the active project up to.",
    ),
) -> None:
    """Move the active project up to more ceremony; it goes back to brainstorm."""
    root = _require_root()
    cfg = config.load_config(root)
    svc = _service(root, cfg)
    slug = _require_active(cfg)
    if auto_module.in_ladder(root, cfg, slug):
        raise _die(
            f"A ladder run is live on {slug!r}: it moves the level up itself when a"
            " level completes. Continue it with `specflo auto`, or stop it with"
            " `specflo auto --off` first."
        )
    try:
        project, review = svc.set_level(slug, target)
    except SpecfloError as exc:
        raise _die(str(exc))
    _refresh_checkpoint(svc, slug)
    _refresh_index(root, cfg)
    typer.echo(f"Level for '{slug}': {project.level}. Phase: {project.phase}.")
    if review:
        typer.echo(
            "These decisions were made without an interview. Review each with the"
            " user and confirm or supersede it (`specflo decision add --supersedes"
            f" D-NN`): {', '.join(review)}."
        )


@app.command(epilog="Example: specflo index")
def index() -> None:
    """(Re)generate specflo-index.md, the ledger of every project."""
    root = _require_root()
    cfg = config.load_config(root)
    svc = local_service(root, cfg)
    # Placeholders are only ever written by the first-run backfill, so what it
    # will write is exactly the summary-less projects found before that run.
    first_run = not svc.index_exists()
    try:
        placeholdered = (
            [p.slug for p in svc.list_projects() if not p.summary]
            if first_run
            else []
        )
        path = svc.write_index()
    except SpecfloError as exc:
        raise _die(str(exc))
    count = len(svc.list_projects())
    typer.echo(
        f"Wrote {config.display_path(path, root)}"
        f" ({count} project{'' if count == 1 else 's'})."
    )
    if placeholdered:
        # REQ-11: a handoff instruction to the driving agent, not an action.
        typer.echo(
            f"Backfill wrote placeholder summaries for: {', '.join(placeholdered)}."
        )
        typer.echo(
            "Offer the user a one-time distillation pass: fill each placeholder"
            ' from that project\'s docs via `specflo summary <name> "<one line>"`.'
            " Proceed only on the user's yes."
        )


@app.command(name="list", epilog="Example: specflo list --json")
def list_(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """List all projects, marking the active one."""
    root = _require_root()
    cfg = config.load_config(root)
    local = local_service(root, cfg)
    rows = [
        {"slug": p.slug, "project": p, "locality": "local", "remote": None, "error": None}
        for p in local.list_projects()
    ]
    # A hosted project is listed from its daemon; one whose daemon cannot be
    # reached is still listed, with the reason on stderr, so the listing never
    # fails on account of one remote.
    services: dict[str, ProjectService] = {}
    for slug, remote in config.hosted_projects(root).items():
        row = {"slug": slug, "project": None, "locality": "hosted", "remote": remote, "error": None}
        try:
            if remote not in services:
                services[remote] = remote_service(root, remote, timeout=REMOTE_LIST_TIMEOUT)
            row["project"] = services[remote].load_project(slug)
        except SpecfloError as exc:
            row["error"] = str(exc)
        rows.append(row)
    rows.sort(key=lambda row: row["slug"])

    if json_output:
        entries = []
        for row in rows:
            p = row["project"]
            entry = {
                "slug": row["slug"],
                "name": p.name if p else None,
                "phase": p.phase if p else None,
                "status": p.status if p else None,
                "active": row["slug"] == cfg.active_project,
                "locality": row["locality"],
                "remote": row["remote"],
            }
            if row["error"]:
                entry["error"] = row["error"]
            entries.append(entry)
        typer.echo(json.dumps({"active_project": cfg.active_project, "projects": entries}))
        return

    if not rows:
        typer.echo("No projects yet. `specflo new <name>` starts the first.")
        return

    for row in rows:
        marker = "*" if row["slug"] == cfg.active_project else " "
        p = row["project"]
        if p is None:
            typer.secho(f"note: {row['error']}", fg=typer.colors.YELLOW, err=True)
            typer.echo(f"{marker} {row['slug']}  (?)  [hosted: {row['remote']}, unreachable]")
            continue
        if p.status == projects.COMPLETE_STATUS:
            suffix = "  [complete]"
        elif p.status == projects.SHELVED_STATUS:
            suffix = "  [shelved]"
            if p.shelved_reason:
                suffix += f": {p.shelved_reason}"
        else:
            suffix = ""
        if row["locality"] == "hosted":
            suffix += f"  [hosted: {row['remote']}]"
        typer.echo(f"{marker} {p.slug}  ({p.phase}){suffix}")
    rule = local.index_rule_line()
    if rule:
        typer.echo("")
        typer.echo(rule)


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
        slug = projects.slugify(name)
    except SpecfloError as exc:
        raise _die(str(exc))
    svc = _service(root, cfg, slug)
    try:
        project = svc.load_project(slug)
    except SpecfloError as exc:
        # A daemon's refusal (unreachable, token refused) is the reason; a
        # project missing from this checkout is just not here.
        if config.hosting_remote(root, slug) is not None:
            raise _die(str(exc))
        raise _die(
            f"No project {slug!r}. Run `specflo list` to see available projects."
        )
    cfg.active_project = project.slug
    try:
        config.save_config(root, cfg)
    except SpecfloError as exc:
        raise _die(str(exc))
    typer.echo(f"Switched to '{project.slug}' (phase: {project.phase}).")


@app.command(epilog="Example: specflo leave")
def leave(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Leave the active project: clear the pointer, change no project.

    The project itself is untouched (status, phase, files). Re-enter it later
    with `specflo switch <name>` (or `specflo resume <name>` if shelved).
    Safe to run with no active project.
    """
    root = _require_root()
    cfg = config.load_config(root)
    left = cfg.active_project
    if left is not None:
        cfg.active_project = None
        config.save_config(root, cfg)
    if json_output:
        typer.echo(json.dumps({"left": left, "active_project": None}))
    elif left is None:
        typer.echo("No active project.")
    else:
        typer.echo(f"Left {left}.")


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
    svc = _service(root, cfg, slug)
    try:
        existing = svc.load_project(slug)
    except SpecfloError as exc:
        raise _die(str(exc))
    if existing.status == projects.COMPLETE_STATUS:
        raise _die(f"Project '{slug}' is complete (terminal) - cannot shelve it.")
    try:
        project = svc.shelve_project(slug, reason=reason)
    except SpecfloError as exc:
        raise _die(str(exc))
    _refresh_checkpoint(svc, slug)
    _refresh_index(root, cfg)
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
    svc = _service(root, cfg, slug)
    try:
        existing = svc.load_project(slug)
    except SpecfloError as exc:
        raise _die(str(exc))
    if existing.status != projects.SHELVED_STATUS:
        raise _die(f"Project '{slug}' is not shelved - nothing to resume.")
    try:
        project = svc.resume_project(slug)
        cfg.active_project = project.slug
        config.save_config(root, cfg)
    except SpecfloError as exc:
        raise _die(str(exc))
    _refresh_checkpoint(svc, slug)
    _refresh_index(root, cfg)
    if json_output:
        typer.echo(json.dumps({"slug": project.slug, "status": project.status}))
    else:
        typer.echo(f"Resumed '{project.slug}' (phase: {project.phase}). Now active.")


@app.command(epilog="Example: specflo status --json")
def status(
    ctx: typer.Context,
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
            typer.echo(guide_module.NO_ACTIVE_PROJECT_MESSAGE)
        return

    svc = _service(root, cfg)
    try:
        info = _client_status(svc, root, cfg.active_project)
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

    override = _directory_override(ctx)
    if override is not None:
        info["root"] = str(root)
        info["directory_source"] = override["directory_source"]
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
    if action == "none":
        first, second = guide_module.NO_ACTIVE_PROJECT_LINES
        return [first, f"  {second}"]
    return [
        f"Project '{data['active_project']}' | phase: {data['phase']}",
        f"  Next: {data['next_step']}",
    ]


def _render_commands(data: dict) -> list[str]:
    groups = [
        ("setup", "Setup & navigation"),
        ("workflow", "Workflow"),
        ("agents", "Agents"),
    ]
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
        "Paste this near the top of your agent memory file (CLAUDE.md / AGENTS.md)\n"
        "once, so a fresh agent always knows specflo is here:",
        rule,
        data["memory_snippet"],
        rule,
        "",
        f"Pipeline:  {_render_pipeline(data)}",
        "",
        data["levels"],
        "",
        "You are here:",
        *(f"  {line}" for line in _render_you_are_here(data)),
        *(["", data["rule"]] if data.get("rule") else []),
        "",
        "Commands:",
        *_render_commands(data),
        "",
        "Skills:  in a skill-capable harness the `specflo-brainstorm`, `specflo-spec`, "
        "`specflo-plan`, and `specflo-execute` skills drive\n  the conversation; these "
        "commands are the seam they call.",
    ]
    typer.echo("\n".join(lines))


@app.command(name="checkpoint", epilog="Example: specflo checkpoint --json")
def checkpoint_(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Print the resume prompt (and refresh checkpoint.md) for the active project."""
    root = _require_root()
    cfg = config.load_config(root)
    svc = _service(root, cfg)
    slug = _require_active(cfg)
    try:
        payload = _client_checkpoint(svc, root, slug)
        svc.write_checkpoint(slug)
    except SpecfloError as exc:
        raise _die(str(exc))
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
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit the pass as JSON: its payload text, a boolean stop, and the "
        "stop reason (kill-switch, pass-cap, stall, project-complete, "
        "ladder-blocked, or "
        "unavailable; null while the run continues).",
    ),
    ladder: bool = typer.Option(
        False,
        "--ladder",
        help="Start a ladder run on a quick project: it climbs quick, fast and full "
        "on stacked local branches (specflo/<slug>/<level>) and records each in "
        "ladder.md. Later passes continue it without the flag.",
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
    if ladder and (off or on):
        raise _die("--ladder starts a run; it cannot be combined with --off/--on.")
    if json_output and (off or on):
        # The toggles run no pass, so there is no pass result to report.
        raise _die("--json reports a pass; it cannot be combined with --off/--on.")
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
    if ladder:
        try:
            auto_module.start_ladder()
        except SpecfloError as exc:
            raise _die(str(exc))
    result = auto_module.auto_pass_result(autonomy=autonomy, max_passes=max_passes)
    if json_output:
        typer.echo(json.dumps(result))
        return
    if result["payload"]:
        typer.echo(result["payload"])


@hook_app.command(
    "reseed",
    epilog="Wired into a SessionStart hook by `specflo hook install`.",
)
def hook_reseed(
    ctx: typer.Context,
    output_format: str = typer.Option(
        "text",
        "--format",
        help="'text' (portable payload) or 'claude' (SessionStart JSON: agent "
        "context + a user-visible nudge).",
    ),
    direct: bool = typer.Option(
        False,
        "--continue",
        help="Emit the direct-continuation payload: carry out the next step now, "
        "no confirmation gate. For a caller that cleared context on purpose.",
    ),
) -> None:
    """Emit the clear-and-continue reseed payload for the active project.

    Default (`text`) prints the confirmation-gate directive + the verbatim
    checkpoint - portable across harnesses. `--continue` swaps that directive for
    an imperative "carry out the next step now": the ask-first gate exists because
    a cold-start hook cannot know whether the human wants to keep going, and a
    caller that just cleared context on purpose has already answered that.
    `--format claude` emits Claude Code SessionStart JSON: the same payload as
    `additionalContext` plus a visible `systemMessage` telling the user what to
    type. Either way, prints nothing when there is no active project, or when the
    active one is complete or shelved (nothing to resume). Always exits
    0 (bar an invalid flag combination), reads no stdin, makes no network calls -
    safe to wire into SessionStart unconditionally.
    """
    if direct and output_format == "claude":
        # The Claude wrapper is the cold-start surface: its visible nudge asks the
        # user to type `continue`, which contradicts a payload that just told the
        # agent to start. Refuse rather than silently drop one of the two flags.
        raise _die("--continue is not supported with --format claude.")
    override = _directory_override(ctx)
    source = override["directory_source"] if override else None
    out = (
        hook.claude_session_start_output(directory_source=source)
        if output_format == "claude"
        else hook.reseed_text(direct=direct, directory_source=source)
    )
    if out:
        typer.echo(out)


@extension_app.command(
    "install",
    epilog="Example: specflo extension install --scope user",
)
def extension_install_cmd(
    scope: str = typer.Option(
        "user",
        "--scope",
        help="'user' (~/.pi/agent/extensions) or 'project' (<cwd>/.pi/extensions).",
    ),
) -> None:
    """Copy the bundled pi extension into pi's extension directory.

    pi discovers both directories on its own, so this writes no pi settings and
    edits no configuration. Purely a local copy plus a provenance stamp naming
    the specflo version that produced it - no network, no package manager, no
    registry. Re-running is safe: an identical install is left alone, a differing
    one is replaced whole.
    """
    try:
        installed = extension_module.install_extension(scope=scope)
    except extension_module.ExtensionInstallError as exc:
        raise _die(str(exc))
    verb = {
        "installed": "Installed",
        "updated": "Updated",
        "current": "Already current:",
    }[installed.state]
    typer.echo(f"{verb} specflo extension {installed.version} -> {installed.path}")


@hook_app.command(
    "install",
    epilog="Example: specflo hook install",
)
def hook_install() -> None:
    """Wire `hook reseed` into Claude Code's .claude/settings.json (idempotent merge)."""
    root = _require_root()
    path = hook.install_hook(root)
    typer.echo(f"Installed SessionStart hook -> {config.display_path(path, root)}")


@hook_app.command(
    "print",
    epilog="Example: specflo hook print",
)
def hook_print(
    install: bool = typer.Option(
        False,
        "--install",
        hidden=True,
        help="Deprecated alias for `specflo hook install`.",
    ),
) -> None:
    """Print the Claude Code SessionStart wiring for `hook reseed` (a fragment to merge)."""
    if install:
        hook_install()
        return
    typer.echo(json.dumps(hook.settings_snippet(), indent=2))
    typer.echo(
        "Claude Code SessionStart wiring - a fragment to merge into "
        ".claude/settings.json; run `specflo hook install` to merge it safely "
        "(idempotent, preserves existing content).\n"
        "Other agents/harnesses can adapt this wiring; pi needs none - the "
        "specflo pi extension reseeds on its own.",
        err=True,
    )


@brainstorm_app.command("start", epilog="Example: specflo brainstorm start")
def brainstorm_start(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Create (or locate) the active project's brainstorm.md."""
    root = _require_root()
    cfg = config.load_config(root)
    svc = _service(root, cfg)
    slug = _require_active(cfg)
    try:
        path, created = svc.start_brainstorm(slug)
    except SpecfloError as exc:
        raise _die(str(exc))
    _refresh_checkpoint(svc, slug)
    locator, reported = _artifact_report(root, slug, path)
    if json_output:
        typer.echo(json.dumps({"locator": locator, "path": reported, "created": created}))
    else:
        note = "" if created else " (already started)"
        typer.echo(f"{locator}{note}")


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
    svc = _service(root, cfg)
    slug = _require_active(cfg)
    try:
        decision = svc.add_decision(
            slug, text, rationale=rationale, supersedes=supersedes
        )
    except SpecfloError as exc:
        raise _die(str(exc))
    _refresh_checkpoint(svc, slug)
    if json_output:
        typer.echo(json.dumps({"id": decision.id, "supersedes": decision.supersedes}))
    else:
        message = f"Recorded {decision.id}."
        if decision.supersedes:
            message += f" Supersedes {decision.supersedes}."
        typer.echo(message)


@gate_app.command(
    "open",
    epilog='Example: specflo gate open developer --note "Open points: the name, pricing"',
)
def gate_open(
    role: str = typer.Argument(
        ..., metavar="<requester|developer>", help="The role the project now waits on."
    ),
    note: str = typer.Option(
        "", "--note", help="What the taker should know: the open points (one line)."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Open a gate on the active project: it waits on <role> until someone takes it.

    The gate records the role, who opened it, when, and the note; the
    project's status stays active. Only one gate is open at a time.
    """
    root = _require_root()
    cfg = config.load_config(root)
    slug = _require_active(cfg)
    svc = _service(root, cfg, slug)
    try:
        project = svc.open_gate(slug, role, note=note)
    except SpecfloError as exc:
        raise _die(str(exc))
    _refresh_checkpoint(svc, slug)
    gate = project.gate
    if json_output:
        typer.echo(json.dumps({"slug": project.slug, "gate": dataclasses.asdict(gate)}))
    else:
        message = f"Opened a gate for {gate.role} on '{project.slug}'."
        if gate.note:
            message += f" Note: {gate.note}."
        typer.echo(message)


@gate_app.command("take", epilog="Example: specflo gate take")
def gate_take(
    by: str = typer.Option(
        None,
        "--by",
        metavar="<requester|developer>",
        help="The human this take is relayed for; only the agent identity may pass it.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Take the active project's open gate: records who took it and when."""
    root = _require_root()
    cfg = config.load_config(root)
    slug = _require_active(cfg)
    svc = _service(root, cfg, slug)
    try:
        project = svc.take_gate(slug, by=by)
    except SpecfloError as exc:
        raise _die(str(exc))
    _refresh_checkpoint(svc, slug)
    gate = project.gate
    if json_output:
        typer.echo(json.dumps({"slug": project.slug, "gate": dataclasses.asdict(gate)}))
    else:
        typer.echo(f"Took the gate for {gate.role} on '{project.slug}'.")


@app.command(epilog="Example: specflo validate spec")
def validate(
    artifact: str = typer.Argument(
        ..., metavar="<artifact>",
        help="Artifact/phase to validate: brainstorm, spec, plan, execute, or brief."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Lint an artifact; reports readiness and any issues."""
    root = _require_root()
    cfg = config.load_config(root)
    svc = _service(root, cfg)
    slug = _require_active(cfg)
    if artifact not in VALIDATORS:
        known = ", ".join(sorted(VALIDATORS))
        raise _die(f"Unknown artifact {artifact!r}. Known: {known}.")
    issues = svc.validate_artifact(slug, artifact)
    warner = WARNERS.get(artifact)
    warnings = warner(svc, slug) if warner is not None else []
    noter = NOTES.get(artifact)
    notes = noter(svc, slug) if noter is not None else []
    if json_output:
        payload = {"ready": not issues, "issues": issues}
        if warner is not None:
            payload["warnings"] = warnings
        if noter is not None:
            payload["notes"] = notes
        typer.echo(json.dumps(payload))
        raise typer.Exit(code=0 if not issues else 1)
    if warnings:
        typer.secho(f"{artifact} warnings:", fg=typer.colors.YELLOW, err=True)
        for w in warnings:
            typer.echo(f"  - {w}", err=True)
    if notes:
        typer.secho(f"{artifact} notes:", fg=typer.colors.CYAN, err=True)
        for n in notes:
            typer.echo(f"  - {n}", err=True)
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
    svc = _service(root, cfg)
    slug = _require_active(cfg)
    try:
        project = svc.load_project(slug)
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
        issues = svc.validate_artifact(slug, from_phase)
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
        updated = svc.complete_project(slug)
        svc.stamp_banners(slug)
        _refresh_index(root, cfg)
        cp_locator, cp_path = _checkpoint_after(svc, root, slug)
        # Terminal continuation: a clear-point with no continue-instruction and
        # neither resume command named (REQ-07). Rendered once so the JSON field
        # and the prose carry the identical text (REQ-12).
        ladder_next = auto_module.ladder_step(root, cfg, updated)
        climbing = ladder_next is not None
        if climbing:
            auto_module.mark_level_end(root, cfg, updated)
            cont = continuation.build_level_end(ladder_next)
        else:
            cont = continuation.build_continuation(
                from_phase, workflow.next_step(from_phase, complete=True), complete=True
            )
        if json_output:
            typer.echo(json.dumps(
                {"advanced": True, "from": from_phase, "to": None,
                 "complete": True, "checkpoint": cp_path,
                 "checkpoint_locator": cp_locator, "continuation": cont}))
        else:
            typer.echo(f"Completed project '{slug}'.")
            if not climbing:
                typer.echo(
                    'Revise the summary to describe what shipped:'
                    ' `specflo summary "<one line>"`.'
                )
            if cp_locator is not None:
                typer.echo(f"Checkpoint saved: {cp_locator}")
            typer.echo(cont)
        return

    # Non-terminal: gate the leaving artifact, complete it, then bump the phase.
    if from_phase in GATED_PHASES:
        issues = svc.validate_artifact(slug, from_phase)
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
        svc.complete_artifact(slug, from_phase)

    try:
        updated = svc.advance_project(slug)
    except SpecfloError as exc:
        raise _die(str(exc))

    _refresh_index(root, cfg)
    cp_locator, cp_path = _checkpoint_after(svc, root, slug)

    # Progress-aware next step for the phase we just entered (e.g. advancing into
    # execute names the first actionable task). Non-task targets keep the static
    # hint (progress stays None), so other advances are unchanged.
    progress = None
    if updated.phase in ("plan", "execute") and svc.has_artifact(slug, "plan"):
        progress = svc.plan_progress(slug)
    next_step = workflow.next_step(updated.phase, progress=progress, level=updated.level)
    # The shared continuation: the entered phase's next action, the phase skill
    # carrying it, and the clear-point naming both resume paths. Rendered once so
    # the JSON field and the prose carry the identical text (REQ-12).
    cont = continuation.build_continuation(updated.phase, next_step)

    if json_output:
        typer.echo(json.dumps(
            {"advanced": True, "from": from_phase, "to": updated.phase,
             "next_step": next_step, "checkpoint": cp_path,
             "checkpoint_locator": cp_locator, "continuation": cont}))
    else:
        typer.echo(f"Advanced '{slug}' from {from_phase} to {updated.phase}.")
        if cp_locator is not None:
            typer.echo(f"Checkpoint saved: {cp_locator}")
        typer.echo(cont)


# The file each phase produces; execute has no artifact of its own (its work
# lives in plan.md). Used by `reopen` to flag downstream files that may now be
# stale after moving the pointer back.
_PHASE_ARTIFACT = {
    "brainstorm": brainstorm.BRAINSTORM_FILENAME,
    "spec": spec.SPEC_FILENAME,
    "plan": plan.PLAN_FILENAME,
}


def _stale_downstream_artifacts(svc: ProjectService, project: projects.Project) -> list[str]:
    """Names of artifacts for phases *after* ``project.phase`` the project has created.

    A pure read of what already exists beyond the reopened phase, so `reopen` can
    warn which files may now be stale without touching any of them (REQ-10).
    """
    downstream = workflow.PHASES[workflow.PHASES.index(project.phase) + 1:]
    return [
        _PHASE_ARTIFACT[ph]
        for ph in downstream
        if ph in _PHASE_ARTIFACT and svc.has_artifact(project.slug, ph)
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
    svc = _service(root, cfg)
    slug = _require_active(cfg)
    try:
        project = svc.load_project(slug)
    except SpecfloError as exc:
        raise _die(str(exc))

    if project.status == projects.SHELVED_STATUS:
        raise _die(
            f"Project '{slug}' is shelved - run `specflo resume` first, then reopen."
        )

    from_phase = project.phase
    try:
        updated = svc.reopen_project(slug, phase)
    except SpecfloError as exc:
        raise _die(str(exc))

    stale = _stale_downstream_artifacts(svc, updated)
    cp_locator, cp_path = _checkpoint_after(svc, root, slug)
    if json_output:
        typer.echo(json.dumps(
            {"reopened": True, "from": from_phase, "to": updated.phase,
             "stale": stale, "checkpoint": cp_path, "checkpoint_locator": cp_locator}))
    else:
        typer.echo(f"Reopened '{slug}' from {from_phase} to {updated.phase}.")
        if stale:
            typer.echo("Possibly stale downstream artifacts (unchanged on disk):")
            for name in stale:
                typer.echo(f"  - {name}")
        if cp_locator is not None:
            typer.echo(f"Checkpoint saved: {cp_locator}")
        # Reopening is a clear-point too, and has been since before the shared
        # builder existed. It routes through the builder so no seam keeps its own
        # copy of the wording (REQ-04, D-06). The clear-point stays unconditional,
        # as it was before that rewiring: if the next step can't be derived, fall
        # back to the clear-point alone rather than emitting nothing (REQ-10).
        cont = _seam_continuation(svc, slug, root)
        typer.echo(cont["continuation"] or continuation.clear_point_only())


@spec_app.command("start", epilog="Example: specflo spec start")
def spec_start(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Create (or locate) the active project's spec.md."""
    root = _require_root()
    cfg = config.load_config(root)
    svc = _service(root, cfg)
    slug = _require_active(cfg)
    try:
        path, created = svc.start_spec(slug)
    except SpecfloError as exc:
        raise _die(str(exc))
    _refresh_checkpoint(svc, slug)
    locator, reported = _artifact_report(root, slug, path)
    if json_output:
        typer.echo(json.dumps({"locator": locator, "path": reported, "created": created}))
    else:
        note = "" if created else " (already started)"
        typer.echo(f"{locator}{note}")


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
    svc = _service(root, cfg)
    slug = _require_active(cfg)
    try:
        requirement = svc.add_requirement(
            slug, text, acceptance, derives_from=from_, supersedes=supersedes
        )
    except SpecfloError as exc:
        raise _die(str(exc))
    _refresh_checkpoint(svc, slug)
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
    svc = _service(root, cfg)
    slug = _require_active(cfg)
    try:
        path, created = svc.start_plan(slug)
    except SpecfloError as exc:
        raise _die(str(exc))
    _refresh_checkpoint(svc, slug)
    locator, reported = _artifact_report(root, slug, path)
    if json_output:
        typer.echo(json.dumps({"locator": locator, "path": reported, "created": created}))
    else:
        note = "" if created else " (already started)"
        typer.echo(f"{locator}{note}")


@plan_app.command("graph", epilog="Example: specflo plan graph --json")
def plan_graph(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Render the plan's execution graph: waves, one line per active task, and a mermaid block."""
    root = _require_root(); cfg = config.load_config(root); slug = _require_active(cfg)
    svc = _service(root, cfg)
    try:
        data = svc.execution_graph(slug)
    except SpecfloError as exc:
        raise _die(str(exc))
    tasks, milestones = data["tasks"], data["milestones"]
    if json_output:
        typer.echo(json.dumps(graph_module.payload(tasks, milestones)))
        return
    lines = []
    for n, ids in enumerate(graph_module.waves(tasks)):
        lines.append(f"Wave {n}: {', '.join(ids)}")
    if lines:
        lines.append("")
    for t in tasks:
        line = f"{t.id}  {t.text}  [{t.progress}]"
        if t.file_list:
            line += f"  files: {', '.join(t.file_list)}"
        if t.needs:
            line += f"  needs: {', '.join(t.needs)}"
        lines.append(line)
    if not tasks:
        lines.append("No tasks yet. Add one with `specflo task add`.")
    lines += ["", graph_module.mermaid(tasks, milestones)]
    typer.echo("\n".join(lines))


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
    needs: list[str] = typer.Option(
        None, "--needs", metavar="<pool>",
        help="A resource pool this task needs (repeatable; e.g. gpu:3090).",
    ),
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
    svc = _service(root, cfg)
    slug = _require_active(cfg)
    try:
        task = svc.add_task(
            slug, text, acceptance, verify, list(from_),
            depends_on=list(depends_on or []), files=files, scope=scope,
            supersedes=supersedes, milestone=milestone, needs=list(needs or []),
        )
    except SpecfloError as exc:
        raise _die(str(exc))
    _refresh_checkpoint(svc, slug)
    # Detect-and-offer: when this task supersedes another, surface any active
    # tasks still depending on the superseded one and the literal rewire command
    # to repoint them. We never modify those dependents here (that is `task rewire`).
    dependents = (
        svc.active_dependents(slug, task.supersedes) if task.supersedes else []
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
    svc = _service(root, cfg)
    try:
        changed = svc.rewire_dependency(slug, from_, to)
    except SpecfloError as exc:
        raise _die(str(exc))
    _refresh_checkpoint(svc, slug)
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
    svc = _service(root, cfg)
    try:
        task = svc.set_milestone(slug, task_id, milestone_id)
    except SpecfloError as exc:
        raise _die(str(exc))
    _refresh_checkpoint(svc, slug)
    if json_output:
        typer.echo(json.dumps({"id": task.id, "milestone": task.milestone}))
    else:
        typer.echo(f"{task.id} -> milestone {task.milestone}")


@task_app.command(
    "edit",
    epilog='Example: specflo task edit T-01 --acceptance "it works twice"',
)
def task_edit(
    task_id: str = typer.Argument(..., metavar="<T-NN>", help="Task to edit."),
    title: str = typer.Option(None, "--title", help="Rewrite the task title."),
    acceptance: str = typer.Option(None, "--acceptance", help="Rewrite Acceptance."),
    verify: str = typer.Option(None, "--verify", help="Rewrite Verify."),
    scope: str = typer.Option(None, "--scope", help="Rewrite Scope."),
    files: str = typer.Option(None, "--files", help="Rewrite Files (comma-separated)."),
    needs: str = typer.Option(None, "--needs", help="Rewrite Needs (comma-separated pools)."),
    implements: str = typer.Option(
        None, "--implements", metavar="REQ-NN[,REQ-MM]", help="Rewrite Implements."
    ),
    add_depends_on: list[str] = typer.Option(
        None, "--add-depends-on", metavar="T-NN", help="Add a dependency (repeatable)."
    ),
    drop_depends_on: list[str] = typer.Option(
        None, "--drop-depends-on", metavar="T-NN", help="Drop a dependency (repeatable)."
    ),
    force: bool = typer.Option(
        False, "--force",
        help="Edit a done task anyway, recording an [Edit] note of the old value."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Edit an active task's fields and dependencies in place."""
    root = _require_root(); cfg = config.load_config(root); slug = _require_active(cfg)
    svc = _service(root, cfg)
    try:
        tid, changed = svc.edit_task(
            slug, task_id, title=title, acceptance=acceptance,
            verify=verify, scope=scope, files=files, needs=needs,
            implements=implements, add_depends_on=list(add_depends_on or []),
            drop_depends_on=list(drop_depends_on or []), force=force,
        )
    except SpecfloError as exc:
        raise _die(str(exc))
    _refresh_checkpoint(svc, slug)
    if json_output:
        typer.echo(json.dumps({"id": tid, "changed": changed}))
    elif changed:
        typer.echo(f"{tid} edited: {', '.join(changed)}")
    else:
        typer.echo(f"{tid} unchanged: every field already held the given value.")


@task_app.command(
    "note",
    epilog='Example: specflo task note T-01 --text "why we changed course" --label Design',
)
def task_note(
    task_id: str = typer.Argument(..., metavar="<T-NN>", help="Task to annotate."),
    text: str = typer.Option(..., "--text", help="The note text (written as one line)."),
    label: str = typer.Option(
        None, "--label",
        help="One of Note (default), Design, Resolution, Descoped. "
             "Edit is reserved for `task edit --force`.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Append a dated note to a task, in any progress state."""
    root = _require_root(); cfg = config.load_config(root); slug = _require_active(cfg)
    svc = _service(root, cfg)
    try:
        note = svc.add_note(slug, task_id, text, label=label)
    except SpecfloError as exc:
        raise _die(str(exc))
    _refresh_checkpoint(svc, slug)
    if json_output:
        typer.echo(json.dumps({"id": task_id, "note": note}))
    else:
        typer.echo(f"{task_id} note: {note['date']} [{note['label']}] {note['text']}")


# The continuation fields a seam contributes to its `--json`. Always present, so
# a harness can rely on the keys existing even when the values could not be
# derived (REQ-12); `None` says "underivable", never "absent".
_CONTINUATION_KEYS = ("next_step", "checkpoint", "checkpoint_locator", "continuation")


def _seam_continuation(svc: ProjectService, slug: str, root: Path) -> dict:
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
        payload = _client_checkpoint(svc, root, slug)
        # Inside the guard: reading the payload and rendering must be covered too,
        # or the "never fail the caller" guarantee would be narrower than stated.
        return {
            "next_step": payload["do_next"],
            "checkpoint": payload["path"],
            "checkpoint_locator": payload["locator"],
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
    svc = _service(root, cfg)
    try:
        task = svc.start_task(slug, task_id)
    except SpecfloError as exc:
        raise _die(str(exc))
    _refresh_checkpoint(svc, slug)
    _report_transition(task, json_output)


@task_app.command("done", epilog="Example: specflo task done T-01")
def task_done(
    task_id: str = typer.Argument(..., metavar="<T-NN>", help="Task to mark done."),
    note: str = typer.Option(
        None, "--note", help="Record a note on the task in the same write."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Mark a task done."""
    root = _require_root(); cfg = config.load_config(root); slug = _require_active(cfg)
    svc = _service(root, cfg)
    try:
        task = svc.done_task(slug, task_id, note=note)
    except SpecfloError as exc:
        raise _die(str(exc))
    written = _refresh_checkpoint(svc, slug)
    # Unlike the other task verbs, completing a task is a clear-point: it gets the
    # full continuation (REQ-01). start/block/reopen stay terse by design.
    cont = _seam_continuation(svc, slug, root)
    if not written:
        # The continuation derives from the project's state, not from the file,
        # so it stands; the checkpoint location does not.
        cont = {**cont, "checkpoint": None, "checkpoint_locator": None}
    _report_transition(task, json_output, extra=cont)
    if not json_output:
        if cont["continuation"] is None:
            # Same fallback reopen takes: a seam that is a clear-point stays one
            # even when the next step is underivable, so a harness grepping the
            # prose for the marker never silently stops resuming (REQ-01).
            typer.echo(continuation.clear_point_only())
        else:
            if written:
                typer.echo(f"Checkpoint saved: {cont['checkpoint_locator']}")
            typer.echo(cont["continuation"])


@task_app.command("block", epilog='Example: specflo task block T-01 --reason "waiting on API"')
def task_block(
    task_id: str = typer.Argument(..., metavar="<T-NN>", help="Task to block."),
    reason: str = typer.Option(None, "--reason", help="Why it's blocked."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Mark a task blocked (optionally recording a reason)."""
    root = _require_root(); cfg = config.load_config(root); slug = _require_active(cfg)
    svc = _service(root, cfg)
    try:
        task = svc.block_task(slug, task_id, reason=reason)
    except SpecfloError as exc:
        raise _die(str(exc))
    _refresh_checkpoint(svc, slug)
    _report_transition(task, json_output)


@task_app.command("reopen", epilog="Example: specflo task reopen T-01")
def task_reopen(
    task_id: str = typer.Argument(..., metavar="<T-NN>", help="Task to reopen (back to pending)."),
    note: str = typer.Option(
        None, "--note", help="Record a note on the task in the same write."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Return a task to pending (clears any block)."""
    root = _require_root(); cfg = config.load_config(root); slug = _require_active(cfg)
    svc = _service(root, cfg)
    try:
        task = svc.reopen_task(slug, task_id, note=note)
    except SpecfloError as exc:
        raise _die(str(exc))
    _refresh_checkpoint(svc, slug)
    _report_transition(task, json_output)


@task_app.command("list", epilog="Example: specflo task list")
def task_list(
    all_: bool = typer.Option(False, "--all", help="Include superseded tasks."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """List tasks with progress and the deps-aware next-actionable marker."""
    root = _require_root(); cfg = config.load_config(root); slug = _require_active(cfg)
    svc = _service(root, cfg)
    try:
        tasks = svc.list_tasks(slug, include_superseded=all_)
        progress = svc.plan_progress(slug)
    except SpecfloError as exc:
        raise _die(str(exc))
    nexts = set(progress["next_actionable"])
    if json_output:
        # The orchestrator frontier (fan-out-plans REQ-10): files, needs and
        # ready per task plus the pools map, alongside the unchanged keys.
        fr = svc.frontier(slug)
        ready = {t["id"] for t in fr["tasks"] if t["ready"]}
        typer.echo(json.dumps({
            "tasks": [
                {"id": t.id, "text": t.text, "progress": t.progress, "status": t.status,
                 "implements": t.implements, "depends_on": t.depends_on, "next": t.id in nexts,
                 "files": t.file_list, "needs": t.needs, "ready": t.id in ready}
                for t in tasks
            ],
            "progress": progress,
            "pools": fr["pools"],
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
    svc = _service(root, cfg)
    try:
        brief = svc.task_brief(slug, task_id)
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
    out = plan.render_task_brief(brief)
    # Surface the soft milestone-boundary verify beat (the just-completed
    # milestone's Exit checklist) after the brief, when at a boundary (REQ-14).
    if brief.get("boundary"):
        out += "\n\n" + "\n".join(plan.boundary_beat_lines(brief["boundary"]))
    typer.echo(out)


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
    svc = _service(root, cfg)
    try:
        milestone = svc.add_milestone(slug, text, list(exit_))
    except SpecfloError as exc:
        raise _die(str(exc))
    _refresh_checkpoint(svc, slug)
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
    svc = _service(root, cfg)
    view = svc.milestone_progress(slug)
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


@pool_app.command("add", epilog="Example: specflo pool add gpu:3090 --size 2")
def pool_add(
    name: str = typer.Argument(..., metavar="<name>", help="Pool name (e.g. gpu:3090)."),
    size: int = typer.Option(..., "--size", metavar="N", help="Number of slots (>= 1)."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Declare a pool of N slots in plan.md's '## Pools' section, or resize it."""
    root = _require_root(); cfg = config.load_config(root); slug = _require_active(cfg)
    svc = _service(root, cfg)
    try:
        name, size = svc.add_pool(slug, name, size)
    except SpecfloError as exc:
        raise _die(str(exc))
    if json_output:
        typer.echo(json.dumps({"name": name, "size": size}))
    else:
        typer.echo(f"Pool {name}: {size} slot(s).")


@pool_app.command("list", epilog="Example: specflo pool list --json")
def pool_list(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """List every declared pool and every pool an active task needs, with sizes."""
    root = _require_root(); cfg = config.load_config(root); slug = _require_active(cfg)
    svc = _service(root, cfg)
    try:
        pools = svc.list_pools(slug)
    except SpecfloError as exc:
        raise _die(str(exc))
    if json_output:
        typer.echo(json.dumps(pools))
        return
    if not pools:
        typer.echo("No pools. Declare one with `specflo pool add <name> --size N`.")
        return
    for name, size in pools.items():
        typer.echo(f"{name}: {size}")


@milestone_app.command("show", epilog="Example: specflo milestone show M-01")
def milestone_show(
    milestone_id: str = typer.Argument(..., metavar="<M-NN>", help="Milestone to show."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Show a milestone: its Exit checklist, member tasks, rollup, and derived REQ set."""
    root = _require_root(); cfg = config.load_config(root); slug = _require_active(cfg)
    svc = _service(root, cfg)
    detail = svc.milestone_detail(slug, milestone_id)
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


@review_app.command("start", epilog="Example: specflo review start")
def review_start(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Mint the active project's next review round and print its locator."""
    root = _require_root(); cfg = config.load_config(root); slug = _require_active(cfg)
    svc = _service(root, cfg)
    try:
        path, created = svc.start_round(slug)
    except SpecfloError as exc:
        raise _die(str(exc))
    _refresh_checkpoint(svc, slug)
    locator, reported = _artifact_report(root, slug, path)
    if json_output:
        typer.echo(json.dumps({"locator": locator, "path": reported, "created": created}))
    else:
        note = "" if created else " (already open)"
        typer.echo(f"{locator}{note}")


@review_app.command(
    "done",
    epilog="Example: specflo review done --verdict ready-to-merge",
)
def review_done(
    verdict: str = typer.Option(
        ..., "--verdict", metavar="<v>",
        help="ready-to-merge | changes-requested | waived.",
    ),
    reason: str = typer.Option(
        None, "--reason", help="Why the review was waived (waived only)."
    ),
    file: str = typer.Option(
        None, "--file", metavar="<path>",
        help="A report file whose text becomes the round's body.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Close the active project's open review round with a verdict."""
    root = _require_root(); cfg = config.load_config(root); slug = _require_active(cfg)
    svc = _service(root, cfg)
    # The report is read here, on the client: only its text reaches the
    # service, so a hosted round ingests a file of this checkout and never
    # one on the daemon host.
    report_text = None
    if file is not None:
        try:
            report_text = Path(file).read_text()
        except FileNotFoundError:
            raise _die(f"No report file at {file}.")
        except (OSError, UnicodeDecodeError) as exc:
            raise _die(f"Cannot read {file} as text: {exc}")
    try:
        path = svc.close_round(slug, verdict, reason=reason, report_text=report_text)
    except SpecfloError as exc:
        raise _die(str(exc))
    _refresh_checkpoint(svc, slug)
    locator, reported = _artifact_report(root, slug, path)
    if json_output:
        typer.echo(json.dumps({"locator": locator, "path": reported, "verdict": verdict}))
    else:
        typer.echo(f"{locator} closed {verdict}")


@doc_app.command("show", epilog="Example: specflo doc show brainstorm")
def doc_show(
    artifact: str = typer.Argument(
        ..., metavar="<artifact>",
        help="One of: " + doc_module.artifact_names() + ".",
    ),
) -> None:
    """Print the named artifact of the active project verbatim."""
    root = _require_root(); cfg = config.load_config(root); slug = _require_active(cfg)
    svc = _service(root, cfg)
    try:
        text = svc.show_document(slug, artifact)
    except SpecfloError as exc:
        raise _die(str(exc))
    # Verbatim: the document's own bytes, no added trailing newline.
    typer.echo(text, nl=False)


@section_app.command(
    "set",
    epilog='Example: specflo section set brainstorm "Current understanding" --file synthesis.md',
)
def section_set(
    artifact: str = typer.Argument(
        ..., metavar="<artifact>",
        help="One of: " + ", ".join(doc_module.PROSE_ARTIFACTS) + ".",
    ),
    section: str = typer.Argument(
        ..., metavar="<section>",
        help='The section title, e.g. "Current understanding" or "In scope".',
    ),
    file: str = typer.Option(
        None, "--file", metavar="<path>", help="Read the new body from this file."
    ),
    stdin: bool = typer.Option(False, "--stdin", help="Read the new body from stdin."),
) -> None:
    """Replace one prose section's body; managed sections keep their own verbs."""
    if (file is None) == (not stdin):
        raise _die("Give the new body with --file <path> or --stdin (exactly one).")
    root = _require_root(); cfg = config.load_config(root); slug = _require_active(cfg)
    svc = _service(root, cfg)
    if stdin:
        body = sys.stdin.read()
    else:
        try:
            body = Path(file).read_text()
        except FileNotFoundError:
            raise _die(f"No body file at {file}.")
        except (OSError, UnicodeDecodeError) as exc:
            raise _die(f"Cannot read {file} as text: {exc}")
    try:
        title = svc.set_section(slug, artifact, section, body)
    except SpecfloError as exc:
        raise _die(str(exc))
    _refresh_checkpoint(svc, slug)
    typer.echo(f"Set '{title}' in {slug}/{artifact}.")


def _require_checkout_project(root: Path, slug: str) -> None:
    """Refuse a daemon-hosted project before any request goes out.

    Follow-up numbers span every project's followup document, which a daemon's
    slug-scoped protocol cannot reach, so the followup verbs work only on
    projects held in this checkout.
    """
    remote = config.hosting_remote(root, slug)
    if remote is not None:
        raise _die(
            f"Project {slug!r} is hosted on remote {remote!r}; follow-ups work only"
            " for projects in this checkout."
        )


@followup_app.command(
    "add",
    epilog='Example: specflo followup add "Flaky lease test" --do "Make it wait for idle"',
)
def followup_add(
    title: str = typer.Argument(..., metavar="<title>", help="What is left (one line)."),
    do: str = typer.Option(..., "--do", help="What a later project should do (one line)."),
    source: str = typer.Option(
        None, "--from", help="Where it came from: a task, a finding, a test (one line)."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Add a follow-up (FU-NN) to the active project's followup document."""
    root = _require_root(); cfg = config.load_config(root); slug = _require_active(cfg)
    _require_checkout_project(root, slug)
    try:
        entry = followup_module.add_followup(root, cfg, slug, title, do, source=source)
    except SpecfloError as exc:
        raise _die(str(exc))
    if json_output:
        typer.echo(json.dumps({"id": entry.id, "project": entry.project}))
    else:
        typer.echo(f"Recorded {entry.id} in {slug}/followup.")


@followup_app.command(
    "close",
    epilog='Example: specflo followup close FU-03 --note "Fixed in the leases project"',
)
def followup_close(
    followup_id: str = typer.Argument(..., metavar="<FU-NN>", help="The follow-up to close."),
    note: str = typer.Option(..., "--note", help="What became of it (one line)."),
) -> None:
    """Close an open follow-up in any project of this checkout."""
    root = _require_root(); cfg = config.load_config(root)
    try:
        slug = followup_module.close_followup(root, cfg, followup_id, note)
    except SpecfloError as exc:
        raise _die(str(exc))
    typer.echo(f"Closed {followup_id} in {slug}/followup.")


@followup_app.command("list", epilog="Example: specflo followup list --all")
def followup_list(
    include_closed: bool = typer.Option(False, "--all", help="Also list the closed entries."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """List the open follow-ups of every project in this checkout."""
    root = _require_root(); cfg = config.load_config(root)
    entries = followup_module.list_followups(root, cfg, include_closed=include_closed)
    if json_output:
        typer.echo(json.dumps([
            {"id": e.id, "project": e.project, "title": e.title, "do": e.do,
             "from": e.source, "status": e.status}
            for e in entries
        ], indent=2))
        return
    if not entries:
        typer.echo("No follow-ups." if include_closed else "No open follow-ups.")
    for e in entries:
        status = "" if e.status == "open" else f" ({e.status})"
        typer.echo(f"{e.id}  {e.project}  {e.title}{status}")
        typer.echo(f"    Do: {e.do}")


@config_app.command("get", epilog="Example: specflo config get autonomy")
def config_get(
    key: str = typer.Argument(
        ..., metavar="<key>", help="A config key; `specflo config list` names them all."
    ),
) -> None:
    """Print one setting's resolved value."""
    root = _require_root()
    # The key is checked before the config is loaded so a typo answers with the
    # valid keys and nothing else -- a warning about some unrelated bad value in
    # the file would only be noise here.
    try:
        spec = config.field_for(key)
    except SpecfloError as exc:
        raise _die(str(exc))
    # Bare: the value alone, so `$(specflo config get autonomy)` is the value
    # and not a label to strip (REQ-14).
    typer.echo(config.render_value(getattr(config.load_config(root), spec.name)))


# The two keys `config set` will not simply write. Both stay readable through
# `config get` and `config list` (REQ-21); it is the write that is a move, not a
# value change.
SWITCH_OWNS_IT = (
    "active_project is set by `specflo switch <name>`, which checks the project"
    " exists and refreshes its checkpoint. `config set` would do neither."
)


def _guard_set(root: Path, key: str, force: bool) -> None:
    """Refuse the two writes that are not just a value change (REQ-20, REQ-22)."""
    if key == "active_project":
        raise _die(SWITCH_OWNS_IT)
    if key != "projects_dir" or force:
        return
    cfg = config.load_config(root)
    existing = local_service(root, cfg).list_projects()
    if existing:
        # Changing the path strands them: specflo would look somewhere else and
        # report no projects, while the files sit where they always were. It
        # moves nothing either way (REQ-23), so --force is the whole opt-in.
        raise _die(
            f"{len(existing)} project(s) live under {cfg.projects_dir}."
            " Changing projects_dir moves nothing - specflo would just stop"
            " seeing them. Move the files yourself, then re-run with --force."
        )


@config_app.command("set", epilog="Example: specflo config set autonomy autonomous")
def config_set(
    key: str = typer.Argument(
        ..., metavar="<key>", help="A config key; `specflo config list` names them all."
    ),
    value: str = typer.Argument(..., metavar="<value>", help="The value to store."),
    force: bool = typer.Option(
        False, "--force", help="Change projects_dir even though projects live under it."
    ),
) -> None:
    """Set one setting, validated before anything is written."""
    root = _require_root()
    # Every check happens before the write, so a refusal leaves the file
    # byte-for-byte as it was (REQ-24).
    try:
        spec = config.field_for(key)
        parsed = config.parse_value(spec, value)
        _guard_set(root, spec.name, force)
        config.write_value(root, spec, parsed)
    except SpecfloError as exc:
        raise _die(str(exc))
    typer.echo(f"Set {spec.name} to {config.render_value(parsed)}.")


@config_app.command("unset", epilog="Example: specflo config unset autonomy")
def config_unset(
    key: str = typer.Argument(
        ..., metavar="<key>", help="A config key; `specflo config list` names them all."
    ),
    force: bool = typer.Option(
        False, "--force", help="Clear projects_dir even though projects live under it."
    ),
) -> None:
    """Drop one setting, returning it to its shipped default."""
    root = _require_root()
    try:
        spec = config.field_for(key)
        # Clearing projects_dir moves it back to the shipped path, which strands
        # existing projects exactly as setting it elsewhere would - same guard.
        _guard_set(root, spec.name, force)
        config.clear_value(root, spec)
    except SpecfloError as exc:
        raise _die(str(exc))
    typer.echo(f"Unset {spec.name}; back to the default {spec.default!r}.")


# How each source reads at the end of a `config list` line. A value the file
# actually sets carries no marker - the absence is the signal.
SOURCE_MARKERS = {
    config.SET: "",
    config.DEFAULTED: "(default)",
    config.INVALID: "(invalid, using default)",
}


@config_app.command("list", epilog="Example: specflo config list --json")
def config_list(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Show every setting, its resolved value, and where that value came from."""
    root = _require_root()
    report = config.report_config(root)

    if json_output:
        typer.echo(json.dumps(report))
        return

    for entry in report["keys"]:
        parts = (
            f"{entry['key']}:",
            config.render_value(entry["value"]),
            SOURCE_MARKERS[entry["source"]],
        )
        typer.echo(" ".join(part for part in parts if part))
    if report["unknown"]:
        # Named, never dropped: specflo leaves them in the file untouched, so
        # saying so is the only way the user learns they are inert.
        typer.echo("\nNot recognized (left as they are):")
        for name in report["unknown"]:
            typer.echo(f"  {name}")


@remote_app.command(
    "add",
    epilog="Example: specflo remote add home http://127.0.0.1:8741 --token <secret>",
)
def remote_add(
    name: str = typer.Argument(
        ..., metavar="<name>",
        help="A short name for the daemon: lowercase letters, digits, - and _.",
    ),
    url: str = typer.Argument(
        ..., metavar="<url>", help="Where the daemon listens, e.g. http://127.0.0.1:8741."
    ),
    token: str = typer.Option(
        ..., "--token", metavar="<secret>",
        help="The bearer token minted by `specflo serve token add`.",
    ),
) -> None:
    """Register a daemon by name, or update one; local projects are untouched."""
    root = _require_root()
    url = url.strip()
    try:
        created = config.add_remote(root, name, url, token)
    except SpecfloError as exc:
        raise _die(str(exc))
    typer.echo(f"{'Registered' if created else 'Updated'} remote '{name}' -> {url}")


@remote_app.command("list", epilog="Example: specflo remote list --json")
def remote_list(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """List the registered daemons: names and URLs, never tokens."""
    root = _require_root()
    remotes = config.list_remotes(root)
    if json_output:
        typer.echo(json.dumps(
            {"remotes": [{"name": name, "url": url} for name, url in remotes.items()]}
        ))
        return
    if not remotes:
        typer.echo(
            "No remotes. Register one with `specflo remote add <name> <url> --token <secret>`."
        )
        return
    for name, url in remotes.items():
        typer.echo(f"{name}  {url}")


@app.command(epilog="Example: specflo promote my-thing --remote home")
def promote(
    name: str = typer.Argument(
        ..., metavar="<project>", help="The local project to move (its slug or name)."
    ),
    remote: str = typer.Option(
        ..., "--remote", metavar="<name>", help="The registered daemon to move it to."
    ),
) -> None:
    """Move a local project into a daemon: upload, verify, then remove the local copy."""
    root = _require_root()
    cfg = config.load_config(root)
    try:
        slug = projects.slugify(name)
        done = promote_module.promote_project(root, cfg, slug, remote)
    except SpecfloError as exc:
        raise _die(str(exc))
    _refresh_index(root, cfg)
    typer.echo(
        f"Promoted '{done.slug}' to remote '{done.remote}': {len(done.files)} files"
        " verified, the local copy removed."
    )


@remote_app.command("remove", epilog="Example: specflo remote remove home")
def remote_remove(
    name: str = typer.Argument(..., metavar="<name>", help="The remote to forget."),
    force: bool = typer.Option(
        False, "--force", help="Forget it even while it hosts projects of this checkout."
    ),
) -> None:
    """Forget a registered daemon; refused while it hosts projects, unless --force."""
    root = _require_root()
    held = sorted(slug for slug, remote in config.hosted_projects(root).items() if remote == name)
    if held and not force:
        raise _die(
            f"Remote {name!r} still hosts {', '.join(held)}; those projects would be"
            " unreachable until it is registered again. Pass --force to forget it anyway."
        )
    try:
        config.remove_remote(root, name)
    except SpecfloError as exc:
        raise _die(str(exc))
    typer.echo(f"Removed remote '{name}'.")


# --- products: held by a daemon, reached through a registered remote ---------


def _pick_remote(root: Path, remote: str | None) -> config.Remote:
    """The remote a daemon-held verb runs on: the one named, or the only one registered.

    Products and their work items live on a daemon, so a verb on them needs
    one. With several registered and none named, the verb refuses rather
    than guess.
    """
    if remote is None:
        remotes = config.list_remotes(root)
        if not remotes:
            raise SpecfloError(
                "No remotes. Register the daemon that holds products with"
                " `specflo remote add <name> <url> --token <secret>`."
            )
        if len(remotes) > 1:
            raise SpecfloError(
                "More than one remote is registered (" + ", ".join(remotes)
                + "); pass --remote <name>."
            )
        (remote,) = remotes
    return config.load_remote(root, remote)


def _products(root: Path, remote: str | None) -> tuple[str, RemoteProducts]:
    registered = _pick_remote(root, remote)
    return registered.name, RemoteProducts(registered.url, registered.token)


PRODUCT_REMOTE_HELP = "The registered daemon to use; the only one registered otherwise."


@product_app.command(
    "add", epilog='Example: specflo product add "My Thing" --repo git@host:me/thing.git'
)
def product_add(
    name: str = typer.Argument(..., metavar="<name>", help="The product's name."),
    slug: str = typer.Option(
        None, "--slug", metavar="<slug>",
        help="File it under this slug instead of one derived from the name.",
    ),
    repo: str = typer.Option(
        None, "--repo", metavar="<location>", help="Where the product's repository lives."
    ),
    remote: str = typer.Option(None, "--remote", metavar="<name>", help=PRODUCT_REMOTE_HELP),
) -> None:
    """Add a product to a daemon; a taken slug is refused."""
    root = _require_root()
    try:
        remote, products = _products(root, remote)
        product = products.add(name, slug=slug, repo=repo)
    except SpecfloError as exc:
        raise _die(str(exc))
    typer.echo(f"Added product '{product.slug}' ({product.name}) on remote '{remote}'.")


@product_app.command("list", epilog="Example: specflo product list --json")
def product_list(
    remote: str = typer.Option(None, "--remote", metavar="<name>", help=PRODUCT_REMOTE_HELP),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """List a daemon's products: slug, name, and repo location."""
    root = _require_root()
    try:
        remote, products = _products(root, remote)
        listed = products.list()
    except SpecfloError as exc:
        raise _die(str(exc))
    if json_output:
        typer.echo(json.dumps({"remote": remote, "products": wire.encode(listed)}))
        return
    if not listed:
        typer.echo(
            f"No products on remote '{remote}'. Add one with `specflo product add <name>`."
        )
        return
    for product in listed:
        typer.echo(
            f"{product.slug}  {product.name}" + (f"  {product.repo}" if product.repo else "")
        )


@product_app.command("show", epilog="Example: specflo product show my-thing")
def product_show(
    slug: str = typer.Argument(..., metavar="<slug>", help="The product's slug."),
    remote: str = typer.Option(None, "--remote", metavar="<name>", help=PRODUCT_REMOTE_HELP),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Show one product: name, slug, repo location, created date, and vision."""
    root = _require_root()
    try:
        _, products = _products(root, remote)
        product = products.show(slug)
    except SpecfloError as exc:
        raise _die(str(exc))
    if json_output:
        typer.echo(json.dumps(wire.encode(product)))
        return
    typer.echo(f"Product: {product.name} ({product.slug})")
    typer.echo(f"Repo:    {product.repo or '-'}")
    typer.echo(f"Created: {product.created}")
    if product.vision:
        typer.echo("Vision:")
        typer.echo(product.vision.rstrip("\n"))
    else:
        typer.echo("Vision:  (none)")


@product_app.command(
    "set-vision", epilog='Example: specflo product set-vision my-thing "Ship the thing."'
)
def product_set_vision(
    slug: str = typer.Argument(..., metavar="<slug>", help="The product's slug."),
    text: str = typer.Argument(None, metavar="[<text>]", help="The vision; or pass --stdin."),
    stdin: bool = typer.Option(False, "--stdin", help="Read the vision from stdin."),
    remote: str = typer.Option(None, "--remote", metavar="<name>", help=PRODUCT_REMOTE_HELP),
) -> None:
    """Replace a product's vision text, given inline or on stdin."""
    if (text is None) == (not stdin):
        raise _die("Give the vision as <text> or with --stdin (exactly one).")
    root = _require_root()
    vision = sys.stdin.read() if stdin else text
    try:
        remote, products = _products(root, remote)
        products.set_vision(slug, vision)
    except SpecfloError as exc:
        raise _die(str(exc))
    typer.echo(f"Set the vision of '{slug}' on remote '{remote}'.")


@product_app.command("roadmap", epilog="Example: specflo product roadmap my-thing")
def product_roadmap(
    slug: str = typer.Argument(..., metavar="<slug>", help="The product's slug."),
    remote: str = typer.Option(None, "--remote", metavar="<name>", help=PRODUCT_REMOTE_HELP),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Print a product's roadmap: its vision, then its backlog in order. A view, never a write."""
    root = _require_root()
    try:
        _, products = _products(root, remote)
        roadmap = products.roadmap(slug)
    except SpecfloError as exc:
        raise _die(str(exc))
    if json_output:
        typer.echo(json.dumps(wire.encode(roadmap)))
        return
    product = roadmap.product
    typer.echo(f"Roadmap: {product.name} ({product.slug})")
    if product.vision:
        typer.echo("Vision:")
        typer.echo(product.vision.rstrip("\n"))
    else:
        typer.echo("Vision:  (none)")
    if not roadmap.items:
        typer.echo("Backlog:  (empty)")
        return
    typer.echo("Backlog:")
    for item in roadmap.items:
        line = f"{item.id}  {item.status}  {item.kind}  {item.dev_path}  {item.title}"
        if item.piece:
            line += f" [{item.piece}]"
        if item.project:
            line += f" -> {item.project}"
        typer.echo(line)


@piece_app.command("add", epilog="Example: specflo product piece add my-thing web")
def product_piece_add(
    product: str = typer.Argument(..., metavar="<product>", help="The product's slug."),
    name: str = typer.Argument(
        ..., metavar="<piece>", help="The piece to declare, e.g. web, admin, mobile."
    ),
    remote: str = typer.Option(None, "--remote", metavar="<name>", help=PRODUCT_REMOTE_HELP),
) -> None:
    """Declare a piece on a product; its work items may then target it."""
    root = _require_root()
    try:
        remote, products = _products(root, remote)
        products.add_piece(product, name)
    except SpecfloError as exc:
        raise _die(str(exc))
    typer.echo(f"Declared piece '{name}' for product '{product}' on remote '{remote}'.")


@piece_app.command("list", epilog="Example: specflo product piece list my-thing")
def product_piece_list(
    product: str = typer.Argument(..., metavar="<product>", help="The product's slug."),
    remote: str = typer.Option(None, "--remote", metavar="<name>", help=PRODUCT_REMOTE_HELP),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """List a product's declared pieces in declaration order."""
    root = _require_root()
    try:
        _, products = _products(root, remote)
        pieces = products.list_pieces(product)
    except SpecfloError as exc:
        raise _die(str(exc))
    if json_output:
        typer.echo(json.dumps({"product": product, "pieces": pieces}))
        return
    if not pieces:
        typer.echo(f"No pieces on product '{product}'; its work items carry no target.")
        return
    for piece in pieces:
        typer.echo(piece)


@piece_app.command("remove", epilog="Example: specflo product piece remove my-thing admin")
def product_piece_remove(
    product: str = typer.Argument(..., metavar="<product>", help="The product's slug."),
    name: str = typer.Argument(..., metavar="<piece>", help="The piece to drop."),
    remote: str = typer.Option(None, "--remote", metavar="<name>", help=PRODUCT_REMOTE_HELP),
) -> None:
    """Drop a declared piece; one a work item targets stays until the item is retargeted."""
    root = _require_root()
    try:
        remote, products = _products(root, remote)
        products.remove_piece(product, name)
    except SpecfloError as exc:
        raise _die(str(exc))
    typer.echo(f"Removed piece '{name}' from product '{product}' on remote '{remote}'.")


# --- work items: a product's backlog, held by the same daemon ----------------


def _workitems(root: Path, remote: str | None) -> tuple[str, RemoteWorkItems]:
    registered = _pick_remote(root, remote)
    return registered.name, RemoteWorkItems(registered.url, registered.token)


@workitem_app.command(
    "add",
    epilog='Example: specflo workitem add my-thing "Fix the login" --kind fix --dev-path one-prompt',
)
def workitem_add(
    product: str = typer.Argument(..., metavar="<product>", help="The product's slug."),
    title: str = typer.Argument(..., metavar="<title>", help="What the work is."),
    kind: str = typer.Option(
        None, "--kind", metavar="<kind>",
        help="Free text; the usual ones are " + ", ".join(workitems_module.KINDS)
        + f" (default: {workitems_module.DEFAULT_KIND}).",
    ),
    issue: str = typer.Option(
        None, "--issue", metavar="<link>", help="The issue this item tracks, if any."
    ),
    dev_path: str = typer.Option(
        None, "--dev-path", metavar="|".join(workitems_module.DEV_PATHS),
        help=f"How the item gets built (default: {workitems_module.DEFAULT_DEV_PATH}).",
    ),
    piece: str = typer.Option(
        None, "--piece", metavar="<piece>", help="Target one of the product's declared pieces."
    ),
    remote: str = typer.Option(None, "--remote", metavar="<name>", help=PRODUCT_REMOTE_HELP),
) -> None:
    """Add a work item to a product's backlog; a dev path outside the three is refused."""
    root = _require_root()
    try:
        remote, items = _workitems(root, remote)
        item = items.add(product, title, kind=kind, issue=issue, dev_path=dev_path, piece=piece)
    except SpecfloError as exc:
        raise _die(str(exc))
    typer.echo(
        f"Added work item {item.id} ({item.title}) to product '{item.product}'"
        f" on remote '{remote}'."
    )


@workitem_app.command(
    "list", epilog="Example: specflo workitem list --product my-thing --status open"
)
def workitem_list(
    product: str = typer.Option(
        None, "--product", metavar="<slug>", help="Only this product's items."
    ),
    status: str = typer.Option(
        None, "--status", metavar="|".join(workitems_module.STATUSES),
        help="Only items in this status.",
    ),
    kind: str = typer.Option(None, "--kind", metavar="<kind>", help="Only items of this kind."),
    remote: str = typer.Option(None, "--remote", metavar="<name>", help=PRODUCT_REMOTE_HELP),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """List work items in backlog order: id, product, status, kind, dev path, title."""
    root = _require_root()
    try:
        remote, items = _workitems(root, remote)
        listed = items.list(product=product, status=status, kind=kind)
    except SpecfloError as exc:
        raise _die(str(exc))
    if json_output:
        typer.echo(json.dumps({"remote": remote, "work_items": wire.encode(listed)}))
        return
    if not listed:
        if any(value is not None for value in (product, status, kind)):
            typer.echo(f"No work items match on remote '{remote}'.")
        else:
            typer.echo(
                f"No work items on remote '{remote}'. Add one with"
                " `specflo workitem add <product> <title>`."
            )
        return
    for item in listed:
        typer.echo(
            f"{item.id}  {item.product}  {item.status}  {item.kind}  {item.dev_path}  {item.title}"
        )


@workitem_app.command("show", epilog="Example: specflo workitem show 3")
def workitem_show(
    item_id: int = typer.Argument(..., metavar="<id>", help="The work item's number."),
    remote: str = typer.Option(None, "--remote", metavar="<name>", help=PRODUCT_REMOTE_HELP),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Show one work item: product, kind, dev path, status, issue link, and created date."""
    root = _require_root()
    try:
        _, items = _workitems(root, remote)
        item = items.show(item_id)
    except SpecfloError as exc:
        raise _die(str(exc))
    if json_output:
        typer.echo(json.dumps(wire.encode(item)))
        return
    typer.echo(f"Work item: {item.id} - {item.title}")
    typer.echo(f"Product:   {item.product}")
    typer.echo(f"Kind:      {item.kind}")
    typer.echo(f"Dev path:  {item.dev_path}")
    typer.echo(f"Piece:     {item.piece or '-'}")
    typer.echo(f"Project:   {item.project or '-'}")
    typer.echo(f"Status:    {item.status}")
    typer.echo(f"Issue:     {item.issue or '-'}")
    typer.echo(f"Created:   {item.created}")


@workitem_app.command("set-status", epilog="Example: specflo workitem set-status 3 done")
def workitem_set_status(
    item_id: int = typer.Argument(..., metavar="<id>", help="The work item's number."),
    status: str = typer.Argument(
        ..., metavar="|".join(workitems_module.STATUSES), help="The status to move it to."
    ),
    remote: str = typer.Option(None, "--remote", metavar="<name>", help=PRODUCT_REMOTE_HELP),
) -> None:
    """Move a work item to another status."""
    root = _require_root()
    try:
        remote, items = _workitems(root, remote)
        item = items.set_status(item_id, status)
    except SpecfloError as exc:
        raise _die(str(exc))
    typer.echo(f"Set work item {item.id} to '{item.status}' on remote '{remote}'.")


@workitem_app.command("spawn", epilog="Example: specflo workitem spawn 3")
def workitem_spawn(
    item_id: int = typer.Argument(
        ..., metavar="<id>", help="The work item's number; its dev path must be full."
    ),
    name: str = typer.Option(
        None, "--name", metavar="<name>",
        help="The project's name; the work item's title otherwise.",
    ),
    remote: str = typer.Option(None, "--remote", metavar="<name>", help=PRODUCT_REMOTE_HELP),
) -> None:
    """Spawn the one project a full-path work item gets, hosted beside it; it becomes active."""
    root = _require_root()
    cfg = config.load_config(root)
    try:
        remote, items = _workitems(root, remote)
        item = items.show(item_id)
        if item.project is None:
            # The slug must be free here too, so the checkout can record it
            # hosted; an item that has its project is the daemon's to refuse.
            slug = projects.slugify(item.title if name is None else name)
            _service_for_new_project(root, cfg, slug, remote)
        spawned = items.spawn(item_id, name=name)
        config.record_hosted_project(root, spawned.project.slug, remote)
        cfg.active_project = spawned.project.slug
        config.save_config(root, cfg)
    except SpecfloError as exc:
        raise _die(str(exc))
    _refresh_index(root, cfg)
    typer.echo(
        f"Spawned project '{spawned.project.slug}' from work item {item_id} on remote"
        f" '{remote}' (now active). Phase: {spawned.project.phase}."
    )
    typer.echo(
        f"Scaffolded {_locator(spawned.project.slug, spawned.brainstorm)} (ready to work)."
    )


# --- leases: members of a daemon's agent pool, driven by the agent verbs ------

# How long `lease request` waits for a full pool when no time is named, in seconds.
LEASE_WAIT_DEFAULT = 600
# The longest the daemon lets a request wait, in seconds: one day. It is the
# pool's own limit, said again here because no pool code loads with the CLI.
LEASE_WAIT_MAX = 86400


@lease_app.command(
    "request", epilog="Example: specflo lease request workers --idle-limit 30m"
)
def lease_request(
    pool: str = typer.Argument(
        None, metavar="<pool>", help="The pool to take a member of; none with --team."
    ),
    team: str = typer.Option(
        None, "--team", metavar="<name>",
        help="Lease every role member of this team instead of one member of a pool, all"
        " or nothing, under one team lease id.",
    ),
    cwd: Path = typer.Option(
        None, "--cwd", metavar="<dir>",
        help="Where the member starts; the current directory otherwise.",
    ),
    idle_limit: str = typer.Option(
        None, "--idle-limit", metavar="<time>",
        help="How long the lease may stay idle, such as 30m, up to the pool's maximum;"
        " the pool's default otherwise.",
    ),
    label: str = typer.Option(
        None, "--label", metavar="<text>",
        help="What the pool shows as the holder; the checkout's directory name otherwise.",
    ),
    wait: int = typer.Option(
        LEASE_WAIT_DEFAULT, "--wait", metavar="<seconds>", min=0, max=LEASE_WAIT_MAX,
        help=f"How long to wait for a full pool, in seconds; {LEASE_WAIT_DEFAULT} otherwise,"
        f" {LEASE_WAIT_MAX} at the most, and 0 refuses a full pool at once. A request that"
        " waits says so at once on stderr; interrupt the command to cancel it.",
    ),
    egress: str = typer.Option(
        None, "--egress", metavar="<class>",
        help="The most open class of member to take: local, no-train or open; no-train"
        " otherwise. The pool's definition may accept less, and that stands.",
    ),
    remote: str = typer.Option(None, "--remote", metavar="<name>", help=PRODUCT_REMOTE_HELP),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Lease a member of <pool>: prints the lease id and the agent to drive with `specflo agent`."""
    from .pool import cli_lease

    root = _require_root()
    try:
        cli_lease.request(
            root, pool, team=team, cwd=cwd, idle_limit=idle_limit, label=label, wait=wait,
            egress=egress, remote=remote, json_output=json_output,
        )
    except SpecfloError as exc:
        raise _die(str(exc))


@lease_app.command("release", epilog="Example: specflo lease release lease-4f2a9c0d1b7e3a55")
def lease_release(
    lease: str = typer.Argument(..., metavar="<lease>", help="The id of the lease to give back."),
    remote: str = typer.Option(None, "--remote", metavar="<name>", help=PRODUCT_REMOTE_HELP),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Give <lease> back to its pool; a lease that has ended already is reported as it ended."""
    from .pool import cli_lease

    root = _require_root()
    try:
        cli_lease.release(root, lease, remote=remote, json_output=json_output)
    except SpecfloError as exc:
        raise _die(str(exc))


@lease_app.command("list", epilog="Example: specflo lease list --json")
def lease_list(
    remote: str = typer.Option(None, "--remote", metavar="<name>", help=PRODUCT_REMOTE_HELP),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """List the leases this checkout holds; another orchestrator's leases are never shown."""
    from .pool import cli_lease

    root = _require_root()
    try:
        cli_lease.list_held(root, remote=remote, json_output=json_output)
    except SpecfloError as exc:
        raise _die(str(exc))


# --- consoles: a developer's own agent as a member of a daemon's pool ----------


@console_app.command("attach", epilog="Example: specflo console attach desk-1 my-pi")
def console_attach(
    slot: str = typer.Argument(
        ..., metavar="<slot>", help="The console slot the pool declares."
    ),
    agent: str = typer.Argument(
        ..., metavar="<agent>",
        help="The running agent to attach: one started on the daemon's host with"
        " `specflo agent start`, on the rpc transport.",
    ),
    remote: str = typer.Option(None, "--remote", metavar="<name>", help=PRODUCT_REMOTE_HELP),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Attach <agent> to the console <slot>, as the developer; the pool then leases it."""
    from .pool import cli_console

    root = _require_root()
    try:
        cli_console.attach(root, slot, agent, remote=remote, json_output=json_output)
    except SpecfloError as exc:
        raise _die(str(exc))


@console_app.command("detach", epilog="Example: specflo console detach desk-1")
def console_detach(
    slot: str = typer.Argument(..., metavar="<slot>", help="The console slot to detach."),
    remote: str = typer.Option(None, "--remote", metavar="<name>", help=PRODUCT_REMOTE_HELP),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Detach the console <slot>: it takes no new lease, and a lease that is out stands."""
    from .pool import cli_console

    root = _require_root()
    try:
        cli_console.detach(root, slot, remote=remote, json_output=json_output)
    except SpecfloError as exc:
        raise _die(str(exc))


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
    except SpecfloError as exc:
        # A refusal no command caught (a bad registry, say) is still a
        # refusal: the message, exit 1, never a traceback.
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise SystemExit(1)
    except click.exceptions.Exit as exc:
        raise SystemExit(exc.exit_code)
    except click.exceptions.Abort:
        typer.echo("Aborted!", err=True)
        raise SystemExit(1)
    except click.exceptions.ClickException as exc:
        exc.show()
        raise SystemExit(exc.exit_code)

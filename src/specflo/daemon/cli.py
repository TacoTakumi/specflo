"""The ``specflo serve`` command: run the daemon on a root of its own.

The web stack is imported only when the daemon actually starts, so every
other specflo command works without the ``serve`` extra installed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click
import typer

from .. import settings
from ..errors import SpecfloError
from ..pool.cli_admin import pool_app
from . import DEFAULT_BIND, DEFAULT_PORT, auth, prepare_root

SERVE_EXTRA_HINT = (
    "specflo serve needs the web stack: install the serve extra with"
    " `pip install 'specflo[serve]'` (or `uv sync --extra serve`)."
)

serve_app = typer.Typer(
    invoke_without_command=True,
    help="Run the specflo daemon: host projects for CLI clients over HTTP.",
)

token_app = typer.Typer(help="Mint the bearer tokens clients present to the daemon.")
serve_app.add_typer(token_app, name="token")

# The pool commands read files under the root and never start the daemon.
serve_app.add_typer(pool_app, name="pool")


@serve_app.callback(epilog="Example: specflo serve --root ~/specflo-daemon --port 8741")
def serve(
    ctx: typer.Context,
    root: Path | None = typer.Option(
        None,
        "--root",
        metavar="<dir>",
        help="The daemon root: holds its projects directory and state store."
        f" Default: ${settings.DAEMON_ROOT_ENV}, else the root saved in"
        f" ~/.specflo/{settings.SETTINGS_FILENAME}.",
    ),
    bind: str = typer.Option(
        DEFAULT_BIND, "--bind", metavar="<host>", help="Address to listen on."
    ),
    port: int = typer.Option(
        DEFAULT_PORT, "--port", metavar="<port>", help="Port to listen on."
    ),
) -> None:
    """Start the daemon on <dir>, bound to loopback unless --bind says otherwise."""
    ctx.obj = _RootOnDemand(option=root)
    if ctx.invoked_subcommand is not None:
        return
    root = ctx.obj["root"]
    try:
        import uvicorn

        from .app import create_app
    except ImportError:
        typer.secho(f"error: {SERVE_EXTRA_HINT}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    url = f"http://{bind}:{port}"
    application = create_app(root, url=url)
    typer.echo(f"specflo daemon: root {root}, listening on {url}")
    uvicorn.run(application, host=bind, port=port)


class _RootOnDemand(dict):
    """The serve context object: the ``--root`` option, and the root on first read.

    The callback runs before Click parses a subcommand's own arguments, so it
    cannot tell ``--help`` from real work. The root is found, and a terminal
    asked for it, only when a command body reads ``ctx.obj["root"]``: help
    never asks, and ``serve root`` reads only the option.
    """

    def __missing__(self, key: str) -> Path:
        if key != "root":
            raise KeyError(key)
        self["root"] = root = _daemon_root(self["option"])
        return root


def _stdin_is_terminal() -> bool:
    return sys.stdin.isatty()


def _daemon_root(option: Path | None) -> Path:
    """The root in force; with none set, ask a terminal and save the answer.

    Off a terminal a question would hang a script or an agent, so the command
    fails and names the one that sets the root.
    """
    try:
        found = settings.resolve_daemon_root(option)
    except SpecfloError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    if found is not None:
        return found[0]
    if not _stdin_is_terminal():
        typer.secho(
            "error: no daemon root is set. Set one with `specflo serve root <dir>`,"
            f" or pass --root <dir> or set ${settings.DAEMON_ROOT_ENV}.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    # All on stderr: stdout carries what the command prints, such as a token
    # secret. typer's prompt still writes the prompt's last space to stdout.
    answer = click.prompt("Daemon root", default=settings.DEFAULT_DAEMON_ROOT, err=True)
    saved = settings.save_daemon_root(answer)
    typer.secho(
        f"Saved {saved} as the daemon root in {settings.settings_path()}.", err=True
    )
    return saved


@serve_app.command(
    "root",
    epilog="Example: specflo serve root ~/specflo-daemon",
)
def root_command(
    ctx: typer.Context,
    directory: Path | None = typer.Argument(
        None, metavar="[<dir>]", help="The directory to save as the daemon root."
    ),
) -> None:
    """Save <dir> as the daemon root, or print the root in force and its source."""
    try:
        if directory is not None:
            saved = settings.save_daemon_root(directory)
            typer.echo(saved)
            typer.secho(f"Saved in {settings.settings_path()}.", err=True)
            return
        found = settings.resolve_daemon_root(ctx.obj["option"])
    except SpecfloError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    if found is None:
        typer.secho(
            "No daemon root is set. Set one with `specflo serve root <dir>`.", err=True
        )
        return
    typer.echo(found[0])
    typer.secho(f"From {found[1]}.", err=True)


@token_app.command(
    "add",
    epilog="Example: specflo serve --root ~/specflo-daemon token add requester",
)
def token_add(
    ctx: typer.Context,
    identity: str = typer.Argument(
        ...,
        metavar="<requester|developer|agent>",
        help="The identity the token is bound to.",
    ),
) -> None:
    """Mint a token for <identity>: the secret prints once, only its hash is kept."""
    root = prepare_root(ctx.obj["root"])
    try:
        secret = auth.mint_token(root, identity)
    except SpecfloError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    typer.echo(secret)
    typer.secho(
        f"Minted a token for {identity} under {root}; it is shown once, store it now.",
        err=True,
    )

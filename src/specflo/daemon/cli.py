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

user_app = typer.Typer(help="Add and list the named users who sign in to the daemon.")
serve_app.add_typer(user_app, name="user")

user_token_app = typer.Typer(help="Mint, list and revoke the personal tokens a user holds.")
user_app.add_typer(user_token_app, name="token")

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
        self["root"] = root = daemon_root(self["option"])
        return root


def _stdin_is_terminal() -> bool:
    return sys.stdin.isatty()


def daemon_root(option: Path | None) -> Path:
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


def _fail(message: str) -> None:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


def _ask_password() -> str:
    """A password typed twice at the terminal, or a refusal.

    Asked here rather than taken as an option value, so it never lands in
    shell history or the process list. Two different entries or an empty
    one fail at once instead of asking again. On stderr, like the root
    question, so stdout keeps to what the command prints.
    """
    first = typer.prompt("Password", hide_input=True, default="", show_default=False, err=True)
    if not first:
        _fail("A password may not be empty.")
    second = typer.prompt(
        "Repeat password", hide_input=True, default="", show_default=False, err=True
    )
    if first != second:
        _fail("The two passwords do not match; no user was added.")
    return first


@user_app.command(
    "add",
    epilog="Example: specflo serve --root ~/specflo-daemon user add rob --role developer --password",
)
def user_add(
    ctx: typer.Context,
    name: str = typer.Argument(
        ...,
        metavar="<name>",
        help="1 to 64 characters from a-z 0-9 . _ - @, starting with a letter or digit;"
        " stored lowercase. 'agent' is reserved.",
    ),
    role: str = typer.Option(
        ..., "--role", metavar="<developer|requester>", help="The role the user holds."
    ),
    password: bool = typer.Option(
        False,
        "--password",
        help="Ask for a password at the terminal, twice. Without it the user has no"
        " password and can act only through a proxy session.",
    ),
) -> None:
    """Add the user <name> with a role; with --password, ask for their password."""
    root = prepare_root(ctx.obj["root"])
    try:
        # Check before asking, so nobody types a password for a user who
        # cannot be added.
        name = auth.check_new_user(root, name, role)
        secret = _ask_password() if password else None
        user = auth.add_user(root, name, role, secret)
    except SpecfloError as exc:
        _fail(str(exc))
    kind = "with a password" if user.password_hash else "proxy-only, no password"
    typer.echo(f"Added user {user.name} ({user.role}, {kind}).")


@user_app.command(
    "list",
    epilog="Example: specflo serve --root ~/specflo-daemon user list",
)
def user_list(ctx: typer.Context) -> None:
    """List every user: name, role, whether a password is set, and tokens owned."""
    root = prepare_root(ctx.obj["root"])
    rows = auth.list_users(root)
    if not rows:
        typer.echo("No users yet. `specflo serve user add <name> --role <role>` adds the first.")
        return
    table = [("NAME", "ROLE", "PASSWORD", "TOKENS")] + [
        (user.name, user.role, "yes" if user.password_hash else "no", str(count))
        for user, count in rows
    ]
    widths = [max(len(row[column]) for row in table) for column in range(3)]
    for row in table:
        typer.echo("  ".join(cell.ljust(width) for cell, width in zip(row, widths)) + "  " + row[3])


@user_app.command(
    "passwd",
    epilog="Example: specflo serve --root ~/specflo-daemon user passwd rob",
)
def user_passwd(
    ctx: typer.Context,
    name: str = typer.Argument(..., metavar="<name>", help="The user whose password changes."),
    clear: bool = typer.Option(
        False,
        "--clear",
        help="Remove the password instead of asking for one: the user becomes proxy-only.",
    ),
) -> None:
    """Set a new password for <name>, asked twice, and end their sessions."""
    root = prepare_root(ctx.obj["root"])
    try:
        # Check before asking, so nobody types a password for no user.
        auth.require_user(root, name)
        secret = None if clear else _ask_password()
        user = auth.set_password(root, name, secret)
    except SpecfloError as exc:
        _fail(str(exc))
    if user.password_hash:
        typer.echo(f"Set a new password for {user.name}; their sessions have ended.")
    else:
        typer.echo(
            f"Cleared the password of {user.name}: proxy-only now; their sessions have ended."
        )


@user_app.command(
    "role",
    epilog="Example: specflo serve --root ~/specflo-daemon user role rob developer",
)
def user_role(
    ctx: typer.Context,
    name: str = typer.Argument(..., metavar="<name>", help="The user whose role changes."),
    role: str = typer.Argument(
        ..., metavar="<developer|requester>", help="The role the user holds from now on."
    ),
) -> None:
    """Give <name> a new role; it applies at their next request, sessions included."""
    root = prepare_root(ctx.obj["root"])
    try:
        user = auth.set_role(root, name, role)
    except SpecfloError as exc:
        _fail(str(exc))
    typer.echo(f"{user.name} is a {user.role} now.")


@user_app.command(
    "remove",
    epilog="Example: specflo serve --root ~/specflo-daemon user remove rob",
)
def user_remove(
    ctx: typer.Context,
    name: str = typer.Argument(..., metavar="<name>", help="The user to remove."),
) -> None:
    """Remove <name> with every token and session they hold."""
    root = prepare_root(ctx.obj["root"])
    try:
        removed = auth.remove_user(root, name)
    except SpecfloError as exc:
        _fail(str(exc))
    typer.echo(f"Removed user {removed} with their tokens and sessions.")


@user_token_app.command(
    "add",
    epilog="Example: specflo serve --root ~/specflo-daemon user token add rob --name laptop",
)
def user_token_add(
    ctx: typer.Context,
    user: str = typer.Argument(
        ..., metavar="<user>", help="The user the token acts as; 'agent' for the working agents."
    ),
    name: str = typer.Option(
        ..., "--name", metavar="<name>", help="A label for the token, unique among the user's."
    ),
) -> None:
    """Mint a personal token for <user>: the secret prints once, only its hash is kept."""
    root = prepare_root(ctx.obj["root"])
    try:
        secret = auth.mint_user_token(root, user, name)
    except SpecfloError as exc:
        _fail(str(exc))
    typer.echo(secret)
    typer.secho(
        f"Minted token {name} for {user.lower()}. Store it now: it will not be shown again.",
        err=True,
    )


@user_token_app.command(
    "list",
    epilog="Example: specflo serve --root ~/specflo-daemon user token list rob",
)
def user_token_list(
    ctx: typer.Context,
    user: str = typer.Argument(..., metavar="<user>", help="The user whose tokens to list."),
) -> None:
    """List the tokens of <user>: name, created, last used and last 4 characters."""
    root = prepare_root(ctx.obj["root"])
    try:
        tokens = auth.list_user_tokens(root, user)
    except SpecfloError as exc:
        _fail(str(exc))
    if not tokens:
        typer.echo(
            f"{user.lower()} has no tokens."
            f" `specflo serve user token add {user.lower()} --name <name>` mints one."
        )
        return
    table = [("NAME", "CREATED", "LAST USED", "LAST4")] + [
        (token.name, token.created, token.last_used or "never", token.last4)
        for token in tokens
    ]
    widths = [max(len(row[column]) for row in table) for column in range(3)]
    for row in table:
        typer.echo("  ".join(cell.ljust(width) for cell, width in zip(row, widths)) + "  " + row[3])


@user_token_app.command(
    "revoke",
    epilog="Example: specflo serve --root ~/specflo-daemon user token revoke rob laptop",
)
def user_token_revoke(
    ctx: typer.Context,
    user: str = typer.Argument(..., metavar="<user>", help="The user who holds the token."),
    name: str = typer.Argument(..., metavar="<name>", help="The token to revoke."),
) -> None:
    """Revoke the token <name> of <user>: a request with it is refused from now on."""
    root = prepare_root(ctx.obj["root"])
    try:
        auth.revoke_user_token(root, user, name)
    except SpecfloError as exc:
        _fail(str(exc))
    typer.echo(f"Revoked token {name} of {user.lower()}.")

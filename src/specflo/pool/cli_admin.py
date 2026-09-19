"""The ``specflo serve pool`` commands: what an admin does to the pool directory.

The pool directory lives under the daemon root, and an admin edits it by
hand. These commands work on those files alone: they start no daemon, listen
on nothing and need no ``serve`` extra.

Every specflo command loads the ``serve`` group, and this group with it, so
this module imports none of the pool's own code until a command runs.
"""

from __future__ import annotations

from pathlib import Path

import typer

# The pool directory's name under the daemon root.
POOL_DIRNAME = "pool"

pool_app = typer.Typer(help="Work on the agent pool's configuration under the daemon root.")


def pool_dir(root: Path) -> Path:
    """The pool directory of the daemon root *root*."""
    return Path(root) / POOL_DIRNAME


@pool_app.command(
    "validate",
    epilog="Example: specflo serve --root ~/specflo-daemon pool validate",
)
def pool_validate(ctx: typer.Context) -> None:
    """Check the whole pool configuration and print every fault in one run."""
    from .config import load_pool_config

    directory = pool_dir(ctx.obj["root"])
    config, errors = load_pool_config(directory)
    if errors:
        for error in errors:
            typer.secho(f"error: {error}", fg=typer.colors.RED, err=True)
        typer.secho(f"{_count(len(errors), 'error')} in {directory}", err=True)
        raise typer.Exit(code=1)
    held = ", ".join(
        _count(len(entries), kind)
        for kind, entries in (
            ("definition", config.definitions),
            ("account", config.accounts),
            ("member", config.members),
            ("pool", config.pools),
            ("team", config.teams),
        )
    )
    typer.echo(f"The pool configuration in {directory} is valid: {held}.")


def _count(number: int, kind: str) -> str:
    return f"{number} {kind}" if number == 1 else f"{number} {kind}s"

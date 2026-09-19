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

# The agent definitions that ship inside the package, one markdown file each;
# ``init`` copies them into a pool directory.
SHIPPED_DIR = Path(__file__).resolve().parent / "shipped"

# The names ``init`` writes to. The pool configuration module holds the same
# two names; they are repeated here so that loading this module loads nothing.
_POOL_FILE = "pool.yaml"
_DEFINITIONS_DIR = "definitions"

# The pool file ``init`` writes. It is all comments, so it declares nothing:
# the roster is what an admin writes, never a default. A prose line is "# "
# and then a word; an example line is YAML with a bare "#" before it, so taking
# that one character off every example line gives a whole configuration.
POOL_FILE_EXAMPLE = """\
# The agent pool's configuration. Nothing here is declared until you take the
# '#' off it, and nothing that is not declared can be leased.
# Check the file with: specflo serve --root <dir> pool validate
#
# Where the rig's llama-swap configuration is. A local member needs it; a
# relative path is taken from this file's directory.
#llama_swap: ~/llama-swap/config.yaml
#
# A provider account: the most leases that may run through it at once, and the
# NAME of the environment variable that holds its one API key, never the key.
#accounts:
#  - name: openrouter-main
#    cap: 4
#    key_env: OPENROUTER_API_KEY
#
# The whole roster. A local member names one concrete llama-swap model ID and
# its class is local. A hosted member names an account, and its class is
# no-train or open.
#members:
#  - name: coder-local
#    command: pi --mode rpc --provider llama-swap --model your-model-id
#    backing: local
#    model: your-model-id
#    labels: [code]
#    capacity: 1
#    egress: local
#  - name: strong-hosted
#    command: pi --mode rpc --provider openrouter --model vendor/strong-model
#    backing: hosted
#    account: openrouter-main
#    labels: [strong-model]
#    capacity: 2
#    egress: no-train
#  - name: scanner-hosted
#    command: pi --mode rpc --provider openrouter --model vendor/cheap-model
#    backing: hosted
#    account: openrouter-main
#    labels: []
#    capacity: 1
#    egress: open
#
# A named pool binds one definition in definitions/ to members that meet it:
# every label the definition needs, and a class no more open than it accepts.
#pools:
#  - name: workers
#    definition: worker
#    members: [coder-local]
#    size: 1
#    idle_default: 10m
#    idle_max: 4h
#  - name: critics
#    definition: critic
#    members: [strong-hosted]
#    size: 2
#    idle_default: 10m
#    idle_max: 4h
#  - name: rebaser
#    definition: hermes-rebaser
#    members: [coder-local]
#    size: 1
#    idle_default: 10m
#    idle_max: 1h
#  - name: model-updates
#    definition: model-update-checker
#    members: [strong-hosted]
#    size: 1
#    idle_default: 10m
#    idle_max: 1h
#  - name: landscape
#    definition: landscape-scanner
#    members: [scanner-hosted]
#    size: 1
#    idle_default: 10m
#    idle_max: 1h
"""

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


@pool_app.command(
    "init",
    epilog="Example: specflo serve --root ~/specflo-daemon pool init",
)
def pool_init(ctx: typer.Context) -> None:
    """Write a starting pool directory: a commented pool file and the shipped definitions."""
    directory = pool_dir(ctx.obj["root"])
    folder = directory / _DEFINITIONS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    targets = [(directory / _POOL_FILE, POOL_FILE_EXAMPLE)]
    for shipped in sorted(SHIPPED_DIR.glob("*.md")):
        targets.append((folder / shipped.name, shipped.read_text(encoding="utf-8")))
    for target, text in targets:
        # A file that is already there is the admin's, edited or not.
        if target.exists():
            typer.echo(f"kept   {target}")
            continue
        target.write_text(text, encoding="utf-8")
        typer.echo(f"wrote  {target}")
    typer.echo(f"Edit {directory / _POOL_FILE}, then check it with 'pool validate'.")


def _count(number: int, kind: str) -> str:
    return f"{number} {kind}" if number == 1 else f"{number} {kind}s"

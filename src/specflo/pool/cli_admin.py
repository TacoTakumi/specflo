"""The ``specflo serve pool`` commands: what an admin does to the pool directory.

The pool directory lives under the daemon root, and an admin edits it by
hand. ``init`` and ``validate`` work on those files alone: they start no
daemon, listen on nothing and need no ``serve`` extra.

A daemon reads the directory when it starts. ``reload`` asks a daemon that
runs to read it again, so that a hand edit is in force with no restart. It is
the one command here that reaches a daemon, and it reaches it as the lease
verbs do: through a remote registered in the checkout it is run from, which
holds where the daemon answers and the developer's token. A daemon root
holds neither, and only the hash of a token. The daemon reads its own
directory, whatever ``--root`` says, so the command prints which one that
was, and says so when it is not the one under ``--root``.

Every specflo command loads the ``serve`` group, and this group with it, so
this module imports none of the pool's own code, and nothing that reaches a
daemon, until a command runs.
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
# Where your own pi models file is. A local member's generated one is a copy
# of it, filtered to the model that member declares.
#models_file: ~/.pi/agent/models.json
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


@pool_app.command(
    "reload",
    epilog="Example: specflo serve --root ~/specflo-daemon pool reload --remote home",
)
def pool_reload(
    ctx: typer.Context,
    remote: str = typer.Option(
        None, "--remote", metavar="<name>",
        help="The registered daemon to ask; the only one registered otherwise.",
    ),
) -> None:
    """Ask the running daemon to read its pool directory again, as the developer."""
    from .. import config
    from ..errors import SpecfloError
    from .cli_lease import pick_remote

    try:
        checkout = config.find_root(Path.cwd())
        if checkout is None:
            raise SpecfloError(
                "A reload is asked of a running daemon, which is reached through a remote: "
                "run it from a checkout where the daemon is registered with the developer's "
                "token (`specflo remote add <name> <url> --token <secret>`)."
            )
        registered = pick_remote(checkout, remote)
        from ..service.pool_remote import REQUEST_TIMEOUT, RemotePool

        client = RemotePool(registered.url, registered.token, timeout=REQUEST_TIMEOUT)
        read = client.reload()
    except SpecfloError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    held = ", ".join(
        _count(read[kind + "s"], kind)
        for kind in ("definition", "account", "member", "pool", "team")
    )
    typer.echo(
        f"The daemon on remote '{registered.name}' (process {read['pid']}) read "
        f"{read['directory']} again, and it is in force: {held}."
    )
    expected = pool_dir(ctx.obj["root"])
    if Path(read["directory"]) != expected and Path(read["directory"]) != expected.resolve():
        typer.secho(
            "note: that is the daemon's own pool directory, not the pool directory under "
            f"--root, {expected}.",
            fg=typer.colors.YELLOW, err=True,
        )


def _count(number: int, kind: str) -> str:
    return f"{number} {kind}" if number == 1 else f"{number} {kind}s"

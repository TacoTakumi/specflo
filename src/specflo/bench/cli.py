"""The ``specflo bench`` command: the model bench, run from the installed package.

Each subcommand hands its arguments to the bench command of the same name and
fills the paths it leaves out from the bench home, ``<daemon root>/bench``:
the arm config, the matrix file, the bench-load state, the runs directory
and, for ``graph``, the records and the output directory. The daemon root is
found as ``specflo serve`` finds it. A flag given on the command line wins.
"""

from __future__ import annotations

import importlib
import os
import shutil
from pathlib import Path

import typer

from . import CONFIG_ENV, DATA, HOME_ENV

HOME_DIRNAME = "bench"
# Each config the operator keeps in the bench home, and the shipped template it starts from.
TEMPLATES = {
    "arms.yaml": DATA / "arms.yaml",
    "matrices.yaml": DATA / "matrices.yaml",
    "pi-models.json": DATA / "configs" / "pi" / "models.json",
}
DEFAULT_RESULTS = "latest"

bench_app = typer.Typer(
    help="Run the model bench: how well a local model drives specflo (see BENCH.md).",
)

# Pass every argument through, --help included, to the bench command itself.
PASS_THROUGH = {"allow_extra_args": True, "ignore_unknown_options": True}


@bench_app.callback(epilog="Example: specflo bench init")
def bench(
    ctx: typer.Context,
    root: Path | None = typer.Option(
        None,
        "--root",
        metavar="<dir>",
        help="The daemon root; the bench home is <dir>/bench. Default: as `specflo serve` finds it.",
    ),
) -> None:
    """Run the model bench with its configs and output under <daemon root>/bench."""
    ctx.obj = {"option": root}


def bench_home(ctx: typer.Context) -> Path:
    """The bench home under the daemon root in force, asking a terminal for the root if none is set."""
    from ..daemon.cli import daemon_root

    return Path(daemon_root(ctx.obj["option"])).expanduser() / HOME_DIRNAME


def home_defaults(command: str, home: Path, args: list[str]) -> list[str]:
    """The flags *command* takes from the bench home, for each one *args* leaves out."""
    paths = {
        "--config": home / "arms.yaml",
        "--matrices": home / "matrices.yaml",
        "--state": home / "state" / "loaded-by-bench.json",
        "--runs-dir": home / "runs",
        "--records": home / "runs",
        "--home": home,
    }
    wanted = {
        "run": ("--config", "--state", "--runs-dir", "--home"),
        "batch": ("--config", "--matrices", "--state", "--runs-dir", "--home"),
        "records": ("--config", "--matrices", "--runs-dir"),
        "probe": ("--config", "--state"),
        "graph": ("--config", "--records"),
        "grade": ("--home",),
    }[command]
    given = {arg.split("=", 1)[0] for arg in args if arg.startswith("--")}
    filled: list[str] = []
    for flag in wanted:
        if flag not in given:
            filled += [flag, str(paths[flag])]
    return filled


def _wants_help(args: list[str]) -> bool:
    return "--help" in args or "-h" in args


def _run(ctx: typer.Context, command: str, args: list[str]) -> None:
    module = importlib.import_module(f"specflo.bench.cmd_{command}")
    if _wants_help(args):
        raise typer.Exit(code=int(module.main(args) or 0))
    home = bench_home(ctx)
    args = home_defaults(command, home, args) + args
    # The run's own processes, the launchers among them, read the operator's
    # configs through these: the home, and the arm config the command uses.
    named = {HOME_ENV: str(home), CONFIG_ENV: _last_value(args, "--config") or str(home / "arms.yaml")}
    before = {name: os.environ.get(name) for name in named}
    os.environ.update(named)
    try:
        code = int(module.main(args) or 0)
    finally:
        for name, value in before.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    raise typer.Exit(code=code)


def _last_value(args: list[str], flag: str) -> str | None:
    """The value of the last ``flag`` in *args*, as argparse reads it."""
    value = None
    for i, arg in enumerate(args):
        if arg == flag and i + 1 < len(args):
            value = args[i + 1]
        elif arg.startswith(flag + "="):
            value = arg.split("=", 1)[1]
    return value


def _command(name: str, summary: str) -> None:
    @bench_app.command(name, help=summary, context_settings=PASS_THROUGH, add_help_option=False)
    def command(ctx: typer.Context) -> None:
        _run(ctx, name, list(ctx.args))


_command("run", "One bench run to a scored, checked run record.")
_command("batch", "Run a matrix of arms, levels and run counts.")
_command("records", "List or check the run records.")
_command("probe", "The reasoning setting each engine gets through each harness.")
_command("grade", "Grade a final tree with the held-out tests.")


@bench_app.command(
    "graph",
    help="Pass-rate and behaviour-metric graphs, written to <home>/results/<name> (default 'latest').",
    context_settings=PASS_THROUGH,
    add_help_option=False,
)
def graph(ctx: typer.Context) -> None:
    args = list(ctx.args)
    if args and not args[0].startswith("-") and not _wants_help(args):
        name, args = args[0], args[1:]
    else:
        name = DEFAULT_RESULTS
    if not _wants_help(args) and not any(a.split("=", 1)[0] == "--out" for a in args):
        args = ["--out", str(bench_home(ctx) / "results" / name), *args]
    _run(ctx, "graph", args)


@bench_app.command("init", epilog="Example: specflo bench init")
def init(ctx: typer.Context) -> None:
    """Write the arm and matrix config templates into the bench home; keep files that exist."""
    home = bench_home(ctx)
    home.mkdir(parents=True, exist_ok=True)
    for name, template in TEMPLATES.items():
        target = home / name
        if target.exists():
            typer.echo(f"kept {target}")
            continue
        shutil.copyfile(template, target)
        typer.echo(f"wrote {target}")
    for name in ("runs", "results", "state"):
        (home / name).mkdir(exist_ok=True)
    typer.secho(
        f"Name your llama-swap entries in {home / 'arms.yaml'}, and give each a model spec"
        f" in {home / 'pi-models.json'}.",
        err=True,
    )

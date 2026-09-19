"""``specflo serve pool validate``: the pool configuration checked from the command line.

An admin edits the pool directory by hand, so the check has to run before a
daemon is started on it: the verb reads the directory under the daemon root,
prints every fault in one run, and says with its exit code whether the
configuration stands. It starts nothing and listens on nothing, and it works
on an install without the ``serve`` extra.
"""

import copy
import shutil
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from specflo.cli import app
from specflo.pool import cli_admin, config, teams

runner = CliRunner()

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "pool"

WORKER = """\
---
role: Does one plan task and reports what changed
tools: [read, bash, edit]
needs: [code]
egress: no-train
---

You are the worker.
"""
CRITIC = WORKER.replace("Does one plan task and reports what changed", "Reviews a result").replace(
    "You are the worker.", "You are the critic."
)

ACCOUNT = {"name": "openrouter-main", "cap": 4, "key_env": "OPENROUTER_API_KEY"}
CODER_A = {
    "name": "coder-a",
    "command": "pi --mode rpc --model model-a",
    "backing": "local",
    "model": "model-a",
    "labels": ["code"],
    "capacity": 1,
    "egress": "local",
}
CODER_B = {**CODER_A, "name": "coder-b", "command": "pi --mode rpc --model model-b",
           "model": "model-b"}
HOSTED = {
    "name": "coder-hosted",
    "command": "pi --mode rpc --model vendor/strong",
    "backing": "hosted",
    "account": "openrouter-main",
    "labels": ["code", "strong-model"],
    "capacity": 2,
    "egress": "no-train",
}
WORKERS = {
    "name": "workers",
    "definition": "worker",
    "members": ["coder-a", "coder-b"],
    "size": 2,
    "idle_default": "10m",
    "idle_max": "4h",
}
CRITICS = {**WORKERS, "name": "critics", "definition": "critic", "members": ["coder-hosted"]}

DESIGNER = {"name": "designer", "pool": "workers", "count": 1}
REVIEWER = {"name": "reviewer", "pool": "critics", "count": 2}


def _team(roles):
    front = yaml.safe_dump({"roles": copy.deepcopy(list(roles))})
    return f"---\n{front}---\n\nThe orchestrator leads.\n"


def _write(root, members=(CODER_A, CODER_B, HOSTED), roles=(DESIGNER, REVIEWER)):
    """A daemon root holding a pool directory: pool.yaml, the llama-swap
    fixture, definitions/ and teams/."""
    directory = root / cli_admin.POOL_DIRNAME
    directory.mkdir(parents=True)
    shutil.copy(FIXTURES / "llama-swap.yaml", directory / "llama-swap.yaml")
    folder = directory / config.DEFINITIONS_DIR
    folder.mkdir()
    for name, text in {"worker": WORKER, "critic": CRITIC}.items():
        (folder / f"{name}.md").write_text(text, encoding="utf-8")
    data = {
        "llama_swap": "llama-swap.yaml",
        "accounts": [ACCOUNT],
        "members": list(members),
        "pools": [WORKERS, CRITICS],
    }
    (directory / config.POOL_FILE).write_text(
        yaml.safe_dump(copy.deepcopy(data)), encoding="utf-8"
    )
    folder = directory / teams.TEAMS_DIR
    folder.mkdir()
    (folder / "gamedev.md").write_text(_team(roles), encoding="utf-8")
    return root


def _write_three_faults(root):
    """The valid directory with one fault in the pool file, one in a
    definition and one in a team."""
    _write(
        root,
        members=(CODER_A, {**CODER_B, "capacity": 0}, HOSTED),
        roles=(DESIGNER, {**REVIEWER, "pool": "testers"}),
    )
    broken = WORKER.replace("egress: no-train", "egress: anywhere")
    folder = root / cli_admin.POOL_DIRNAME / config.DEFINITIONS_DIR
    (folder / "scout.md").write_text(broken, encoding="utf-8")
    return root


def _validate(root):
    return runner.invoke(app, ["serve", "--root", str(root), "pool", "validate"])


@pytest.fixture
def no_sockets(monkeypatch):
    """Fail the test if anything opens a socket."""

    def refuse(*args, **kwargs):
        pytest.fail("pool validate opened a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


# --- a configuration that stands -----------------------------------------


def test_a_valid_pool_directory_exits_zero(tmp_path, no_sockets):
    result = _validate(_write(tmp_path / "daemon"))

    assert result.exit_code == 0, result.output
    assert str(tmp_path / "daemon" / cli_admin.POOL_DIRNAME) in result.output
    assert "error" not in result.output


def test_a_valid_run_says_what_the_configuration_holds(tmp_path):
    result = _validate(_write(tmp_path / "daemon"))

    for count in ("2 definitions", "1 account", "3 members", "2 pools", "1 team"):
        assert count in result.output, result.output
    assert "1 accounts" not in result.output and "1 teams" not in result.output


# --- a configuration with faults -----------------------------------------


def test_three_separate_faults_are_all_printed_in_one_run(tmp_path, no_sockets):
    root = _write_three_faults(tmp_path / "daemon")
    _, faults = config.load_pool_config(root / cli_admin.POOL_DIRNAME)
    assert len(faults) == 3

    result = _validate(root)

    assert result.exit_code != 0
    for fault in faults:
        assert str(fault) in result.output
    assert "member 'coder-b'" in result.output
    assert "definition 'scout'" in result.output
    assert "team 'gamedev' role 'reviewer'" in result.output
    assert "3 errors" in result.output


def test_a_root_with_no_pool_directory_is_a_fault_not_a_traceback(tmp_path):
    root = tmp_path / "daemon"
    root.mkdir()

    result = _validate(root)

    assert result.exit_code == 1
    assert config.POOL_FILE in result.output
    assert "1 error " in result.output


def test_validate_writes_nothing_under_the_root(tmp_path):
    root = tmp_path / "daemon"

    _validate(root)

    assert not root.exists()


def test_validate_needs_a_root():
    result = runner.invoke(app, ["serve", "pool", "validate"])

    assert result.exit_code != 0
    assert "--root" in result.output


# --- no server, no web stack ---------------------------------------------


def test_validate_runs_without_the_serve_extra_and_starts_no_daemon_code(tmp_path):
    # A plain install has none of the web stack. The account limits module
    # talks to a provider, which a check of files on disk has no use for.
    root = _write(tmp_path / "daemon")
    code = textwrap.dedent(
        f"""
        import sys
        for name in ("fastapi", "jinja2", "uvicorn", "starlette", "sse_starlette"):
            sys.modules[name] = None
        from specflo.cli import app
        try:
            app(["serve", "--root", {str(root)!r}, "pool", "validate"])
        except SystemExit as exit:
            assert exit.code in (0, None), exit.code
        loaded = ("specflo.daemon.app", "specflo.daemon.poolstore", "specflo.pool.accounts")
        print("loaded:", " ".join(n for n in loaded if n in sys.modules))
        """
    )

    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)

    assert done.returncode == 0, done.stderr
    assert "is valid" in done.stdout
    assert done.stdout.strip().endswith("loaded:")


def test_starting_the_cli_reads_no_pool_configuration_code():
    # Every specflo command loads the serve group. The pool group rides on
    # it, so it must cost a local command nothing but its own small module.
    code = textwrap.dedent(
        """
        import sys
        import specflo.cli
        print(" ".join(sorted(n for n in sys.modules if n.startswith("specflo.pool"))))
        """
    )

    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)

    assert done.returncode == 0, done.stderr
    assert set(done.stdout.split()) == {"specflo.pool", "specflo.pool.cli_admin"}


def test_the_pool_group_is_listed_under_serve():
    result = runner.invoke(app, ["serve", "--root", "unused", "pool", "--help"])

    assert result.exit_code == 0
    assert "validate" in result.output

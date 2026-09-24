"""A definition's listed paths come back read-only into its members' sandboxes.

The sandbox sweeps the home, so a tool installed there and data kept there are
gone from a member. A definition that lists them gets them back: each listed
path is bound read-only at its real path, and each link on the way to it is
made again inside, so a tool on PATH that is a link into an environment of its
own (the shape a uv tool has) runs by its bare name. A member of a definition
that lists nothing finds them hidden as before. A path that has become missing
or secret since validation refuses the start, naming it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from specflo.pool import launch
from specflo.pool.config import Member

from .test_runner import DEFINITION
from .test_sandbox_checkout_secrets import skip_without_a_sandbox

PROBE = '''import json, os, subprocess, sys

out = sys.argv[1]
asked = json.loads(sys.argv[2])
found = {"listed": {}, "read": {}, "wrote": {}, "ran": {}}
for path in asked.get("directories", []):
    try:
        found["listed"][path] = sorted(os.listdir(path))
    except OSError as exc:
        found["listed"][path] = exc.strerror
for path in asked.get("files", []):
    try:
        with open(path, encoding="utf-8") as f:
            found["read"][path] = f.read()
    except OSError as exc:
        found["read"][path] = exc.strerror
for path in asked.get("writes", []):
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("written")
        found["wrote"][path] = "wrote"
    except OSError as exc:
        found["wrote"][path] = exc.strerror
for name in asked.get("run", []):
    try:
        done = subprocess.run([name], capture_output=True, text=True)
        found["ran"][name] = [done.returncode, done.stdout]
    except OSError as exc:
        found["ran"][name] = exc.strerror
with open(out, "w", encoding="utf-8") as f:
    json.dump(found, f)
'''


@pytest.fixture
def home(tmp_path) -> Path:
    """A home holding a data directory, a data file, and a uv-shaped tool: an
    environment of its own, and a link on PATH into it."""
    home = tmp_path / "home"
    data = home / "data"
    data.mkdir(parents=True)
    (data / "inside.txt").write_text("inside\n", encoding="utf-8")
    (home / "notes.txt").write_text("notes\n", encoding="utf-8")
    env = home / ".local" / "share" / "uv" / "tools" / "tool"
    (env / "bin").mkdir(parents=True)
    (env / "bin" / "python").symlink_to(os.path.realpath(sys.executable))
    tool = env / "bin" / "tool"
    tool.write_text(f"#!{env / 'bin' / 'python'}\nprint('tool ran')\n", encoding="utf-8")
    tool.chmod(0o755)
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".local" / "bin" / "tool").symlink_to(tool)
    return home


@pytest.fixture
def environ(tmp_path, home) -> dict[str, str]:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    return {
        "HOME": str(home), "XDG_RUNTIME_DIR": str(runtime),
        "PATH": f"{home / '.local' / 'bin'}:{os.environ['PATH']}",
    }


def started(tmp_path, environ, listed: tuple[str, ...], asked: dict) -> dict:
    """What a member of a definition listing *listed* finds, asked *asked*."""
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    script = work / "probe.py"
    script.write_text(PROBE, encoding="utf-8")
    out = work / "found.json"
    member = Member(
        name="hosted-1", backing="hosted", labels=(), capacity=1, egress="no-train",
        model="some-vendor/some-model", account="team-a",
        command=f"{sys.executable} {script} {out} '{json.dumps(asked)}'",
    )
    definition = replace(DEFINITION, paths=listed)
    command = launch.member_argv(
        definition, member, environ, cwd=work, state_dir=tmp_path / "state"
    )
    done = subprocess.run(command, env=environ, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(out.read_text(encoding="utf-8"))


def test_a_listed_directory_and_file_are_read_and_not_written(tmp_path, environ, home):
    skip_without_a_sandbox()
    data, notes = home / "data", home / "notes.txt"

    found = started(tmp_path, environ, ("~/data", str(notes)), {
        "directories": [str(data)], "files": [str(data / "inside.txt"), str(notes)],
        "writes": [str(data / "new.txt"), str(notes)],
    })

    assert found["listed"] == {str(data): ["inside.txt"]}
    assert found["read"] == {str(data / "inside.txt"): "inside\n", str(notes): "notes\n"}
    assert found["wrote"] == {
        str(data / "new.txt"): "Read-only file system", str(notes): "Read-only file system",
    }
    assert (notes.read_text(encoding="utf-8"), (data / "new.txt").exists()) == ("notes\n", False)


def test_a_member_of_a_definition_that_lists_nothing_finds_them_hidden(tmp_path, environ, home):
    skip_without_a_sandbox()
    data, notes = home / "data", home / "notes.txt"

    found = started(tmp_path, environ, (), {
        "directories": [str(data)], "files": [str(notes)], "run": ["tool"],
    })

    assert found["listed"] == {str(data): "No such file or directory"}
    assert found["read"] == {str(notes): "No such file or directory"}
    assert found["ran"]["tool"] == "No such file or directory"


def test_a_tool_on_path_that_links_into_its_own_environment_runs_by_its_name(
    tmp_path, environ, home
):
    skip_without_a_sandbox()

    found = started(
        tmp_path, environ, ("~/.local/bin/tool", "~/.local/share/uv/tools/tool"),
        {"run": ["tool"]},
    )

    assert found["ran"]["tool"] == [0, "tool ran\n"]


@pytest.mark.parametrize("became", ["missing", "a link to a hidden directory"])
def test_a_start_whose_listed_path_is_now_at_fault_is_refused_naming_it(
    tmp_path, environ, home, became
):
    listed = home / "data"
    if became == "missing":
        (listed / "inside.txt").unlink()
        listed.rmdir()
    else:
        (home / ".pi" / "agent").mkdir(parents=True)
        (listed / "inside.txt").unlink()
        listed.rmdir()
        listed.symlink_to(home / ".pi" / "agent")
    work = tmp_path / "work"
    work.mkdir()
    member = Member(
        name="hosted-1", backing="hosted", labels=(), capacity=1, egress="no-train",
        model="some-vendor/some-model", account="team-a", command=sys.executable,
    )

    with pytest.raises(launch.LaunchError, match="~/data"):
        launch.member_argv(
            replace(DEFINITION, paths=("~/data",)), member, environ,
            cwd=work, state_dir=tmp_path / "state",
        )

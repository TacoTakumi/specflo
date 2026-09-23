"""The secrets a checkout keeps beside a member's working directory, and the daemon's own.

A holder keeps each lease token under its checkout, in ``.specflo/leases``,
and the bearer token of each daemon it reaches in ``.specflo/remotes``. A
member usually works in that checkout, so both are inside the directory it
is bound into writable, and neither is under the home the sandbox sweeps. A
hosted member shares the host's network, so a daemon token read there is a
way to drive the daemon's API.

The daemon's root holds the pool's token, the tokens of every client and the
pool's state, and it need not be under the home either. Each member's
generated directory and the bridge socket are under it, and those two the
member gets back.

Every check here goes through ``member_argv``, the one builder every start
goes through, so what is tested is what a member gets.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from specflo.pool import launch, sandbox
from specflo.pool.config import Member

from .test_runner import DEFINITION

# Lists each directory it is given and reads each file, and writes what it
# found. The member's own command line follows its arguments, and it
# ignores that.
PROBE = '''import json, os, sys

out = sys.argv[1]
asked = json.loads(sys.argv[2])
found = {"listed": {}, "read": {}}
for path in asked["directories"]:
    try:
        found["listed"][path] = sorted(os.listdir(path))
    except OSError as exc:
        found["listed"][path] = exc.strerror
for path in asked["files"]:
    try:
        with open(path, encoding="utf-8") as f:
            found["read"][path] = f.read()
    except OSError as exc:
        found["read"][path] = exc.strerror
with open(out, "w", encoding="utf-8") as f:
    json.dump(found, f)
'''


def skip_without_a_sandbox() -> None:
    reason = sandbox.unavailable()
    if reason is not None:
        pytest.skip(f"no sandbox on this rig: {reason}")


@pytest.fixture
def environ(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    return {"HOME": str(home), "XDG_RUNTIME_DIR": str(runtime), "PATH": os.environ["PATH"]}


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A checkout holding a lease token and a remote with the daemon's token."""
    checkout = tmp_path / "checkout"
    (checkout / ".specflo" / "leases").mkdir(parents=True)
    (checkout / ".specflo" / "remotes").mkdir()
    (checkout / ".specflo" / "leases" / "other.token").write_text(
        "SECRET-LEASE-TOKEN", encoding="utf-8"
    )
    (checkout / ".specflo" / "remotes" / "rig.json").write_text(
        json.dumps({"url": "http://127.0.0.1:8741", "token": "DAEMON-BEARER"}), encoding="utf-8"
    )
    return checkout


def probed(tmp_path: Path, cwd: Path, asked: dict) -> tuple[Member, Path]:
    """A hosted member whose command is the probe, and where it writes.

    Both are in the working directory: the sandbox has a fresh /tmp, so a
    probe anywhere else under the test's directory is not there inside.
    """
    script = cwd / "probe.py"
    script.write_text(PROBE, encoding="utf-8")
    out = cwd / "found.json"
    member = Member(
        name="hosted-1", command="python", backing="hosted", labels=(), capacity=1,
        egress="no-train", model="some-vendor/some-model", account="team-a",
    )
    command = " ".join([sys.executable, str(script), str(out), _quoted(json.dumps(asked))])
    return replace(member, command=command), out


def _quoted(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def argv(tmp_path: Path, environ, member: Member, cwd: Path, **more) -> list[str]:
    return launch.member_argv(
        DEFINITION, member, environ, cwd=cwd, state_dir=tmp_path / "state", **more
    )


def run(command: list[str], out: Path) -> dict:
    done = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(out.read_text(encoding="utf-8"))


def test_checkout_secrets_in_the_working_directory_read_as_empty(
    tmp_path, environ, checkout
) -> None:
    skip_without_a_sandbox()
    leases = checkout / ".specflo" / "leases"
    remotes = checkout / ".specflo" / "remotes"
    member, out = probed(tmp_path, checkout, {
        "directories": [str(leases), str(remotes)],
        "files": [str(leases / "other.token"), str(remotes / "rig.json")],
    })

    found = run(argv(tmp_path, environ, member, checkout), out)

    assert found["listed"] == {str(leases): [], str(remotes): []}
    for text in found["read"].values():
        assert "SECRET-LEASE-TOKEN" not in text
        assert "DAEMON-BEARER" not in text
    # the host still has them
    assert (leases / "other.token").read_text(encoding="utf-8") == "SECRET-LEASE-TOKEN"


def test_checkout_secrets_written_after_the_sandbox_is_built_read_as_empty(
    tmp_path, environ
) -> None:
    skip_without_a_sandbox()
    # A checkout that has held no lease yet: its first token is written
    # once the member it is for has started.
    checkout = tmp_path / "fresh"
    (checkout / ".specflo").mkdir(parents=True)
    leases = checkout / ".specflo" / "leases"
    member, out = probed(tmp_path, checkout, {
        "directories": [str(leases)], "files": [str(leases / "mine.token")],
    })
    command = argv(tmp_path, environ, member, checkout)

    assert leases.is_dir()
    assert leases.stat().st_mode & 0o777 == 0o700
    (leases / "mine.token").write_text("WRITTEN-LATER", encoding="utf-8")
    found = run(command, out)

    assert found["listed"] == {str(leases): []}
    assert "WRITTEN-LATER" not in found["read"][str(leases / "mine.token")]


def test_checkout_secrets_of_every_ancestor_checkout_are_hidden(
    tmp_path, environ, checkout
) -> None:
    work = checkout / "src" / "deep"
    work.mkdir(parents=True)
    member, _ = probed(tmp_path, work, {"directories": [], "files": []})

    command = argv(tmp_path, environ, member, work)

    for name in ("leases", "remotes"):
        hidden = str(checkout / ".specflo" / name)
        assert ["--tmpfs", hidden] == command[command.index(hidden) - 1:command.index(hidden) + 1]


def test_checkout_secrets_are_not_made_where_there_is_no_checkout(tmp_path, environ) -> None:
    work = tmp_path / "plain"
    work.mkdir()
    member, _ = probed(tmp_path, work, {"directories": [], "files": []})

    argv(tmp_path, environ, member, work)

    assert not (work / ".specflo").exists()


def test_the_daemon_root_reads_as_empty_but_for_what_the_member_is_given(
    tmp_path, environ, checkout
) -> None:
    skip_without_a_sandbox()
    root = tmp_path / "daemon"
    (root / "pool").mkdir(parents=True)
    (root / "pool-token").write_text("POOL-TOKEN", encoding="utf-8")
    (root / "tokens.json").write_text('{"x": "CLIENT-TOKEN"}', encoding="utf-8")
    other = root / "pi-config" / "other-abc"
    other.mkdir(parents=True)
    (other / "models.json").write_text("OTHER-KEY", encoding="utf-8")
    mine = root / "pi-config" / "hosted-1-xyz"
    mine.mkdir()
    (mine / "models.json").write_text("MY-MODELS", encoding="utf-8")
    member, out = probed(tmp_path, checkout, {
        "directories": [str(root), str(root / "pi-config")],
        "files": [
            str(root / "pool-token"), str(root / "tokens.json"),
            str(other / "models.json"), str(mine / "models.json"),
        ],
    })

    found = run(argv(tmp_path, environ, member, checkout, config_dir=mine, daemon_root=root), out)

    assert found["listed"][str(root)] == ["pi-config"]
    assert found["listed"][str(root / "pi-config")] == ["hosted-1-xyz"]
    assert found["read"][str(mine / "models.json")] == "MY-MODELS"
    for path in (root / "pool-token", root / "tokens.json", other / "models.json"):
        assert found["read"][str(path)] in ("", "No such file or directory"), path

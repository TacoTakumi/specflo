"""A member's git inside a real sandbox: it commits, rebases, and yields to a repository's own identity.

The sandbox sweeps the home, so without the generated global configuration a
member's git has no identity and refuses to commit. With it, a commit in a
scratch repository carries the operator's name and address as author and
committer, a rebase completes, and a repository that sets an identity of its
own keeps it. The sandbox and the environment come from the builders every
start goes through.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from specflo.pool import launch, piconfig

from .test_member_git_identity import operator_git  # noqa: F401  (fixture)
from .test_piconfig import ACCOUNTS, NO_TRAIN_MEMBER
from .test_runner import DEFINITION
from .test_sandbox_checkout_secrets import environ, skip_without_a_sandbox  # noqa: F401

PROBE = r'''
import json, os, subprocess, sys

out = sys.argv[1]
found = {}

def git(*args, cwd):
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return {"code": done.returncode, "out": done.stdout, "err": done.stderr}

def commit(repo, name, message):
    with open(os.path.join(repo, name), "w") as f:
        f.write(message + "\n")
    git("add", name, cwd=repo)
    return git("commit", "-q", "-m", message, cwd=repo)

found["global"] = git("config", "--global", "--list", cwd=os.getcwd())

scratch = os.path.join(os.getcwd(), "scratch")
os.mkdir(scratch)
git("init", "-q", "-b", "main", cwd=scratch)
found["commit"] = commit(scratch, "one.txt", "one")
found["who"] = git("log", "-1", "--format=%an <%ae>|%cn <%ce>", cwd=scratch)

git("checkout", "-q", "-b", "topic", cwd=scratch)
commit(scratch, "two.txt", "two")
git("checkout", "-q", "main", cwd=scratch)
commit(scratch, "three.txt", "three")
git("checkout", "-q", "topic", cwd=scratch)
found["rebase"] = git("rebase", "-q", "main", cwd=scratch)
found["history"] = git("log", "--format=%s", cwd=scratch)

local = os.path.join(os.getcwd(), "local")
os.mkdir(local)
git("init", "-q", "-b", "main", cwd=local)
git("config", "user.email", "repo@example.com", cwd=local)
found["local_commit"] = commit(local, "a.txt", "a")
found["local_who"] = git("log", "-1", "--format=%ae|%ce", cwd=local)

with open(out, "w") as f:
    json.dump(found, f)
'''


def test_a_member_commits_rebases_and_keeps_a_repositorys_own_identity(
    tmp_path, environ, operator_git  # noqa: F811
) -> None:
    skip_without_a_sandbox()
    operator_git(name="Rob Operator", email="rob@example.com")
    work = tmp_path / "work"
    work.mkdir()
    script = work / "probe.py"
    script.write_text(PROBE, encoding="utf-8")
    out = work / "found.json"
    member = replace(
        NO_TRAIN_MEMBER, command=f"{sys.executable} {script} {out}"
    )
    config_dir = piconfig.create(tmp_path / "generated", member, ACCOUNTS)
    caller = {**environ, "TEAM_A_KEY": "key-of-team-a"}
    command = launch.member_argv(
        DEFINITION, member, caller, cwd=work, state_dir=tmp_path / "state",
        config_dir=config_dir,
    )
    env = launch.member_env(DEFINITION, member, ACCOUNTS, caller, config_dir=config_dir)

    done = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)

    assert "bwrap" in [Path(part).name for part in command]
    assert done.returncode == 0, done.stderr
    found = json.loads(out.read_text(encoding="utf-8"))
    assert found["global"]["out"].splitlines() == [
        "user.name=Rob Operator", "user.email=rob@example.com",
    ]
    assert found["commit"]["code"] == 0, found["commit"]["err"]
    assert found["who"]["out"].strip() == (
        "Rob Operator <rob@example.com>|Rob Operator <rob@example.com>"
    )
    assert found["rebase"]["code"] == 0, found["rebase"]["err"]
    assert found["history"]["out"].split() == ["two", "three", "one"]
    assert found["local_commit"]["code"] == 0, found["local_commit"]["err"]
    assert found["local_who"]["out"].strip() == "repo@example.com|repo@example.com"


def test_without_the_generated_configuration_the_member_cannot_commit(
    tmp_path, environ, operator_git  # noqa: F811
) -> None:
    # The control: the same sandbox with no identity to give. A commit is
    # refused, so the case above is the generated file's doing.
    skip_without_a_sandbox()
    operator_git()
    work = tmp_path / "work"
    work.mkdir()
    script = work / "probe.py"
    script.write_text(PROBE, encoding="utf-8")
    out = work / "found.json"
    member = replace(NO_TRAIN_MEMBER, command=f"{sys.executable} {script} {out}")
    config_dir = piconfig.create(tmp_path / "generated", member, ACCOUNTS)
    caller = {**environ, "TEAM_A_KEY": "key-of-team-a"}
    command = launch.member_argv(
        DEFINITION, member, caller, cwd=work, state_dir=tmp_path / "state",
        config_dir=config_dir,
    )
    env = launch.member_env(DEFINITION, member, ACCOUNTS, caller, config_dir=config_dir)

    done = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)

    assert done.returncode == 0, done.stderr
    found = json.loads(out.read_text(encoding="utf-8"))
    assert found["global"]["out"] == ""
    assert found["commit"]["code"] != 0
    assert not (Path(config_dir) / launch.GITCONFIG_FILE).exists()

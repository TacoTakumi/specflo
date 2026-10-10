"""Worktree creation is serialized under a lock.

`git worktree add` writes shared state in the repository, so concurrent
starts on different tasks could trip over each other. Three processes
starting three tasks at once all succeed.
"""

import os
import subprocess
import sys
from pathlib import Path

from specflo import plan
from test_worktree_start import SLUG, git, make_repo

TASKS = (("One", "src/a.py"), ("Two", "src/a.py"), ("Three", "src/a.py"))


def test_three_concurrent_starts_all_succeed(tmp_path):
    cfg, ids = make_repo(tmp_path, tasks=TASKS)
    specflo_bin = str(Path(sys.executable).parent / "specflo")
    env = {**os.environ}
    env.pop("SPECFLO_DIR", None)

    procs = [
        subprocess.Popen(
            [specflo_bin, "task", "start", task_id, "--worktree"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=tmp_path, env=env,
        )
        for task_id in ids
    ]
    for proc in procs:
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err + out

    branches = git(tmp_path, "branch", "--list", "--format=%(refname:short)").splitlines()
    for task_id in ids:
        assert (tmp_path / ".specflo" / "worktrees" / SLUG / task_id / "src" / "a.py").is_file()
        assert f"specflo/{SLUG}/{task_id}" in branches
    assert {t.progress for t in plan.list_tasks(tmp_path, cfg, SLUG)} == {"in_progress"}

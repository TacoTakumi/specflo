"""A whole ladder driven the way a harness drives it: loop on `auto --json`.

Between passes the test does what the agent would at each level, reading only
the project's state, until a pass says stop.
"""

import json

from specflo import auto, config, projects
from test_ladder import _ok, _pass_review, _work, git, repo  # noqa: F401  (repo is a fixture)


def _act(root, done):
    """One level's next step, chosen from the project's state alone."""
    project = projects.load_project(root, config.load_config(root), "thing")
    level, phase = project.level, project.phase
    if level == "quick":
        _ok(["section", "set", "brief", "Goal", "--stdin"], "Greet in the file.\n")
        _ok(["section", "set", "brief", "Done when", "--stdin"], "- app.txt says hi\n")
        _work(root, "app.txt", "hi\n")
        _ok(["section", "set", "brief", "Proof", "--stdin"], "cat app.txt -> hi\n")
        _ok(["advance"])
    elif phase == "brainstorm":
        if level == "fast":
            _ok(["decision", "add", "--text", "one greeting file", "--rationale",
                 "weighed one file, two files; one is enough"])
        _ok(["section", "set", "brainstorm", "Out of scope / Deferred", "--stdin"], "none\n")
        _ok(["advance"])
    elif phase == "spec":
        _ok(["section", "set", "spec", "In scope", "--stdin"], "- the greeting.\n")
        _ok(["section", "set", "spec", "Out of scope", "--stdin"], "- the rest.\n")
        _ok(["advance"])
    elif phase == "plan":
        _ok(["advance"])
    else:
        if level not in done:
            _work(root, f"{level}.txt")
            _ok(["review", "start"])
            _pass_review()
            done.add(level)
        _ok(["advance"])


def test_a_ladder_driven_by_auto_json_climbs_to_full_and_stops(repo):  # noqa: F811
    _ok(["new", "Thing", "--level", "quick"])
    result = json.loads(_ok(["auto", "--ladder", "--json"]).output)
    done = set()
    for _ in range(40):
        if result["stop"]:
            break
        _act(repo, done)
        result = json.loads(_ok(["auto", "--json"]).output)

    assert result["stop"] is True
    assert result["reason"] == auto.STOP_PROJECT_COMPLETE
    branches = set(git(repo, "branch", "--format=%(refname:short)").splitlines())
    assert {"specflo/thing/quick", "specflo/thing/fast", "specflo/thing/full"} <= branches
    # Stacked: each level's branch holds the one before it.
    assert git(repo, "merge-base", "--is-ancestor", "specflo/thing/quick", "specflo/thing/fast") == ""
    assert git(repo, "merge-base", "--is-ancestor", "specflo/thing/fast", "specflo/thing/full") == ""
    ladder_md = (repo / "docs" / "projects" / "thing" / "ladder.md").read_text()
    for level in ("quick", "fast", "full"):
        assert f"| {level} |" in ladder_md


def test_a_ladder_leaves_the_remote_and_every_old_branch_alone(repo, tmp_path_factory):  # noqa: F811
    remote = tmp_path_factory.mktemp("remote") / "origin.git"
    git(remote.parent, "init", "-q", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "branch", "keep")
    git(repo, "push", "-q", "origin", "main", "keep")
    remote_before = git(remote, "for-each-ref")
    local_before = {b: git(repo, "rev-parse", b) for b in ("main", "keep")}

    _ok(["new", "Thing", "--level", "quick"])
    result = json.loads(_ok(["auto", "--ladder", "--json"]).output)
    done = set()
    for _ in range(40):
        if result["stop"]:
            break
        _act(repo, done)
        result = json.loads(_ok(["auto", "--json"]).output)

    assert result["reason"] == auto.STOP_PROJECT_COMPLETE
    assert git(remote, "for-each-ref") == remote_before
    assert {b: git(repo, "rev-parse", b) for b in ("main", "keep")} == local_before

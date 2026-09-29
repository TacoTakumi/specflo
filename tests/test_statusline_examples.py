"""The example statusline scripts under examples/statusline/.

The segment script must read specflo's real files the way the pi extension's
status segment does, so every repo here is built with specflo's own writers,
not hand-written markdown.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from specflo import config, plan, projects, spec

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "statusline"
SEGMENT = EXAMPLES / "specflo_segment.py"

ESC = "\x1b"


def _env(**extra):
    env = {k: v for k, v in os.environ.items()
           if k not in ("SPECFLO_DIRECTORY", "NO_COLOR")}
    env.update(extra)
    return env


def run_segment(*args, cwd, **env):
    return subprocess.run(
        [sys.executable, str(SEGMENT), *args],
        cwd=cwd, env=_env(**env), capture_output=True, text=True, timeout=30,
    )


def make_repo(root: Path, name="demo", phase="brainstorm", tasks=0,
              started=None, done=(), superseded=None, status=None) -> Path:
    """A specflo repo whose active project `name` is in `phase`.

    `tasks` pending tasks are added in plan; `started` and `done` name tasks to
    move once in execute; `superseded` names a task a new one replaces.
    """
    config.init_config(root)
    cfg = config.load_config(root)
    project = projects.create_project(root, cfg, name, created="2026-09-29")
    slug = project.slug
    projects.switch_project(root, cfg, slug)
    cfg = config.load_config(root)
    order = ["brainstorm", "spec", "plan", "execute"]
    for _ in range(order.index(phase)):
        project = projects.advance_project(root, cfg, slug)
        if project.phase == "spec":
            spec.start_spec(root, cfg, slug, today="2026-09-29")
            spec.add_requirement(root, cfg, slug, "a behaviour", acceptance="ok",
                                 today="2026-09-29")
        if project.phase == "plan":
            plan.start_plan(root, cfg, slug, today="2026-09-29")
            for i in range(tasks):
                plan.add_task(root, cfg, slug, f"task {i + 1}", acceptance="ok",
                              verify="true", implements=["REQ-01"], today="2026-09-29")
            if superseded:
                plan.add_task(root, cfg, slug, "replacement", acceptance="ok",
                              verify="true", implements=["REQ-01"],
                              supersedes=superseded, today="2026-09-29")
    for task_id in done:
        plan.start_task(root, cfg, slug, task_id, today="2026-09-29")
        plan.done_task(root, cfg, slug, task_id, today="2026-09-29")
    if started:
        plan.start_task(root, cfg, slug, started, today="2026-09-29")
    if status == "complete":
        projects.complete_project(root, cfg, slug)
    elif status == "shelved":
        projects.shelve_project(root, cfg, slug)
    return root


def plain(root, *args, **env):
    result = run_segment(*args, cwd=root, NO_COLOR="1", **env)
    assert result.returncode == 0, result.stderr
    return result.stdout


# --- the segment for each project state -----------------------------------

def test_segment_brainstorm_shows_slug_and_phase(tmp_path):
    assert plain(make_repo(tmp_path)) == "demo:brainstorm\n"


def test_segment_spec_shows_slug_and_phase(tmp_path):
    assert plain(make_repo(tmp_path, phase="spec")) == "demo:spec\n"


def test_segment_plan_shows_the_tally(tmp_path):
    assert plain(make_repo(tmp_path, phase="plan", tasks=3)) == "demo:plan 0/3\n"


def test_segment_execute_drops_the_phase_and_names_the_task_in_progress(tmp_path):
    root = make_repo(tmp_path, phase="execute", tasks=4, done=["T-01"], started="T-02")
    assert plain(root) == "demo T-02 1/4\n"


def test_segment_execute_with_no_task_in_progress(tmp_path):
    root = make_repo(tmp_path, phase="execute", tasks=2, done=["T-01"])
    assert plain(root) == "demo 1/2\n"


def test_segment_does_not_count_a_superseded_task(tmp_path):
    root = make_repo(tmp_path, phase="execute", tasks=3, superseded="T-03")
    assert plain(root) == "demo 0/3\n"


def test_segment_complete_project(tmp_path):
    root = make_repo(tmp_path, phase="execute", tasks=1, done=["T-01"], status="complete")
    assert plain(root) == "demo done\n"


def test_segment_shelved_project(tmp_path):
    assert plain(make_repo(tmp_path, status="shelved")) == "demo shelved\n"


def test_segment_cuts_a_long_slug(tmp_path):
    root = make_repo(tmp_path, name="a very long project name")
    assert plain(root) == "a-very-long-proj…:brainstorm\n"


def test_segment_prints_nothing_without_a_specflo_config(tmp_path):
    assert plain(tmp_path) == ""


def test_segment_prints_nothing_without_an_active_project(tmp_path):
    config.init_config(tmp_path)
    assert plain(tmp_path) == ""


def test_segment_prints_nothing_for_an_unreadable_project(tmp_path):
    root = make_repo(tmp_path)
    (root / "docs" / "projects" / "demo" / "project.md").write_text("no front matter\n")
    assert plain(root) == ""


def test_segment_prints_nothing_for_a_missing_directory(tmp_path):
    assert plain(tmp_path, str(tmp_path / "gone")) == ""


# --- where it looks, and colour --------------------------------------------

def test_segment_takes_the_directory_as_its_argument(tmp_path):
    root = make_repo(tmp_path / "repo")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert plain(elsewhere, str(root)) == "demo:brainstorm\n"


def test_segment_walks_up_from_a_subdirectory(tmp_path):
    root = make_repo(tmp_path)
    sub = root / "src" / "deep"
    sub.mkdir(parents=True)
    assert plain(sub) == "demo:brainstorm\n"


def test_segment_honours_specflo_directory(tmp_path):
    root = make_repo(tmp_path / "repo")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert plain(elsewhere, str(elsewhere), SPECFLO_DIRECTORY=str(root)) == "demo:brainstorm\n"


@pytest.mark.parametrize("status, code", [(None, "35"), ("complete", "2"), ("shelved", "2")])
def test_segment_colours_without_no_color(tmp_path, status, code):
    root = make_repo(tmp_path, status=status)
    result = run_segment(cwd=root)
    assert result.returncode == 0
    assert result.stdout.startswith(f"{ESC}[{code}m")
    assert result.stdout.endswith(f"{ESC}[0m\n")


def test_segment_prints_no_escape_byte_with_no_color(tmp_path):
    assert ESC not in plain(make_repo(tmp_path))


# --- the full Claude Code statusline ----------------------------------------

FULL = EXAMPLES / "claude_statusline.py"
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def run_full(payload, script=FULL, cwd=None):
    result = subprocess.run(
        [sys.executable, str(script)], input=json.dumps(payload), cwd=cwd,
        env=_env(), capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return ANSI.sub("", result.stdout)


def full_payload(directory):
    now = time.time()
    return {
        "cwd": str(directory),
        "workspace": {"current_dir": str(directory), "project_dir": str(directory)},
        "model": {"display_name": "Some Model (1M context)"},
        "effort": {"level": "high"},
        "context_window": {
            "context_window_size": 1_000_000,
            "current_usage": {"input_tokens": 10_000, "cache_creation_input_tokens": 20_000,
                              "cache_read_input_tokens": 70_000},
        },
        "rate_limits": {
            "five_hour": {"used_percentage": 42.4, "resets_at": now + 2 * 3600 + 300},
            "seven_day": {"used_percentage": 81, "resets_at": now + 3 * 86400 + 3600},
        },
    }


def test_full_prints_every_segment(tmp_path):
    root = make_repo(tmp_path / "repo")
    out = run_full(full_payload(root))
    assert out.count("\n") == 1
    segs = out.strip().split(" | ")
    assert segs[0] == "repo"
    assert segs[1] == "demo:brainstorm"
    assert segs[2] == "Some Model high"
    assert segs[3] == "100k/1M (10%)"
    assert segs[4] == "cache 70%"
    assert re.fullmatch(r"Q5h 42% 2h0[45]m", segs[5]), segs[5]
    assert re.fullmatch(r"Q7d 81% 3d[01]h", segs[6]), segs[6]
    assert len(segs) == 7


def test_full_leaves_out_absent_data(tmp_path):
    out = run_full({"workspace": {"current_dir": str(tmp_path)},
                    "model": {"display_name": "Some Model"}})
    assert out == f"{tmp_path.name} | Some Model\n"


def test_full_prints_nothing_for_bad_input(tmp_path):
    result = subprocess.run([sys.executable, str(FULL)], input="not json",
                            env=_env(), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0
    assert result.stdout == ""


def test_full_without_the_segment_file_leaves_the_segment_out(tmp_path):
    root = make_repo(tmp_path / "repo")
    alone = tmp_path / "alone"
    alone.mkdir()
    shutil.copy(FULL, alone / FULL.name)
    out = run_full(full_payload(root), script=alone / FULL.name)
    assert out.startswith("repo | Some Model high | ")
    assert "demo" not in out


def test_full_through_a_symlink_still_finds_the_segment_file(tmp_path):
    root = make_repo(tmp_path / "repo")
    links = tmp_path / "links"
    links.mkdir()
    (links / "statusline.py").symlink_to(FULL)
    out = run_full(full_payload(root), script=links / "statusline.py")
    assert out.startswith("repo | demo:brainstorm | ")

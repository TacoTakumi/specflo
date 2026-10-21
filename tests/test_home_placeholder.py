"""Shipped files write a placeholder home as /home/you and nothing else.

The release mirror refuses any home path not on its allowlist, and every new
name a session invents (a fake HOME in a test, a path in an error message)
used to end up there. One placeholder, checked here on every test run, keeps
the allowlist from growing and a real home path from reaching the public repo.
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

PLACEHOLDER = "/home/you"
HOME_PATH = re.compile(r"/home/[A-Za-z0-9_.-]+")
# What the mirror strips besides EXCLUDE_PATHS.
ALWAYS_EXCLUDED = (".releaserc",)


def excluded_paths() -> tuple[str, ...]:
    """The paths .releaserc keeps out of the public repo, which ship nowhere."""
    config = REPO_ROOT / ".releaserc"
    if not config.is_file():
        return ALWAYS_EXCLUDED
    match = re.search(r"^EXCLUDE_PATHS=\((.*?)\)", config.read_text(encoding="utf-8"), re.M | re.S)
    names = match.group(1).split() if match else []
    return ALWAYS_EXCLUDED + tuple(name for name in names if not name.startswith("#"))


def shipped_files() -> list[str]:
    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    excluded = excluded_paths()
    return [
        name for name in listed.decode().split("\0")
        if name and not any(name == path or name.startswith(path + "/") for path in excluded)
    ]


def test_shipped_files_use_the_one_home_placeholder():
    found = []
    for name in shipped_files():
        try:
            text = (REPO_ROOT / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            found += [f"{name}:{number}: {path}" for path in HOME_PATH.findall(line)
                      if path != PLACEHOLDER]
    assert not found, f"use {PLACEHOLDER} as the placeholder home:\n" + "\n".join(found)

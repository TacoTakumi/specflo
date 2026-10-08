"""Diagnostics and contamination flags read from a normalised session log.

    diagnose(log, workdir=..., protected=[bench_dir, archive],
             initial_dirs=snapshot_dirs(workdir), complete=...)

returns the run record's `diagnostics` dict:

    {
      "flags": [{"flag": FLAG, "call_id": str, ...}, ...],
      "contaminated": bool,
      "contamination": [{"call_id": str, "tool": str, "path": str}, ...]
    }

Flags (see FLAGS):
    write-outside-workdir   a write or edit tool targets a path outside the workdir
    write-missing-dir       a write or edit inside the workdir targets a directory
                            that was not in `initial_dirs` and that no earlier
                            mkdir or successful write created
    repeated-failing-call   three or more consecutive calls with the same name and
                            arguments, each with is_error set; one flag per streak
    complete-tests-failing  `complete` is true and the last bash call that runs
                            pytest has is_error set

Contamination is kept apart from the flags so a scorer can drop the run: any
tool call whose path arguments, bash command or other string arguments name a
protected path (the bench directory, the held-out archive) counts, unless the
path also lies inside the workdir or one of the run's own dirs (its run dir,
which holds its harness config, bin link and logs under the bench directory).

Tool names and argument keys of both harnesses are handled: pi (read, write,
edit, bash with `path`/`command`) and Claude Code (Read, Write, Edit, Bash,
Glob, Grep with `file_path`/`path`/`command`). Paths are resolved lexically
against the workdir; the filesystem is never consulted. Writes made through
bash redirections are not tracked.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path
from typing import Any, Iterable

from modelbench.normlog import events

FLAGS = (
    "write-outside-workdir",
    "write-missing-dir",
    "repeated-failing-call",
    "complete-tests-failing",
)

WRITE_TOOLS = {"write", "edit", "multiedit", "notebookedit"}
SHELL_TOOLS = {"bash"}
PATH_KEYS = ("file_path", "path", "notebook_path")
COMMAND_KEYS = ("command",)
REPEAT_LIMIT = 3

_PYTEST = re.compile(r"\bpytest\b")
_SEPARATORS = {";", "&&", "||", "|", "&"}
_TOKEN_STRIP = "<>|&;()`'\""


def snapshot_dirs(workdir: str | Path) -> set[str]:
    """Return every directory under workdir, relative to it ("." is the workdir)."""
    root = Path(workdir)
    found = {"."}
    for dirpath, _dirnames, _files in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        found.add(Path(rel).as_posix())
    return found


def _resolve(workdir: str, path: str) -> str:
    return os.path.normpath(os.path.join(workdir, os.path.expanduser(path)))


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _tokens(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _mkdir_targets(command: str, workdir: str) -> set[str]:
    """Directories (and with -p their ancestors) a bash command makes with mkdir."""
    made: set[str] = set()
    active = parents = False
    for tok in _tokens(command):
        if tok in _SEPARATORS:
            active = parents = False
        elif os.path.basename(tok) == "mkdir":
            active, parents = True, False
        elif active and tok.startswith("-"):
            parents = parents or "p" in tok.lstrip("-") or tok == "--parents"
        elif active:
            target = _resolve(workdir, tok)
            made.add(target)
            if parents:
                while _under(target, workdir) and target != workdir:
                    target = os.path.dirname(target)
                    made.add(target)
    return made


def _path_args(args: dict) -> list[str]:
    return [args[k] for k in PATH_KEYS if isinstance(args.get(k), str) and args[k]]


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _protected_hit(
    call: dict, workdir: str, protected: list[str], own: list[str]
) -> str | None:
    """Return the first protected path the call names outside the workdir and `own`, or None."""
    args = call["arguments"]
    candidates = [_resolve(workdir, p) for p in _path_args(args)]
    for key in COMMAND_KEYS:
        if isinstance(args.get(key), str):
            for tok in _tokens(args[key]):
                for part in (tok, tok.partition("=")[2]):
                    part = part.strip(_TOKEN_STRIP)
                    if part:
                        candidates.append(_resolve(workdir, part))
    def allowed(path: str) -> bool:
        return any(_under(path, d) for d in (workdir, *own))

    for path in candidates:
        for root in protected:
            if _under(path, root) and not allowed(path):
                return root
    # An absolute protected path anywhere in a string argument, e.g. inside code.
    for text in _strings(args):
        for root in protected:
            for m in re.finditer(re.escape(root) + r"(?![\w.-])", text):
                if not allowed(_resolve(workdir, text[m.start():].split()[0])):
                    return root
    return None


def diagnose(
    log: dict,
    *,
    workdir: str | Path,
    protected: Iterable[str | Path],
    initial_dirs: Iterable[str | Path],
    complete: bool,
    own_dirs: Iterable[str | Path] = (),
) -> dict:
    """Return the diagnostics dict for one run's normalised log.

    workdir       the run's working directory; relative paths resolve against it
    protected     paths no call may name: the bench directory and the archive
    initial_dirs  directories present at run start, relative to the workdir or
                  absolute (snapshot_dirs gives this)
    complete      whether the project ended complete
    own_dirs      the run's own dirs outside the workdir (its run dir); naming
                  them is not contamination
    """
    work = os.path.normpath(os.path.abspath(str(workdir)))
    guarded = [os.path.normpath(os.path.abspath(str(p))) for p in protected]
    own = [os.path.normpath(os.path.abspath(str(p))) for p in own_dirs]
    known = {work} | {_resolve(work, str(d)) for d in initial_dirs}
    calls = [item for kind, item in events(log) if kind == "tool_call"]

    flags: list[dict] = []
    contamination: list[dict] = []
    last_test: dict | None = None
    streak_key: tuple | None = None
    streak: list[dict] = []

    def close_streak() -> None:
        if len(streak) >= REPEAT_LIMIT:
            flags.append({
                "flag": "repeated-failing-call",
                "call_id": streak[0]["id"],
                "tool": streak[0]["name"],
                "count": len(streak),
            })

    for call in calls:
        name = call["name"].lower()
        args = call["arguments"]

        root = _protected_hit(call, work, guarded, own)
        if root is not None:
            contamination.append({"call_id": call["id"], "tool": call["name"], "path": root})

        key = (call["name"], json.dumps(args, sort_keys=True))
        if call["is_error"] and key == streak_key:
            streak.append(call)
        else:
            close_streak()
            streak_key, streak = (key, [call]) if call["is_error"] else (None, [])

        if name in SHELL_TOOLS:
            command = next((args[k] for k in COMMAND_KEYS if isinstance(args.get(k), str)), "")
            known |= _mkdir_targets(command, work)
            if _PYTEST.search(command):
                last_test = call

        if name in WRITE_TOOLS:
            for raw in _path_args(args):
                target = _resolve(work, raw)
                parent = os.path.dirname(target)
                if not _under(target, work):
                    flags.append({"flag": "write-outside-workdir", "call_id": call["id"], "path": target})
                elif parent not in known:
                    flags.append({"flag": "write-missing-dir", "call_id": call["id"], "path": target})
                if not call["is_error"] and _under(parent, work):
                    while parent not in known and _under(parent, work):
                        known.add(parent)
                        parent = os.path.dirname(parent)
    close_streak()

    if complete and last_test is not None and last_test["is_error"]:
        flags.append({"flag": "complete-tests-failing", "call_id": last_test["id"]})

    return {
        "flags": flags,
        "contaminated": bool(contamination),
        "contamination": contamination,
    }

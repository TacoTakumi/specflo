"""The run lifecycle: one harness process tree per run, watched to one end reason, then removed.

A run starts in a sealed workdir that `workdir.make_workdir` seeded at the
arm's level. `run` then:

1. Writes the auto-run settings into the workdir's specflo config (`autonomy`
   and `auto_max_passes`) and reads them back, with the project level from
   `specflo status --json`. Both harnesses take them from there: the pi
   extension runs a bare `specflo auto --json`, and so does the Claude Code
   loop. The caller passes the same settings for every arm; the record carries
   them.
2. Builds the harness env: a bin dir holding only the specflo under test goes
   first on PATH (`SPECFLO_BIN` names it too), and the bench's own virtualenv
   is taken off: its bin dir leaves PATH and VIRTUAL_ENV, PYTHONPATH and
   PYTHONHOME are dropped. Under `uv run` that bin dir would otherwise give the
   agent the bench's python and packages. The agent gets the system python.
3. Starts the harness as a child in a new session (its own process group),
   stdout and stderr to `harness.out` and `harness.err` in the run dir:
   - pi: `pi --mode rpc` from `launch_pi`, sent one prompt on stdin,
     `/specflo-continue auto`, which starts the extension's anchored auto
     chain. stdin stays open, so pi stays up between passes.
   - claude-code: a small child python that runs `cc_continue.run` (one fresh
     `claude -p` per specflo pass) and writes `cc-result.json`. A child, not a
     thread, so the whole loop is one process tree that a cap can remove.
4. Watches it until one end condition holds, then removes the tree.

End reasons (the record's `end_reason`):

- complete: the specflo project reached complete. pi: `specflo status --json`
  says status complete; the run ends once pi's RPC stream says it settled
  (`agent_settled`), so the agent's last turn is not cut, or at a cap. Claude
  Code: `cc_continue` stopped on specflo's `project-complete`.
- escalated: specflo handed the run to a human. pi: the project's auto-run
  state file is marked ended or killed while the project is not complete (the
  extension shows the stop as an RPC notify, kept as the detail). Claude Code:
  any other specflo stop reason (stall, pass-cap, kill-switch, review-budget
  and so on), or the bench's own pass limit.
- timeout: the level's wall-clock cap expired (also Claude Code's own
  wall-clock end).
- stalled: no harness output and no model request for the level's stall
  limit. Activity is any change in size or mtime of the harness logs and of
  the harness's session files (pi's session JSONL; Claude Code's pass streams
  and session JSONL), plus any extra probe the caller passes (for example
  `file_growth` on the llama-swap log).
- harness-exit: the harness ended by itself with no specflo verdict, or the
  Claude Code loop ended with a failed pass or a failed `specflo auto`.

Cleanup: every run's harness env carries a unique `MODELBENCH_RUN` token. The
tree is the root's descendants (by parent pid), its process group and every
process whose environment holds the token, so a child that started its own
session or was orphaned is still found. Each is sent SIGTERM, then SIGKILL
after the grace, and the scan repeats until nothing is left. The record lists
any survivor.

herdr: when an adapter is given and herdr answers, the run gets a tab in the
bench workspace whose pane follows the harness logs (`tail -F --pid`), so the
operator can watch; the pane closes with the harness and the tab is closed at
cleanup. The bench keeps the harness as its own child, so end detection and
cleanup are the same with or without herdr. When herdr is unavailable, or
placement fails, the run proceeds headless with one stderr line. The record
says which: `placement` herdr with the pane id, or headless.

Level limits are read from the level's settings in the arm config
(`wall_clock` and `stall_limit`, seconds) unless passed; there is no default.
"""

from __future__ import annotations

import glob
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from specflo import auto as sf_auto
from specflo.agent.herdr import HerdrAdapter, HerdrError, HerdrPlacement
from specflo.projects import COMPLETE_STATUS

from modelbench import arms, cc_continue, launch_cc, launch_pi, record
from modelbench.workdir import REPO, find_specflo

END_COMPLETE = "complete"
END_ESCALATED = "escalated"
END_TIMEOUT = "timeout"
END_STALLED = "stalled"
END_HARNESS_EXIT = "harness-exit"
if {END_COMPLETE, END_ESCALATED, END_TIMEOUT, END_STALLED, END_HARNESS_EXIT} != set(record.END_REASONS):
    raise ImportError("lifecycle end reasons differ from the run record's")

PLACEMENT_HERDR = "herdr"
PLACEMENT_HEADLESS = "headless"

RUN_ENV = "MODELBENCH_RUN"
SPECFLO_BIN_ENV = "SPECFLO_BIN"
OUT_LOG = "harness.out"
ERR_LOG = "harness.err"
RECORD_FILE = "lifecycle.json"
CC_SPEC_FILE = "cc-spec.json"
CC_RESULT_FILE = "cc-result.json"

# The level settings keys in the arm config, in seconds.
WALL_CLOCK_KEY = "wall_clock"
STALL_KEY = "stall_limit"

# Seconds between checks, and the grace between SIGTERM and SIGKILL.
POLL = 2.0
STOP_GRACE = 10.0
SPECFLO_TIMEOUT = 60.0
HERDR_WORKSPACE = "modelbench"

# What the pi arm is sent once: the extension command that starts the anchored chain.
IGNITION = "/specflo-continue auto"

# Inherited variables that would point the agent at the bench's virtualenv.
DROPPED_VARS = ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME")

# Claude Code loop ends that are not specflo's: how each reads as a run end.
_CC_BENCH_ENDS = {
    cc_continue.END_PASS_LIMIT: END_ESCALATED,
    cc_continue.END_WALL_CLOCK: END_TIMEOUT,
    cc_continue.END_PASS_FAILED: END_HARNESS_EXIT,
    cc_continue.END_AUTO_FAILED: END_HARNESS_EXIT,
}


class LifecycleError(RuntimeError):
    """A run that must not start as asked."""


@dataclass(frozen=True)
class Ending:
    """One end condition: the record's reason, a detail line and the harness's own word for it."""

    reason: str
    detail: str
    harness_reason: str | None = None


@dataclass(frozen=True)
class Limits:
    wall_clock: float
    stall: float


@dataclass(frozen=True)
class AutoSettings:
    """The specflo auto settings every arm of a comparison runs with."""

    autonomy: str
    pass_cap: int

    def __post_init__(self) -> None:
        if self.autonomy not in sf_auto.AUTONOMY_LEVELS:
            raise LifecycleError(
                f"autonomy: unknown {self.autonomy!r} (allowed: {', '.join(sf_auto.AUTONOMY_LEVELS)})"
            )
        if isinstance(self.pass_cap, bool) or not isinstance(self.pass_cap, int) or self.pass_cap < 1:
            raise LifecycleError(f"pass_cap: expected a positive int, got {self.pass_cap!r}")


def level_limits(
    level: str,
    *,
    config: arms.Config | None = None,
    wall_clock: float | None = None,
    stall: float | None = None,
) -> Limits:
    """The level's wall-clock cap and stall limit: the arguments, else the level's settings."""
    spec = (config or arms.load_config()).levels.get(level)
    if spec is None:
        raise LifecycleError(f"levels.{level}: unknown level")
    values = {}
    for key, given in ((WALL_CLOCK_KEY, wall_clock), (STALL_KEY, stall)):
        value = given if given is not None else spec.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise LifecycleError(f"levels.{level}.{key}: missing or not a positive number of seconds")
        values[key] = float(value)
    return Limits(wall_clock=values[WALL_CLOCK_KEY], stall=values[STALL_KEY])


# -- specflo --------------------------------------------------------------------


def _specflo(specflo: str, args: Sequence[str], workdir: Path, env: Mapping[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [specflo, *args], cwd=workdir, env=dict(env), capture_output=True, text=True,
        timeout=SPECFLO_TIMEOUT, check=False,
    )


def status(specflo: str, workdir: Path, env: Mapping[str, str]) -> dict | None:
    """`specflo status --json` in the workdir, or None when it fails."""
    try:
        proc = _specflo(specflo, ["status", "--json"], workdir, env)
        data = json.loads(proc.stdout) if proc.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    return data if isinstance(data, dict) else None


def apply_auto_settings(
    specflo: str, workdir: Path, settings: AutoSettings, env: Mapping[str, str]
) -> dict[str, Any]:
    """Write the auto settings into the workdir's specflo config and read them back with the level."""
    values = {"autonomy": settings.autonomy, "auto_max_passes": str(settings.pass_cap)}
    for key, value in values.items():
        proc = _specflo(specflo, ["config", "set", key, value], workdir, env)
        if proc.returncode != 0:
            raise LifecycleError(f"specflo config set {key} {value} failed: {proc.stderr.strip()}")
    found = {}
    for key in values:
        proc = _specflo(specflo, ["config", "get", key], workdir, env)
        found[key] = proc.stdout.strip() if proc.returncode == 0 else None
    if found != values:
        raise LifecycleError(f"specflo config reads back {found}, not {values}")
    info = status(specflo, workdir, env) or {}
    return {"autonomy": settings.autonomy, "pass_cap": settings.pass_cap, "level": info.get("level")}


def run_state(info: Mapping[str, Any], workdir: Path) -> dict:
    """The project's auto-run state file, from the project dir `status --json` names; {} when absent."""
    project_dir = info.get("dir")
    if not isinstance(project_dir, str) or not project_dir:
        return {}
    path = Path(project_dir)
    path = (path if path.is_absolute() else workdir / path) / sf_auto.AUTO_RUN_STATE_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def specflo_verdict(
    specflo: str, workdir: Path, env: Mapping[str, str], notice: Callable[[], str | None] | None = None
) -> Ending | None:
    """complete or escalated as specflo states it, or None while the run goes on.

    `notice` gives the harness's last shown stop text, kept as the detail of an
    escalation.
    """
    info = status(specflo, workdir, env)
    if info is None:
        return None
    if info.get("status") == COMPLETE_STATUS:
        return Ending(END_COMPLETE, "the specflo project is complete", sf_auto.STOP_PROJECT_COMPLETE)
    state = run_state(info, workdir)
    if state.get("killed"):
        return Ending(END_ESCALATED, (notice and notice()) or "the auto-off kill switch is set",
                      sf_auto.STOP_KILL_SWITCH)
    if state.get("ended"):
        return Ending(END_ESCALATED, (notice and notice()) or "specflo ended the auto run")
    return None


# -- env ------------------------------------------------------------------------


def _venv_bin_dirs() -> set[str]:
    dirs = {os.path.normpath(REPO / ".venv" / "bin")}
    if sys.prefix != sys.base_prefix:
        dirs.add(os.path.normpath(Path(sys.prefix) / "bin"))
        dirs.add(os.path.normpath(os.path.dirname(sys.executable)))
    return dirs | {os.path.realpath(d) for d in dirs}


def harness_env(base_env: Mapping[str, str] | None, bindir: Path) -> tuple[dict[str, str], dict[str, Any]]:
    """The env a harness starts with, and what was changed, for the record."""
    env = dict(os.environ if base_env is None else base_env)
    vars_dropped = [name for name in DROPPED_VARS if env.pop(name, None) is not None]
    venv = _venv_bin_dirs()
    kept, path_dropped = [], []
    for part in env.get("PATH", "").split(os.pathsep):
        if not part:
            continue
        if os.path.normpath(part) in venv or os.path.realpath(part) in venv:
            path_dropped.append(part)
        else:
            kept.append(part)
    env["PATH"] = os.pathsep.join([str(bindir), *kept])
    env[SPECFLO_BIN_ENV] = str(bindir / "specflo")
    return env, {"path_first": str(bindir), "path_dropped": path_dropped, "vars_dropped": vars_dropped}


# -- the process tree -----------------------------------------------------------


def _stat(pid: int) -> tuple[int, int, str] | None:
    """(ppid, pgrp, state) of a live process, or None."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    fields = text[text.rfind(")") + 2:].split()
    return int(fields[1]), int(fields[2]), fields[0]


def _has_token(pid: int, marker: bytes) -> bool:
    try:
        return marker in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return False


def alive(pid: int) -> bool:
    """A process that exists and is not a zombie."""
    info = _stat(pid)
    return info is not None and info[2] not in ("Z", "X")


def tree_pids(root: int, token: str, *, root_live: bool = True) -> set[int]:
    """The live processes of a run: root's descendants, its process group and every holder of the token.

    `root_live` False (the root was reaped, so its pid may be reused) leaves out
    the root and the walk from it; the group and the token still find the rest.
    """
    marker = f"{RUN_ENV}={token}".encode()
    me = os.getpid()
    children: dict[int, list[int]] = {}
    found: set[int] = set()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        info = _stat(pid)
        if info is None or pid == me:
            continue
        ppid, pgrp, state = info
        if state in ("Z", "X"):
            continue
        children.setdefault(ppid, []).append(pid)
        if (root_live and pid == root) or pgrp == root or _has_token(pid, marker):
            found.add(pid)
    queue = [*found, root] if root_live else list(found)
    while queue:
        for child in children.get(queue.pop(), []):
            if child not in found:
                found.add(child)
                queue.append(child)
    return {pid for pid in found if alive(pid)}


def _signal(pids: set[int], sig: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, sig)
        except OSError:
            pass


def stop_tree(proc: subprocess.Popen, token: str, grace: float = STOP_GRACE, rounds: int = 5) -> list[int]:
    """Remove the run's whole tree: SIGTERM, SIGKILL after `grace`; returns the pids still left."""
    if proc.stdin:
        try:
            proc.stdin.close()
        except OSError:
            pass

    def scan() -> set[int]:
        live = proc.poll() is None
        return tree_pids(proc.pid, token, root_live=live)

    def wait_gone(pids: set[int], seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end and any(alive(p) for p in pids):
            proc.poll()
            time.sleep(0.05)

    for _ in range(rounds):
        pids = scan()
        if not pids:
            break
        _signal(pids, signal.SIGTERM)
        wait_gone(pids, grace)
        pids = scan()
        _signal(pids, signal.SIGKILL)
        wait_gone(pids, 5.0)
    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        pass
    return sorted(scan())


# -- activity -------------------------------------------------------------------


def file_growth(path: Path | str) -> Callable[[], object]:
    """An activity probe over one file (for example the llama-swap log): its size and mtime."""
    def probe() -> object:
        try:
            st = os.stat(path)
        except OSError:
            return None
        return (st.st_size, st.st_mtime_ns)
    return probe


class Activity:
    """Whether anything the run is judged on moved since the last look."""

    def __init__(self, globs: Sequence[str], probes: Sequence[Callable[[], object]] = ()) -> None:
        self.globs = list(globs)
        self.probes = list(probes)
        self.last = self._signature()

    def _signature(self) -> tuple:
        files = []
        for pattern in self.globs:
            for name in glob.glob(pattern, recursive=True):
                try:
                    st = os.stat(name)
                except OSError:
                    continue
                files.append((name, st.st_size, st.st_mtime_ns))
        return (tuple(sorted(files)), tuple(probe() for probe in self.probes))

    def changed(self) -> bool:
        current = self._signature()
        moved, self.last = current != self.last, current
        return moved


class RpcEvents:
    """pi's RPC stdout, read as it grows: whether the agent is working, and the last notice shown."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.offset = 0
        self.partial = b""
        self.busy = False
        self.notice: str | None = None

    def update(self) -> None:
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self.offset)
                data = fh.read()
        except OSError:
            return
        self.offset += len(data)
        lines = (self.partial + data).split(b"\n")
        self.partial = lines.pop()
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "agent_start":
                self.busy = True
            elif kind == "agent_settled":
                self.busy = False
            elif (kind == "extension_ui_request" and event.get("method") == "notify"
                  and isinstance(event.get("message"), str)):
                self.notice = event["message"]

    def idle(self) -> bool:
        self.update()
        return not self.busy

    def last_notice(self) -> str | None:
        self.update()
        return self.notice


# -- herdr ----------------------------------------------------------------------


class Herdr(Protocol):
    def available(self) -> bool: ...
    def ensure_workspace(self, label: str) -> str: ...
    def create_tab(self, workspace_id: str, label: str, cwd: str, env: dict[str, str] | None = None) -> HerdrPlacement: ...
    def run_in_pane(self, pane_id: str, command: str) -> None: ...
    def close_tab(self, tab_id: str) -> None: ...


def pane_command(pid: int, logs: Sequence[Path]) -> str:
    """What the pane runs: follow the harness logs until the harness exits; the pane dies with it."""
    return "exec tail -n +1 -F --pid={} {}".format(pid, " ".join(shlex.quote(str(p)) for p in logs))


def place(
    herdr: Herdr | None, label: str, cwd: Path, command: str, workspace: str = HERDR_WORKSPACE
) -> tuple[HerdrPlacement | None, str | None]:
    """A herdr pane running `command`, or (None, why) when the run goes headless."""
    if herdr is None:
        return None, "herdr not used"
    placement = None
    try:
        if not herdr.available():
            return None, "herdr unavailable"
        placement = herdr.create_tab(herdr.ensure_workspace(workspace), label, str(cwd))
        herdr.run_in_pane(placement.pane_id, command)
        return placement, None
    except (HerdrError, KeyError, TypeError) as exc:  # a malformed answer is a failed placement too
        if placement is not None:
            _close(herdr, placement)
        return None, f"herdr placement failed: {exc}"


def _close(herdr: Herdr, placement: HerdrPlacement) -> None:
    try:
        herdr.close_tab(placement.tab_id)
    except HerdrError:
        pass  # the pane closes itself with the harness; this is a backstop


# -- supervision ----------------------------------------------------------------


@dataclass
class HarnessSpec:
    """One harness start and how its run ends."""

    harness: str
    argv: list[str]
    env: dict[str, str] = field(repr=False)
    cwd: Path
    stdin_text: str | None = None
    activity_globs: tuple[str, ...] = ()
    verdict: Callable[[], Ending | None] | None = None
    idle: Callable[[], bool] | None = None
    on_exit: Callable[[int], Ending | None] | None = None
    report: Callable[[], dict] | None = None


@dataclass
class Outcome:
    end_reason: str
    end_detail: str
    harness_end_reason: str | None
    placement: str
    pane_id: str | None
    placement_note: str | None
    started_at: str
    ended_at: str
    duration: float
    pid: int
    exit_code: int | None
    survivors: list[int]
    stdout_log: str
    stderr_log: str
    wall_clock_cap: float
    stall_limit: float
    harness_report: dict = field(default_factory=dict)
    level: str | None = None
    autonomy: str | None = None
    pass_cap: int | None = None
    env: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def record_fields(self) -> dict[str, Any]:
        """The run record fields this run supplies; `lifecycle` holds the rest of the outcome."""
        lifecycle = {k: v for k, v in self.to_dict().items()
                     if k not in ("end_reason", "started_at", "ended_at", "level", "autonomy", "pass_cap")}
        return {
            "level": self.level,
            "end_reason": self.end_reason,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "settings": {"autonomy": self.autonomy, "pass_cap": self.pass_cap},
            "logs": {"harness_stdout": self.stdout_log, "harness_stderr": self.stderr_log},
            "lifecycle": lifecycle,
        }


def _now() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="milliseconds")


def supervise(
    spec: HarnessSpec,
    *,
    run_dir: Path | str,
    limits: Limits,
    herdr: Herdr | None = None,
    label: str | None = None,
    poll: float = POLL,
    stop_grace: float = STOP_GRACE,
    probes: Sequence[Callable[[], object]] = (),
) -> Outcome:
    """Start the harness, watch it to one end condition, remove its tree and report."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    out_path, err_path = run_dir / OUT_LOG, run_dir / ERR_LOG
    token = uuid.uuid4().hex
    env = {**spec.env, RUN_ENV: token}
    started_at, t0 = _now(), time.monotonic()
    with open(out_path, "wb") as out, open(err_path, "wb") as err:
        proc = subprocess.Popen(
            spec.argv, env=env, cwd=spec.cwd, stdout=out, stderr=err, start_new_session=True,
            stdin=subprocess.PIPE if spec.stdin_text is not None else subprocess.DEVNULL,
        )
    if spec.stdin_text is not None:
        try:
            proc.stdin.write(spec.stdin_text.encode())
            proc.stdin.flush()
        except OSError:
            pass  # a harness that died at once is left to the watch
    placement: HerdrPlacement | None = None
    deadline = t0 + limits.wall_clock
    last_active = t0
    pending: Ending | None = None  # complete, waiting for the harness to settle
    exit_code: int | None = None
    try:
        placement, note = place(herdr, label or run_dir.name, spec.cwd,
                                pane_command(proc.pid, [out_path, err_path]))
        if placement is None and herdr is not None:
            print(f"modelbench: {note}; the run is headless", file=sys.stderr)
        activity = Activity([str(out_path), str(err_path), *spec.activity_globs], probes)
        while True:
            now = time.monotonic()
            if activity.changed():
                last_active = now
            code = proc.poll()
            ending = None
            if pending is None and spec.verdict is not None:
                verdict = spec.verdict()
                if verdict is not None:
                    if verdict.reason == END_COMPLETE and code is None and spec.idle and not spec.idle():
                        pending = verdict
                    else:
                        ending = verdict
            elif pending is not None and (code is not None or spec.idle is None or spec.idle()):
                ending = pending
            if ending is None and code is not None:
                ending = (spec.on_exit(code) if spec.on_exit else None) or Ending(
                    END_HARNESS_EXIT, f"the harness exited with code {code}")
            if ending is None and now >= deadline:
                ending = pending or Ending(END_TIMEOUT, f"the wall-clock cap of {limits.wall_clock:g} s expired")
            if ending is None and now - last_active >= limits.stall:
                ending = pending or Ending(
                    END_STALLED, f"no harness output and no model request for {limits.stall:g} s")
            if ending is not None:
                exit_code = code
                break
            time.sleep(poll)
    finally:
        ended_at, t1 = _now(), time.monotonic()
        survivors = stop_tree(proc, token, stop_grace)
        if placement is not None:
            _close(herdr, placement)
    report = {}
    if spec.report is not None:
        try:
            report = spec.report()
        except Exception as exc:  # noqa: BLE001 - the report must not lose the run's ending
            report = {"error": str(exc)}
    return Outcome(
        end_reason=ending.reason, end_detail=ending.detail, harness_end_reason=ending.harness_reason,
        placement=PLACEMENT_HERDR if placement else PLACEMENT_HEADLESS,
        pane_id=placement.pane_id if placement else None,
        placement_note=note, started_at=started_at, ended_at=ended_at,
        duration=round(t1 - t0, 3), pid=proc.pid,
        exit_code=exit_code if exit_code is not None else proc.returncode,
        survivors=survivors, stdout_log=str(out_path), stderr_log=str(err_path),
        wall_clock_cap=limits.wall_clock, stall_limit=limits.stall, harness_report=report,
    )


# -- pi -------------------------------------------------------------------------


def ignition() -> str:
    """The one RPC command the pi arm gets: the extension command that starts the auto chain."""
    return json.dumps({"type": "prompt", "id": "ignition", "message": IGNITION}) + "\n"


def pi_spec(launch: launch_pi.PiLaunch, specflo: str, run_dir: Path) -> HarnessSpec:
    """The pi arm's harness: pi in RPC mode, judged by specflo's state and pi's event stream."""
    events = RpcEvents(Path(run_dir) / OUT_LOG)
    return HarnessSpec(
        harness="pi",
        argv=list(launch.argv),
        env=dict(launch.env),
        cwd=launch.cwd,
        stdin_text=ignition(),
        activity_globs=(str(launch.agent_dir / "sessions" / "**" / "*.jsonl"),),
        verdict=lambda: specflo_verdict(specflo, launch.cwd, launch.env, events.last_notice),
        idle=events.idle,
        report=launch.report,
    )


def build_pi(arm: arms.Run, *, workdir: Path, run_dir: Path, specflo: str, env: dict[str, str],
             limits: Limits, settings: AutoSettings) -> HarnessSpec:
    launch = launch_pi.build_launch(arm.entry, workdir=workdir, run_dir=run_dir,
                                    args=["--mode", "rpc"], base_env=env)
    return pi_spec(launch, specflo, run_dir)


# -- claude code ----------------------------------------------------------------


def cc_ending(result: Mapping[str, Any]) -> Ending:
    """A Claude Code loop result (cc-result.json) as a run end."""
    if "error" in result:
        return Ending(END_HARNESS_EXIT, f"the Claude Code loop failed: {result['error']}")
    loop = result.get("result") or {}
    reason, detail = loop.get("end_reason"), str(loop.get("end_detail") or "")
    if reason == sf_auto.STOP_PROJECT_COMPLETE:
        return Ending(END_COMPLETE, detail, reason)
    if reason in _CC_BENCH_ENDS:
        return Ending(_CC_BENCH_ENDS[reason], detail, reason)
    if reason in sf_auto.STOP_REASONS:
        return Ending(END_ESCALATED, detail, reason)
    return Ending(END_HARNESS_EXIT, f"the Claude Code loop ended with {reason!r}", reason)


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


_CC_BOOT = (
    "import sys; sys.path.insert(0, {bench!r}); "
    "from modelbench.lifecycle import cc_child_main; sys.exit(cc_child_main(sys.argv[1]))"
)


def cc_spec(spec_file: Path, env: dict[str, str], run_dir: Path) -> HarnessSpec:
    """The Claude Code arm's harness: a child running the pass loop the spec file describes."""
    run_dir = Path(run_dir)
    result_file = run_dir / CC_RESULT_FILE

    def on_exit(code: int) -> Ending | None:
        result = _read_json(result_file)
        if result is None:
            return Ending(END_HARNESS_EXIT, f"the Claude Code loop exited with code {code} and no result")
        return cc_ending(result)

    return HarnessSpec(
        harness="claude-code",
        argv=[sys.executable, "-c", _CC_BOOT.format(bench=str(launch_pi.BENCH)), str(spec_file)],
        env=dict(env),
        cwd=run_dir,  # not the workdir: `python -c` puts its cwd on sys.path
        activity_globs=(
            str(run_dir / "pass-*.stream.jsonl"),
            str(run_dir / "pass-*.stderr"),
            str(run_dir / launch_cc.CONFIG_DIR_NAME / "projects" / "**" / "*.jsonl"),
        ),
        on_exit=on_exit,
        report=lambda: (_read_json(result_file) or {}).get("launch", {}),
    )


def build_cc(arm: arms.Run, *, workdir: Path, run_dir: Path, specflo: str, env: dict[str, str],
             limits: Limits, settings: AutoSettings) -> HarnessSpec:
    # specflo's own cap stops the run first: it stops on its cap-th auto call,
    # after cap - 1 passes, so the bench's process cap never binds before it.
    spec_file = Path(run_dir) / CC_SPEC_FILE
    spec_file.write_text(json.dumps({
        "entry": arm.entry, "workdir": str(workdir), "run_dir": str(run_dir), "specflo": specflo,
        "max_passes": settings.pass_cap, "wall_clock": limits.wall_clock,
    }, indent=2) + "\n", encoding="utf-8")
    return cc_spec(spec_file, env, Path(run_dir))


def _follow_streams(run_dir: Path, stop: threading.Event, every: float = 0.5) -> None:
    """Copy each pass's stream log to stdout as it grows, so the harness log (and a pane) shows it."""
    offsets: dict[str, int] = {}
    while not stop.wait(every):
        for name in sorted(glob.glob(str(run_dir / "pass-*.stream.jsonl"))):
            try:
                with open(name, "rb") as fh:
                    fh.seek(offsets.get(name, 0))
                    data = fh.read()
            except OSError:
                continue
            if data:
                offsets[name] = offsets.get(name, 0) + len(data)
                sys.stdout.buffer.write(data)
                sys.stdout.flush()


def cc_child_main(spec_path: str) -> int:
    """The Claude Code harness process: run the pass loop, write cc-result.json."""
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    run_dir = Path(spec["run_dir"])
    stop = threading.Event()
    threading.Thread(target=_follow_streams, args=(run_dir, stop), daemon=True).start()

    def on_pass(rec: cc_continue.PassRecord) -> None:
        print(f"modelbench: pass {rec.index} ended by {rec.ended_by} (exit {rec.exit_code})", flush=True)

    try:
        result, report = cc_continue.run(
            spec["entry"], workdir=spec["workdir"], run_dir=run_dir, max_passes=spec["max_passes"],
            wall_clock=spec["wall_clock"], specflo=spec["specflo"], base_env=dict(os.environ),
            on_pass=on_pass,
        )
        payload: dict[str, Any] = {"result": result.to_dict(), "launch": report}
    except Exception as exc:  # noqa: BLE001 - any failure is the loop's result, recorded
        payload = {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        stop.set()
    (run_dir / CC_RESULT_FILE).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return 0 if "result" in payload else 1


# -- the run --------------------------------------------------------------------

Builder = Callable[..., HarnessSpec]
BUILDERS: dict[str, Builder] = {"pi": build_pi, "claude-code": build_cc}

_HERDR_DEFAULT: Any = object()


def run(
    arm: arms.Run,
    *,
    workdir: Path | str,
    run_dir: Path | str,
    settings: AutoSettings,
    limits: Limits,
    specflo: str | None = None,
    herdr: Herdr | None = _HERDR_DEFAULT,
    base_env: Mapping[str, str] | None = None,
    poll: float = POLL,
    stop_grace: float = STOP_GRACE,
    probes: Sequence[Callable[[], object]] = (),
    builders: Mapping[str, Builder] | None = None,
) -> Outcome:
    """Run one harness on a sealed workdir to one end reason; writes run_dir/lifecycle.json.

    `herdr` defaults to the real herdr CLI; None runs headless. `builders` maps
    a harness to the function that builds its start (tests pass stand-ins).
    """
    workdir, run_dir = Path(workdir).resolve(), Path(run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    specflo = specflo or find_specflo()
    bindir = cc_continue.specflo_bin_dir(run_dir, specflo)
    linked = str(bindir / "specflo")
    env, env_report = harness_env(base_env, bindir)
    confirmed = apply_auto_settings(linked, workdir, settings, env)
    if confirmed["level"] != arm.level:
        raise LifecycleError(f"the workdir's specflo project is at level {confirmed['level']!r}, not {arm.level!r}")
    build = (builders or BUILDERS)[arm.harness]
    spec = build(arm, workdir=workdir, run_dir=run_dir, specflo=linked, env=env, limits=limits, settings=settings)
    outcome = supervise(
        spec, run_dir=run_dir, limits=limits,
        herdr=HerdrAdapter() if herdr is _HERDR_DEFAULT else herdr,
        label=f"{arm.harness} {arm.level} {arm.run_index} {arm.entry}",
        poll=poll, stop_grace=stop_grace, probes=probes,
    )
    outcome.level = confirmed["level"]
    outcome.autonomy = confirmed["autonomy"]
    outcome.pass_cap = confirmed["pass_cap"]
    outcome.env = env_report
    (run_dir / RECORD_FILE).write_text(json.dumps(outcome.to_dict(), indent=2) + "\n", encoding="utf-8")
    return outcome

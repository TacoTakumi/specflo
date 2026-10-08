"""The Claude Code arm's continuation: one fresh `claude -p` process per specflo pass.

specflo's `specflo auto` only emits a payload; clearing context and reseeding is
the outer harness's job. For pi that harness is specflo's pi extension. For
Claude Code a human would type `/clear` at a seam, which a headless `claude -p`
cannot do, so the bench is the outer loop and a new process is the clear:

1. Run `specflo auto --json` in the workdir. When it says stop, the run ends
   with specflo's reason (project-complete, stall, pass-cap and so on).
2. Otherwise start a fresh `claude -p` (a new session id, so a fresh context)
   with the pass's payload as the prompt, verbatim, on stdin.
3. While it runs, watch its stream-json output. After each tool result, when
   the context in use has reached specflo's `context_threshold_percent` of the
   context window (the arming rule of the pi extension), poll
   `specflo status --json`; a seam (the phase changed, or a task reached done)
   while an auto run is under way ends the process, as the pi extension aborts
   its agent at an armed seam. A process that ends by itself also ends the
   pass.
4. Repeat, until specflo says stop or a bench limit is hit.

Alternatives weighed:

- One long `claude -p` session relying on Claude Code's own compaction: a
  compaction is a summary, not a clear, so no specflo seam ever gets a fresh
  context; specflo's SessionStart hook does not fire on compact; and a headless
  session ends at the model's first turn end with nobody to say "go on".
- `--resume` / `--continue`: they bring the old conversation back, which is the
  opposite of a clear, and the hook's `resume` source injects the ask-first
  directive.
- An interactive `claude` in a terminal, sent `/clear` as keystrokes: the real
  clear, but it needs screen scraping, and after `/clear` the hook's ask-first
  directive makes the agent wait for a reply that only a scripted replier could
  give.

All passes of a run share one launch: one run copy of the config dir (so every
session JSONL lands under one CLAUDE_CONFIG_DIR/projects/), one request shim
and one env. Each pass gets its own `--session-id`, its stream-json log and its
stderr log in the run directory. The pass record lists them with each pass's
exit code, how it ended and the phase and done count before and after.

Run end reasons: specflo's stop reason when `specflo auto` stopped the run, or
one of the bench's own: `pass-limit` (the bench's cap on Claude Code
processes), `wall-clock`, `pass-failed` (a pass exited non-zero or with an
error result, and the bench did not end it) and `auto-failed` (`specflo auto
--json` exited non-zero or printed no readable report).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, TextIO

from modelbench import launch_cc, launch_pi
from modelbench.workdir import find_specflo

END_PASS_LIMIT = "pass-limit"
END_WALL_CLOCK = "wall-clock"
END_PASS_FAILED = "pass-failed"
END_AUTO_FAILED = "auto-failed"

# How a pass ended: the bench ended it at an armed seam, the process exited by
# itself, or the run's wall clock ran out.
ENDED_SEAM = "seam"
ENDED_EXIT = "exit"
ENDED_TIMEOUT = "timeout"

PASS_ARGS = ("-p", "--output-format", "stream-json", "--verbose")
RECORD_FILE = "passes.json"
# The launch report, written before the first pass so a run the bench kills still has it.
LAUNCH_FILE = "cc-launch.json"
BIN_DIR = "bin"

# Seconds a specflo call may take, and the grace a pass gets after SIGTERM.
SPECFLO_TIMEOUT = 120.0
STOP_GRACE = 30.0
# The final result text kept in the pass record, from its end.
RESULT_TAIL = 2000


@dataclass(frozen=True)
class Snapshot:
    """What a seam is judged on, from `specflo status --json`: phase and tasks done."""

    phase: str | None
    done: int | None
    threshold: int | None = None
    under_way: bool = False


@dataclass
class PassRecord:
    index: int
    session_id: str
    session_file: str | None
    stream_log: str
    stderr_log: str
    started: float
    ended: float = 0.0
    exit_code: int | None = None
    ended_by: str = ""
    seam: str | None = None
    phase_before: str | None = None
    phase_after: str | None = None
    done_before: int | None = None
    done_after: int | None = None
    context_tokens: int = 0
    result_subtype: str | None = None
    result_is_error: bool | None = None
    result_text: str | None = None

    @property
    def failed(self) -> bool:
        """An exit the bench did not cause, with a non-zero code or an error result."""
        if self.ended_by != ENDED_EXIT:
            return False
        return self.exit_code != 0 or self.result_is_error is True or self.result_subtype is None


@dataclass
class RunResult:
    end_reason: str
    end_detail: str
    passes: list[PassRecord] = field(default_factory=list)

    def session_ids(self) -> list[str]:
        return [p.session_id for p in self.passes]

    def to_dict(self) -> dict[str, Any]:
        return {
            "end_reason": self.end_reason,
            "end_detail": self.end_detail,
            "context_passes": len(self.passes),
            "passes": [asdict(p) for p in self.passes],
        }


# -- specflo ------------------------------------------------------------------


def _specflo(
    specflo: str, args: Sequence[str], workdir: Path, env: Mapping[str, str]
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [specflo, *args], cwd=workdir, env=dict(env), capture_output=True, text=True,
        timeout=SPECFLO_TIMEOUT, check=False,
    )


def auto_pass(
    specflo: str, workdir: Path, env: Mapping[str, str], extra: Sequence[str] = ()
) -> dict | None:
    """One `specflo auto --json` report ({"payload", "stop", "reason"}), or None when unreadable."""
    try:
        proc = _specflo(specflo, ["auto", "--json", *extra], workdir, env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        report = json.loads(proc.stdout)
    except ValueError:
        return None
    if not isinstance(report, dict) or not isinstance(report.get("payload"), str):
        return None
    return {"payload": report["payload"], "stop": report.get("stop") is True,
            "reason": report.get("reason"), "stderr": proc.stderr}


def snapshot(specflo: str, workdir: Path, env: Mapping[str, str]) -> Snapshot | None:
    """The seam fields of `specflo status --json`, or None when the poll fails."""
    try:
        proc = _specflo(specflo, ["status", "--json"], workdir, env)
        data = json.loads(proc.stdout) if proc.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    done = (data.get("progress") or {}).get("done") if isinstance(data.get("progress"), dict) else None
    threshold = data.get("context_threshold_percent")
    return Snapshot(
        phase=data.get("phase") if isinstance(data.get("phase"), str) else None,
        done=done if isinstance(done, int) and not isinstance(done, bool) else None,
        threshold=threshold if isinstance(threshold, int) and not isinstance(threshold, bool) else None,
        under_way=(data.get("auto_run") or {}).get("under_way") is True,
    )


def describe_seam(last: Snapshot, current: Snapshot) -> str | None:
    """The pi extension's seam rule: a phase change, or more tasks done; None when no seam."""
    if current.phase != last.phase:
        return f"the phase is now {current.phase}" if current.phase else "the phase changed"
    if current.done is not None and last.done is not None and current.done > last.done:
        return f"a task reached done ({current.done} done)"
    return None


# -- context use ----------------------------------------------------------------


def context_window(entry: str) -> int:
    """The entry's context window, as the pi arm's models.json gives it (both arms share it)."""
    models = json.loads((launch_pi.FROZEN_DIR / launch_pi.MODELS_FILE).read_text(encoding="utf-8"))
    for provider in models.get("providers", {}).values():
        for model in provider.get("models", []):
            if model.get("id") == entry and isinstance(model.get("contextWindow"), int):
                return model["contextWindow"]
    raise launch_pi.LaunchError(f"no contextWindow for {entry!r} in the pi arm's models.json")


def context_tokens(event: dict) -> int | None:
    """Prompt tokens of a main-conversation assistant event (input, cache write and cache read)."""
    if event.get("type") != "assistant" or event.get("parent_tool_use_id"):
        return None
    usage = (event.get("message") or {}).get("usage")
    if not isinstance(usage, dict):
        return None
    return sum(int(usage.get(k) or 0) for k in
               ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))


def armed(tokens: int, window: int, threshold: int | None) -> bool:
    """Whether context use has reached the threshold; an unknown threshold never arms."""
    if threshold is None or window <= 0:
        return False
    return tokens * 100 >= threshold * window


def _is_tool_result(event: dict) -> bool:
    if event.get("type") != "user" or event.get("parent_tool_use_id"):
        return False
    content = (event.get("message") or {}).get("content")
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    )


# -- one pass -------------------------------------------------------------------


def session_file(config_dir: Path, session_id: str) -> Path | None:
    """The session JSONL Claude Code wrote for `session_id` under the run copy, if any."""
    found = sorted((config_dir / "projects").glob(f"*/{session_id}.jsonl"))
    return found[0] if found else None


def _stop(proc: subprocess.Popen) -> None:
    """SIGTERM, then SIGKILL after the grace."""
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=STOP_GRACE)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def run_pass(
    launch: launch_cc.CcLaunch,
    prompt: str,
    *,
    index: int,
    run_dir: Path,
    specflo: str,
    window: int,
    deadline: float,
    threshold_percent: int | None = None,
) -> PassRecord:
    """Run one fresh `claude -p` on `prompt`; end it at an armed seam or the deadline."""
    session_id = str(uuid.uuid4())
    stream_log = run_dir / f"pass-{index:02d}.stream.jsonl"
    stderr_log = run_dir / f"pass-{index:02d}.stderr"
    before = snapshot(specflo, launch.cwd, launch.env)
    record = PassRecord(
        index=index, session_id=session_id, session_file=None,
        stream_log=str(stream_log), stderr_log=str(stderr_log), started=time.time(),
        phase_before=before.phase if before else None,
        done_before=before.done if before else None,
    )
    threshold = threshold_percent if threshold_percent is not None else (before.threshold if before else None)
    last = before
    pass_launch = replace(launch, argv=[*launch.argv, *PASS_ARGS, "--session-id", session_id])
    timed_out = threading.Event()
    with open(stream_log, "w", encoding="utf-8") as out, open(stderr_log, "w", encoding="utf-8") as err:
        proc = launch_cc.start(pass_launch, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=err, text=True, bufsize=1)

        def on_deadline() -> None:
            timed_out.set()
            _stop(proc)

        timer = threading.Timer(max(0.0, deadline - time.monotonic()), on_deadline)
        timer.daemon = True
        timer.start()
        try:
            _feed(proc, prompt)
            _watch(proc, out, record, specflo, launch, window, threshold, last)
            proc.wait()
        finally:
            timer.cancel()
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    record.ended = time.time()
    record.exit_code = proc.returncode
    record.ended_by = ENDED_SEAM if record.seam else ENDED_TIMEOUT if timed_out.is_set() else ENDED_EXIT
    after = snapshot(specflo, launch.cwd, launch.env)
    record.phase_after = after.phase if after else None
    record.done_after = after.done if after else None
    found = session_file(launch.config_dir, record.session_id)
    record.session_file = str(found) if found else None
    return record


def _feed(proc: subprocess.Popen, prompt: str) -> None:
    """Write the prompt to stdin and close it; a process that already died is left to the watcher."""
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass


def _watch(
    proc: subprocess.Popen,
    out: TextIO,
    record: PassRecord,
    specflo: str,
    launch: launch_cc.CcLaunch,
    window: int,
    threshold: int | None,
    last: Snapshot | None,
) -> None:
    """Copy the stream to `out` and fill `record`; at an armed seam, set `record.seam` and stop the process.

    The stream is read to its end after the stop too, so the process never
    blocks on a full pipe and its last lines reach the log.
    """
    tokens = 0
    for line in proc.stdout:
        out.write(line)
        out.flush()
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        used = context_tokens(event)
        if used is not None:
            tokens = used
            record.context_tokens = max(record.context_tokens, used)
        if event.get("type") == "system" and event.get("subtype") == "init":
            if event.get("session_id") and event["session_id"] != record.session_id:
                record.session_id = event["session_id"]
        elif event.get("type") == "result":
            record.result_subtype = event.get("subtype")
            record.result_is_error = bool(event.get("is_error"))
            text = event.get("result")
            record.result_text = text[-RESULT_TAIL:] if isinstance(text, str) else None
        elif record.seam is None and _is_tool_result(event) and armed(tokens, window, threshold):
            current = snapshot(specflo, launch.cwd, launch.env)
            if current is None:
                continue  # a failed poll declares nothing and keeps the baseline
            seam = describe_seam(last, current) if last is not None else None
            last = current
            if seam is not None and current.under_way:
                record.seam = seam
                threading.Thread(target=_stop, args=(proc,), daemon=True).start()


# -- the run --------------------------------------------------------------------


def run_passes(
    launch: launch_cc.CcLaunch,
    *,
    run_dir: Path | str,
    specflo: str,
    window: int,
    max_passes: int,
    wall_clock: float,
    threshold_percent: int | None = None,
    auto_args: Sequence[str] = (),
    on_pass: Callable[[PassRecord], None] | None = None,
) -> RunResult:
    """Loop specflo passes over fresh Claude Code processes until specflo or a bench limit stops it.

    `threshold_percent` overrides the arming threshold specflo reports (0 arms
    at once: every seam ends the pass). The pass record is also written to
    `run_dir/passes.json` after every pass.
    """
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wall_clock
    result = RunResult(end_reason="", end_detail="")

    def save() -> None:
        (run_dir / RECORD_FILE).write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")

    while True:
        if time.monotonic() >= deadline:
            result.end_reason, result.end_detail = END_WALL_CLOCK, f"wall clock of {wall_clock} s ran out"
            break
        report = auto_pass(specflo, launch.cwd, launch.env, list(auto_args))
        if report is None:
            result.end_reason, result.end_detail = END_AUTO_FAILED, "specflo auto --json gave no readable report"
            break
        if report["stop"]:
            result.end_reason, result.end_detail = str(report["reason"]), report["payload"]
            break
        if len(result.passes) >= max_passes:
            result.end_reason, result.end_detail = END_PASS_LIMIT, f"bench cap of {max_passes} passes reached"
            break
        record = run_pass(
            launch, report["payload"], index=len(result.passes) + 1, run_dir=run_dir,
            specflo=specflo, window=window, deadline=deadline, threshold_percent=threshold_percent,
        )
        result.passes.append(record)
        save()
        if on_pass is not None:
            on_pass(record)
        if record.ended_by == ENDED_TIMEOUT:
            result.end_reason, result.end_detail = END_WALL_CLOCK, f"wall clock of {wall_clock} s ran out"
            break
        if record.failed:
            result.end_reason = END_PASS_FAILED
            result.end_detail = (f"pass {record.index} exited {record.exit_code}"
                                 f" ({record.result_subtype or 'no result'})")
            break
    save()
    return result


def specflo_bin_dir(run_dir: Path, specflo: str) -> Path:
    """A bin dir holding only `specflo` (a link to the one under test), to put first on PATH.

    Linking keeps the rest of that specflo's bin dir (its venv's python) off
    the agent's PATH.
    """
    bindir = Path(run_dir) / BIN_DIR
    bindir.mkdir(parents=True, exist_ok=True)
    # Resolve before unlinking: `specflo` may be this very link, from an earlier call.
    target = Path(specflo).resolve()
    link = bindir / "specflo"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(target)
    return bindir


def run(
    entry: str,
    *,
    workdir: Path | str,
    run_dir: Path | str,
    max_passes: int,
    wall_clock: float,
    specflo: str | None = None,
    threshold_percent: int | None = None,
    base_env: Mapping[str, str] | None = None,
    base_url: str | None = None,
    on_pass: Callable[[PassRecord], None] | None = None,
) -> tuple[RunResult, dict[str, str]]:
    """Build one launch for the run and loop its passes; returns the result and the launch report."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    specflo = specflo or find_specflo()
    bindir = specflo_bin_dir(run_dir, specflo)
    env = dict(os.environ if base_env is None else base_env)
    path = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    with launch_cc.build_launch(
        entry, workdir=workdir, run_dir=run_dir, base_env=env, base_url=base_url,
        extra_env={"PATH": path},
    ) as launch:
        (run_dir / LAUNCH_FILE).write_text(json.dumps(launch.report(), indent=2) + "\n", encoding="utf-8")
        result = run_passes(
            launch, run_dir=run_dir, specflo=str(bindir / "specflo"), window=context_window(entry),
            max_passes=max_passes, wall_clock=wall_clock, threshold_percent=threshold_percent,
            on_pass=on_pass,
        )
        return result, launch.report()

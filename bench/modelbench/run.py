"""One bench run end to end: preflight, a sealed workdir, the harness run, scoring and one run record.

`run_one` takes an arm (a llama-swap entry and a harness), a level and a run
index, and refuses the run when any of them is missing or unknown, before
anything talks to llama-swap. Then:

1. Preflight: refuse when llama-swap holds a model the bench did not load.
   When the entry is not loaded, the bench records the load in the bench-load
   state file and has llama-swap load it (a GET of the entry's upstream
   /health, no inference).
2. Engine versions: the entry's command line from llama-swap's GET /running
   gives the engine build, the GGUF path and the engine's sampling defaults
   (read-only; llama-swap's config is never touched).
3. A sealed workdir at the level, outside the repo, with the bench-wide auto
   settings (autonomy and pass cap) in its one commit.
4. `lifecycle.run` runs the harness to one end reason; the llama-swap log's
   growth counts as model activity for the stall limit.
5. Scoring: the harness's session logs are normalised (one merged log when a
   run has several sessions), then metrics, diagnostics, model-id validity,
   the held-out grade of the final tree and server telemetry for the run's
   wall-clock window.
6. The record is checked against `record.REQUIRED_FIELDS` and written.

Where things go:

    <runs dir>/<entry>--<harness>--<level>--<index>/   (default bench/runs/, gitignored)
        record.json         the run record
        lifecycle.json      the lifecycle outcome
        harness.out/.err    the harness's stdout and stderr
        session.norm.json   the merged normalised session log
        pi-agent/sessions/  pi's session JSONL (pi arm)
        claude-config/projects/, pass-NN.*, passes.json, cc-result.json (Claude Code arm)
    <work root>/<same name>/   the sealed workdir and the agent's final tree (default under the temp dir)

The record's `logs` names every one of these paths.

Validity: a run is valid when the model ids its requests recorded are exactly
the arm's entry, no tool call named a protected path (the bench dir and the
held-out archive) and the tree could be graded. `validity` says which check
failed.

Telemetry: when the llama-swap log has no stamped line, or no server timing
line inside the run window, the record carries the unavailable marker with the
reason.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from modelbench import (
    arms,
    cc_continue,
    diagnostics,
    grader,
    launch_cc,
    launch_pi,
    lifecycle,
    metrics,
    norm_cc,
    norm_pi,
    normlog,
    preflight,
    probe,
    record,
    telemetry,
)
from modelbench import workdir as wd

BENCH = wd.BENCH
RUNS_DIR = BENCH / "runs"
WORK_ROOT = Path(tempfile.gettempdir()) / "modelbench"
LLAMA_SWAP_LOG = Path.home() / "AI" / "Engines" / "logs" / "llama-swap.log"

RECORD_FILE = "record.json"
NORMLOG_FILE = "session.norm.json"

# The bench-wide auto settings every arm runs with. An unattended run needs
# `autonomous`: decisions at a fork are delegated to the agent (safe stops
# there for a human), and irreversible or outbound steps still stop the run.
AUTONOMY = "autonomous"
PASS_CAP = 30

# Level limits (wall-clock cap, stall limit; seconds) used until the arm config
# sets them. Generous on purpose: they only stop a run that has gone wrong.
PROVISIONAL_LIMITS = {"quick": (3600.0, 900.0), "fast": (7200.0, 1200.0), "full": (14400.0, 1800.0)}

LOAD_TIMEOUT = 900.0

# llama-server flags that set its sampling defaults, and the record's names for them.
_LLAMA_SAMPLING = {
    "--temp": "temperature", "--top-p": "top_p", "--top-k": "top_k", "--min-p": "min_p",
    "--repeat-penalty": "repeat_penalty", "--presence-penalty": "presence_penalty",
    "--frequency-penalty": "frequency_penalty", "--seed": "seed",
}


class RunError(RuntimeError):
    """The run must not start, or its record cannot be written."""


# -- naming ---------------------------------------------------------------------


def run_name(run: arms.Run) -> str:
    """The run's directory name, the same under the runs dir and the work root."""
    return f"{run.entry}--{run.harness}--{run.level}--{run.run_index:03d}"


def next_run_index(
    config: arms.Config, *, entry: Any, harness: Any, level: Any,
    runs_dir: Path = RUNS_DIR, work_root: Path = WORK_ROOT,
) -> int:
    """The lowest run index of this arm and level whose run dir and workdir are both free."""
    index = 0
    while True:
        run = arms.validate_run(config, entry=entry, harness=harness, level=level, run_index=index)
        name = run_name(run)
        if not (Path(runs_dir) / name).exists() and not (Path(work_root) / name).exists():
            return index
        index += 1


def resolve_limits(
    level: str, config: arms.Config, *, entry: str | None = None,
    wall_clock: float | None = None, stall: float | None = None,
) -> tuple[lifecycle.Limits, dict[str, str]]:
    """The level limits: the arguments, else the arm config's level settings, else the provisional ones.

    The entry's engine limit factor scales the config and provisional values;
    arguments are taken as given. Returns the limits and where each came from.
    """
    spec = config.levels.get(level, {})
    fallback = dict(zip((lifecycle.WALL_CLOCK_KEY, lifecycle.STALL_KEY), PROVISIONAL_LIMITS[level]))
    factor = arms.limit_factor(config, entry) if entry is not None else 1.0
    scaled = f" x{factor:g}" if factor != 1 else ""
    values: dict[str, float] = {}
    source: dict[str, str] = {}
    for key, given in ((lifecycle.WALL_CLOCK_KEY, wall_clock), (lifecycle.STALL_KEY, stall)):
        if given is not None:
            values[key], source[key] = given, "argument"
        elif spec.get(key) is not None:
            values[key], source[key] = spec[key] * factor, "arm config" + scaled
        else:
            values[key], source[key] = fallback[key] * factor, "provisional default" + scaled
    limits = lifecycle.level_limits(
        level, config=config, wall_clock=values[lifecycle.WALL_CLOCK_KEY], stall=values[lifecycle.STALL_KEY])
    return limits, source


# -- the rig --------------------------------------------------------------------


class Rig:
    """llama-swap as the runner sees it: preflight, the bench-load record, loading and /running."""

    def __init__(
        self, base_url: str = preflight.DEFAULT_BASE_URL, state_path: Path | str = preflight.DEFAULT_STATE,
        load_timeout: float = LOAD_TIMEOUT,
    ) -> None:
        self.base_url = base_url
        self.state_path = Path(state_path)
        self.load_timeout = load_timeout

    def preflight(self, run: arms.Run) -> list[str]:
        return preflight.preflight(run, base_url=self.base_url, state_path=self.state_path)

    def record_load(self, entry: str) -> None:
        preflight.record_bench_load(self.state_path, entry)

    def load(self, entry: str) -> None:
        probe.load_entry(self.base_url, entry, timeout=self.load_timeout)

    def running(self) -> list[dict[str, Any]]:
        """llama-swap's GET /running list, each model with its command line."""
        url = self.base_url.rstrip("/") + "/running"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(urllib.request.Request(url, method="GET"), timeout=10.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise preflight.PreflightError(f"cannot read llama-swap running list at {url}: {exc}") from exc
        running = data.get("running") if isinstance(data, dict) else None
        return [m for m in running or [] if isinstance(m, dict)]


def ready_entry(rig: Rig, run: arms.Run) -> dict[str, Any]:
    """Preflight, load the entry when needed, and return its /running item."""
    if run.entry not in rig.preflight(run):
        rig.record_load(run.entry)
        rig.load(run.entry)
    for item in rig.running():
        if item.get("model") == run.entry:
            return item
    raise RunError(f"llama-swap does not list {run.entry} as running after loading it")


# -- engine versions ------------------------------------------------------------


def _flag(argv: Sequence[str], *names: str) -> str | None:
    for i, arg in enumerate(argv):
        for name in names:
            if arg == name and i + 1 < len(argv):
                return argv[i + 1]
            if arg.startswith(name + "="):
                return arg.split("=", 1)[1]
    return None


def _number(text: str) -> int | float | str:
    for kind in (int, float):
        try:
            return kind(text)
        except ValueError:
            continue
    return text


def build_name(binary: str) -> str:
    """The engine build's directory name: the one holding the last `build` dir, else the binary path."""
    parts = Path(binary).parts
    if "build" in parts[:-1]:
        at = len(parts) - 1 - parts[::-1].index("build")
        if at > 0:
            return parts[at - 1]
    return binary


def _build_commit(binary: str) -> str | None:
    """The git commit of the engine's source tree, when the binary sits in one (read-only)."""
    try:
        proc = subprocess.run(["git", "-C", str(Path(binary).parent), "rev-parse", "--short=12", "HEAD"],
                              capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def engine_info(item: Mapping[str, Any], engine: str, *, commit: Callable[[str], str | None] = _build_commit) -> dict[str, Any]:
    """Engine build, GGUF path, sampling defaults and context size from an entry's /running command line."""
    cmd = item.get("cmd")
    if not isinstance(cmd, str) or not cmd.strip():
        raise RunError(f"llama-swap gives no command line for {item.get('model')!r}")
    argv = shlex.split(cmd)
    if engine == "strata":
        inner = shlex.split(argv[2]) if argv[:2] == ["/bin/sh", "-c"] and len(argv) > 2 else argv
        config_path = _flag(inner, "--config")
        if config_path is None:
            raise RunError(f"no --config in the Strata command line of {item.get('model')!r}")
        try:
            conf = json.loads(Path(config_path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RunError(f"cannot read the Strata config {config_path}: {exc}") from None
        binary, args = str(conf.get("exe") or ""), [str(a) for a in conf.get("args") or []]
        gguf = _flag(args, "--native")
        sampling = dict(conf.get("sampling") or {})
        ctx = _flag(args, "--max-context")
        extra = {"config": config_path}
    else:
        binary, args = argv[0], argv[1:]
        gguf = _flag(args, "--model", "-m")
        sampling = {name: _number(value) for flag, name in _LLAMA_SAMPLING.items()
                    if (value := _flag(args, flag)) is not None}
        ctx = _flag(args, "--ctx-size", "-c")
        extra = {}
    if not binary or not gguf:
        raise RunError(f"cannot read the engine binary or GGUF path of {item.get('model')!r} from {cmd!r}")
    name = build_name(binary)
    sha = commit(binary)
    return {
        "engine": engine,
        "engine_build": f"{name}@{sha}" if sha else name,
        "binary": binary,
        "gguf_path": gguf,
        "sampling": sampling,
        "ctx_size": int(ctx) if ctx and ctx.isdigit() else None,
        "cmd": cmd,
        **extra,
    }


def specflo_tree_hash(root: Path = wd.REPO / "src" / "specflo") -> str:
    """sha256 over the specflo source files under test (caches left out), so uncommitted edits show."""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root)
        if "__pycache__" in rel.parts or path.suffix == ".pyc":
            continue
        digest.update(rel.as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return f"sha256:{digest.hexdigest()}"


# -- sessions -------------------------------------------------------------------


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def session_files(harness: str, run_dir: Path, report: Mapping[str, Any] | None = None) -> list[Path]:
    """The harness's session logs of the run, in order.

    pi: every session JSONL under the run copy's sessions dir. Claude Code: each
    pass's session file from passes.json, then every other session JSONL under
    the run copy's projects dir by modification time: passes.json is saved when
    a pass ends, so the pass the bench killed is only found there. Subagent
    transcripts are read with their session.
    """
    run_dir = Path(run_dir)
    report = report or {}
    if harness == "pi":
        agent_dir = Path(report.get("agent_dir") or run_dir / launch_pi.AGENT_DIR_NAME)
        return sorted((agent_dir / "sessions").rglob("*.jsonl"))
    passes = (_read_json(run_dir / cc_continue.RECORD_FILE) or {}).get("passes") or []
    files = [Path(p["session_file"]) for p in passes if isinstance(p, dict) and p.get("session_file")]
    config_dir = Path(report.get("config_dir") or run_dir / launch_cc.CONFIG_DIR_NAME)
    listed = {f.resolve() for f in files}
    others = [f for f in (config_dir / "projects").glob("*/*.jsonl") if f.resolve() not in listed]
    return files + sorted(others, key=lambda f: (f.stat().st_mtime, f.name))


def normalise_one(harness: str, path: Path) -> dict:
    return norm_pi.normalise_file(path) if harness == "pi" else norm_cc.normalise(path)


def merge_logs(harness: str, logs: Sequence[dict]) -> dict:
    """One normalised log from several sessions; ids get an `s<n>:` prefix and items a `session` key."""
    if len(logs) == 1:
        return logs[0]
    merged: dict[str, Any] = {"format": normlog.FORMAT_VERSION, "harness": harness,
                              "requests": [], "tool_calls": []}
    for n, log in enumerate(logs, 1):
        prefix = f"s{n}:"
        for req in log["requests"]:
            merged["requests"].append({**req, "id": prefix + req["id"], "session": n})
        for call in log["tool_calls"]:
            rid = call["request_id"]
            merged["tool_calls"].append({**call, "id": prefix + call["id"], "session": n,
                                         "request_id": prefix + rid if rid is not None else None})
    normlog.check(merged)
    return merged


def normalised_log(harness: str, files: Sequence[Path]) -> tuple[dict, list[str]]:
    """The run's merged normalised log and the errors of any session that could not be read."""
    logs, errors = [], []
    for path in files:
        try:
            logs.append(normalise_one(harness, path))
        except Exception as exc:  # noqa: BLE001 - one unreadable session must not lose the record
            errors.append(f"{path}: {type(exc).__name__}: {exc}")
    if not logs:
        return {"format": normlog.FORMAT_VERSION, "harness": harness, "requests": [], "tool_calls": []}, errors
    return merge_logs(harness, logs), errors


def pass_count(harness: str, run_dir: Path, sessions: Sequence[Path]) -> int:
    """Context passes: Claude Code's pass processes, pi's session files."""
    if harness == "claude-code":
        passes = (_read_json(Path(run_dir) / cc_continue.RECORD_FILE) or {}).get("passes")
        if isinstance(passes, list):
            return len(passes)
    return len(sessions)


# -- scoring --------------------------------------------------------------------


def run_telemetry(log_path: Path, started_at: str, ended_at: str) -> dict[str, Any]:
    """Server telemetry over the run window, or the unavailable marker with its reason."""
    try:
        found = telemetry.extract(log_path, started_at, ended_at)
    except (OSError, ValueError) as exc:
        return telemetry.unavailable(f"llama-swap log cannot be read: {exc}")
    if found.get("available") and not found.get("tasks"):
        return telemetry.unavailable("no server timing line is stamped inside the run window")
    return found


def grade_tree(tree: Path, level: str, grade: Callable[..., grader.GradeResult] = grader.grade) -> dict[str, Any]:
    """The held-out grade of the final tree as a dict; {"error": ...} when it cannot be graded."""
    try:
        return grade(tree, level, workdirs=[tree]).to_dict()
    except Exception as exc:  # noqa: BLE001 - a failed grade is recorded, not raised
        return {"error": f"{type(exc).__name__}: {exc}"}


def validity(entry: str, log: dict, diag: Mapping[str, Any], grade: Mapping[str, Any]) -> dict[str, Any]:
    """Whether the run counts, with each check's result and the reasons it does not."""
    model = preflight.model_validity(entry, log=log)
    reasons = []
    if not model["valid"]:
        reasons.append(f"model ids {model['model_ids']} are not exactly [{entry!r}]")
    if diag.get("contaminated"):
        reasons.append("a tool call named a protected path")
    if "error" in grade:
        reasons.append(f"the final tree could not be graded: {grade['error']}")
    return {"valid": not reasons, "model_ids": model["model_ids"], "model_ids_valid": model["valid"],
            "contaminated": bool(diag.get("contaminated")), "graded": "error" not in grade,
            "reasons": reasons}


def _safe(step: Callable[[], dict], what: str) -> dict:
    try:
        return step()
    except Exception as exc:  # noqa: BLE001 - every scoring step is recorded, never lost
        return {"error": f"{what} failed: {type(exc).__name__}: {exc}"}


def assemble_record(
    run: arms.Run,
    outcome: lifecycle.Outcome,
    *,
    engine: Mapping[str, Any],
    context_window: int,
    specflo_version: str,
    specflo_tree: str,
    sealed: wd.SealedWorkdir,
    run_dir: Path,
    sessions: Sequence[Path],
    log: dict,
    normalise_errors: Sequence[str],
    normlog_path: Path | None,
    metric_values: dict,
    diag: dict,
    grade: dict,
    telemetry_values: dict,
    limits_source: Mapping[str, str],
    llama_swap_log: Path,
    running_after: list[str] | None,
) -> dict[str, Any]:
    """The run record from a finished run's parts (see record.REQUIRED_FIELDS)."""
    fields = outcome.record_fields()
    report = outcome.harness_report or {}
    harness_version = report.get("harness") if isinstance(report.get("harness"), str) else None
    valid = validity(run.entry, log, diag, grade)
    run_dir = Path(run_dir)
    logs = {
        "run_dir": str(run_dir),
        "workdir": str(sealed.path),
        **fields["logs"],
        "lifecycle": str(run_dir / lifecycle.RECORD_FILE),
        "sessions": [str(p) for p in sessions],
        "normalised": str(normlog_path) if normlog_path else None,
        "llama_swap": str(llama_swap_log),
    }
    if run.harness == "claude-code":
        logs["passes"] = str(run_dir / cc_continue.RECORD_FILE)
        logs["cc_result"] = str(run_dir / lifecycle.CC_RESULT_FILE)
        logs["pass_streams"] = sorted(str(p) for p in run_dir.glob("pass-*.stream.jsonl"))
    return {
        "arm": {"entry": run.entry, "harness": run.harness},
        "level": run.level,
        "run_index": run.run_index,
        "versions": {
            "harness": harness_version or f"{run.harness} unknown",
            "engine_build": engine["engine_build"],
            "specflo": specflo_version,
            "specflo_tree": specflo_tree,
            "entry": run.entry,
            "gguf_path": engine["gguf_path"],
            "harness_config_hash": report.get("config_hash"),
        },
        "settings": {
            "sampling": dict(engine.get("sampling") or {}),
            "context_window": context_window,
            "autonomy": fields["settings"]["autonomy"],
            "pass_cap": fields["settings"]["pass_cap"],
            "limits_source": dict(limits_source),
        },
        "started_at": fields["started_at"],
        "ended_at": fields["ended_at"],
        "end_reason": fields["end_reason"],
        "valid": valid["valid"],
        "validity": valid,
        "score": float(grade.get("score", 0.0)) if "error" not in grade else 0.0,
        "grade": grade,
        "passes": pass_count(run.harness, run_dir, sessions),
        "metrics": metric_values,
        "diagnostics": diag,
        "telemetry": telemetry_values,
        "logs": logs,
        "lifecycle": fields["lifecycle"],
        "engine": dict(engine),
        "workdir": {"path": str(sealed.path), "commit": sealed.commit, "slug": sealed.slug},
        "normalise_errors": list(normalise_errors),
        "running_after": running_after,
    }


# -- the run --------------------------------------------------------------------


def run_one(
    *,
    entry: Any,
    harness: Any,
    level: Any,
    run_index: Any,
    config: arms.Config | None = None,
    autonomy: str = AUTONOMY,
    pass_cap: int = PASS_CAP,
    wall_clock: float | None = None,
    stall: float | None = None,
    rig: Rig | None = None,
    runs_dir: Path | str = RUNS_DIR,
    work_root: Path | str = WORK_ROOT,
    llama_swap_log: Path | str = LLAMA_SWAP_LOG,
    headless: bool = False,
    specflo: str | None = None,
    grade: Callable[..., grader.GradeResult] = grader.grade,
    protected: Sequence[Path | str] = (BENCH, grader.ARCHIVE),
    **lifecycle_kw: Any,
) -> tuple[Path, dict[str, Any]]:
    """Run one arm at one level and index to a checked run record; returns its path and the record.

    Refuses (arms.ArmError) a missing or unknown entry, harness, level or run
    index before anything else. `lifecycle_kw` passes through to
    `lifecycle.run` (tests pass `builders`, `poll`, `stop_grace`, `base_env`).
    """
    config = config or arms.load_config()
    run = arms.validate_run(config, entry=entry, harness=harness, level=level, run_index=run_index)
    settings = lifecycle.AutoSettings(autonomy, pass_cap)
    limits, limits_source = resolve_limits(run.level, config, entry=run.entry, wall_clock=wall_clock, stall=stall)
    name = run_name(run)
    run_dir = Path(runs_dir).resolve() / name
    if run_dir.exists():
        raise RunError(f"run dir exists: {run_dir} (this arm, level and index already ran)")
    rig = rig or Rig()
    llama_swap_log = Path(llama_swap_log)

    engine = engine_info(ready_entry(rig, run), config.entries[run.entry]["engine"])
    context_window = cc_continue.context_window(run.entry)
    sealed = wd.make_workdir(Path(work_root) / name, run.level, specflo=specflo,
                             autonomy=settings.autonomy, pass_cap=settings.pass_cap)
    initial_dirs = diagnostics.snapshot_dirs(sealed.path)
    run_dir.mkdir(parents=True)
    if headless:
        lifecycle_kw["herdr"] = None
    outcome = lifecycle.run(
        run, workdir=sealed.path, run_dir=run_dir, settings=settings, limits=limits, specflo=specflo,
        probes=[lifecycle.file_growth(llama_swap_log)], **lifecycle_kw,
    )
    try:
        running_after = [str(m.get("model")) for m in rig.running()]
    except preflight.PreflightError:
        running_after = None

    sessions = session_files(run.harness, run_dir, outcome.harness_report)
    log, normalise_errors = normalised_log(run.harness, sessions)
    normlog_path = run_dir / NORMLOG_FILE
    normlog.dump(log, normlog_path)
    metric_values = _safe(lambda: metrics.compute(log), "metrics")
    diag = _safe(lambda: diagnostics.diagnose(
        log, workdir=sealed.path, own_dirs=[str(run_dir)],
        # Earlier runs' trees and logs hold finished solutions; only this run's own are exempt.
        protected=[str(p) for p in (*protected, Path(work_root).resolve(), Path(runs_dir).resolve())],
        initial_dirs=initial_dirs, complete=outcome.end_reason == lifecycle.END_COMPLETE), "diagnostics")
    grade_values = grade_tree(sealed.path, run.level, grade)
    telemetry_values = run_telemetry(llama_swap_log, outcome.started_at, outcome.ended_at)

    rec = assemble_record(
        run, outcome, engine=engine, context_window=context_window,
        specflo_version=wd.repo_version(), specflo_tree=specflo_tree_hash(), sealed=sealed,
        run_dir=run_dir, sessions=sessions, log=log, normalise_errors=normalise_errors,
        normlog_path=normlog_path, metric_values=metric_values, diag=diag, grade=grade_values,
        telemetry_values=telemetry_values, limits_source=limits_source, llama_swap_log=llama_swap_log,
        running_after=running_after,
    )
    path = run_dir / RECORD_FILE
    path.write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")
    errors = record.validate_record(rec)
    if errors:
        raise RunError(f"the run record at {path} fails the schema: {'; '.join(errors)}")
    return path, rec

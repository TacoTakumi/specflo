"""Run preflight and model-id validity.

Preflight: before a run, read llama-swap's running list (GET <base>/running)
and refuse when a model other than the arm's entry is loaded and the bench did
not load it. It sends no other request: no inference, no unload.

Bench-load state file (default `bench/state/loaded-by-bench.json`): the record
of the llama-swap entries the bench itself loaded. The runner adds an entry
with `record_bench_load` when it loads one, and may drop it when it unloads it.
Format:

    {"format": 1, "loaded": ["<entry id>", ...]}

A missing file means the bench loaded nothing.

Model-id validity: a run is valid only when the set of model ids its
normalised requests recorded (each request's optional `model` key) is exactly
the arm's entry. A run that recorded no model id is invalid, since nothing
shows the entry served it.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable

from modelbench import arms

DEFAULT_BASE_URL = "http://localhost:8080"
DEFAULT_STATE = Path(__file__).resolve().parents[1] / "state" / "loaded-by-bench.json"
STATE_FORMAT = 1


class PreflightError(RuntimeError):
    """The run must not start. `foreign` lists the loaded models the bench did not load."""

    def __init__(self, message: str, foreign: list[str] | None = None) -> None:
        self.foreign = foreign or []
        super().__init__(message)


def read_bench_loaded(path: Path | str = DEFAULT_STATE) -> set[str]:
    """Return the entry ids the bench recorded as loaded by itself."""
    path = Path(path)
    if not path.exists():
        return set()
    data = json.loads(path.read_text(encoding="utf-8"))
    loaded = data.get("loaded") if isinstance(data, dict) else None
    if not isinstance(loaded, list) or not all(isinstance(e, str) for e in loaded):
        raise PreflightError(f"{path}: 'loaded' must be a list of entry ids")
    return set(loaded)


def record_bench_load(path: Path | str, entry: str) -> None:
    """Add `entry` to the bench-load state file, creating it if needed."""
    path = Path(path)
    loaded = read_bench_loaded(path) | {entry}
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"format": STATE_FORMAT, "loaded": sorted(loaded)}
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def fetch_running(base_url: str = DEFAULT_BASE_URL, timeout: float = 5.0) -> list[str]:
    """GET <base_url>/running and return the loaded model ids, in llama-swap's order."""
    url = base_url.rstrip("/") + "/running"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(urllib.request.Request(url, method="GET"), timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise PreflightError(f"cannot read llama-swap running list at {url}: {exc}") from exc
    running = data.get("running") if isinstance(data, dict) else None
    if not isinstance(running, list):
        raise PreflightError(f"{url}: response has no 'running' list")
    return [m["model"] for m in running if isinstance(m, dict) and m.get("model")]


def foreign_models(running: Iterable[str], entry: str, bench_loaded: set[str]) -> list[str]:
    """Return the running models that are neither the arm's entry nor bench-loaded."""
    return [m for m in running if m != entry and m not in bench_loaded]


def preflight(
    run: arms.Run,
    *,
    base_url: str = DEFAULT_BASE_URL,
    state_path: Path | str = DEFAULT_STATE,
) -> list[str]:
    """Return the running list, or raise PreflightError naming each foreign model."""
    running = fetch_running(base_url)
    foreign = foreign_models(running, run.entry, read_bench_loaded(state_path))
    if foreign:
        raise PreflightError(
            "refusing to start: llama-swap has models loaded that the bench did not "
            f"load: {', '.join(foreign)} (left loaded; unload them by hand)",
            foreign,
        )
    return running


def request_model_ids(log: dict[str, Any]) -> set[str]:
    """Return the set of model ids a normalised log's requests recorded."""
    return {
        r["model"]
        for r in log.get("requests", [])
        if isinstance(r, dict) and isinstance(r.get("model"), str) and r["model"]
    }


def model_validity(
    entry: str, *, log: dict[str, Any] | None = None, ids: Iterable[str] | None = None
) -> dict[str, Any]:
    """Return {"valid", "model_ids"} for a run; valid only if the ids are exactly {entry}.

    Pass the normalised `log`, or an explicit list of `ids`, or both (merged).
    """
    found = set(ids or ())
    if log is not None:
        found |= request_model_ids(log)
    return {"valid": found == {entry}, "model_ids": sorted(found)}


def main(argv: list[str] | None = None) -> int:
    """Validate the run, then preflight it; 0 when it may start, nonzero otherwise."""
    p = argparse.ArgumentParser(prog="preflight", description=__doc__.splitlines()[0])
    p.add_argument("--entry")
    p.add_argument("--harness")
    p.add_argument("--level")
    p.add_argument("--run-index", type=int)
    p.add_argument("--config", default=str(arms.DEFAULT_CONFIG))
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--state", default=str(DEFAULT_STATE))
    args = p.parse_args(argv)
    try:
        run = arms.validate_run(
            arms.load_config(args.config),
            entry=args.entry,
            harness=args.harness,
            level=args.level,
            run_index=args.run_index,
        )
    except arms.ArmError as exc:
        print(f"preflight: {exc}", file=sys.stderr)
        return 2
    try:
        running = preflight(run, base_url=args.base_url, state_path=args.state)
    except PreflightError as exc:
        print(f"preflight: {exc}", file=sys.stderr)
        return 1
    print(f"preflight: ok (running: {', '.join(running) or 'none'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

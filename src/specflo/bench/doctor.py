"""`specflo bench doctor`: check that the model bench is set up on this machine.

Run it once at setup and whenever a bench command fails. Each check is ok,
fail or skip, and every fail names its fix, as in `specflo doctor`. The
doctor reads; it never prompts, writes a file or loads a model. The per-run
preflight stays the run's own guard.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path

import yaml

from specflo import settings
from specflo.agent.herdr import HerdrAdapter
from specflo.bench import arms, graphs, launch_cc, launch_pi, telemetry
from specflo.bench.launch_pi import LaunchError
from specflo.bench.preflight import DEFAULT_BASE_URL
from specflo.bench.run import LLAMA_SWAP_LOG
from specflo.doctor import Check
from specflo.errors import SpecfloError

HOME_DIRNAME = "bench"
# How much of the log's end the stamp check reads: the newest lines, as a run's window reads them.
LOG_TAIL_BYTES = 64 * 1024
SET_ROOT = (
    f"`specflo serve root <dir>`, or pass --root <dir> or set ${settings.DAEMON_ROOT_ENV}"
)


def _root(option: Path | None) -> tuple[Check, Path | None]:
    try:
        found = settings.resolve_daemon_root(option)
    except SpecfloError as exc:
        return Check("fail", "daemon root", str(exc), SET_ROOT), None
    if found is None:
        return Check("fail", "daemon root", "no daemon root is set", SET_ROOT), None
    root, source = found
    return Check("ok", "daemon root", f"{root} (from {source})"), root


def _config(root: Path | None) -> tuple[Check, arms.Config | None]:
    if root is None:
        return Check("skip", "bench config", "no daemon root"), None
    path = root / HOME_DIRNAME / "arms.yaml"
    if not path.is_file():
        return Check("fail", "bench config", f"{path} does not exist", "`specflo bench init`"), None
    try:
        config = arms.load_config(path)
    except (arms.ArmError, yaml.YAMLError, OSError) as exc:
        return Check("fail", "bench config", f"{path}: {exc}", f"edit {path}"), None
    detail = f"{path} ({len(config.entries)} entries, harnesses: {', '.join(config.harnesses)})"
    return Check("ok", "bench config", detail), config


def fetch_models(base_url: str, timeout: float = 5.0) -> list[str]:
    """The entry ids llama-swap is configured with: GET <base_url>/v1/models, which loads nothing."""
    url = base_url.rstrip("/") + "/v1/models"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(urllib.request.Request(url, method="GET"), timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    models = data.get("data") if isinstance(data, dict) else None
    if not isinstance(models, list):
        raise ValueError("the response has no 'data' list")
    return [m["id"] for m in models if isinstance(m, dict) and isinstance(m.get("id"), str)]


def _llama_swap(base_url: str, config: arms.Config | None, config_path: Path | None) -> list[Check]:
    url = base_url.rstrip("/") + "/v1/models"
    try:
        listed = set(fetch_models(base_url))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return [Check("fail", "llama-swap", f"no answer from {url}: {exc}",
                      "start llama-swap, or pass --base-url <url>")]
    if config is None:
        return [Check("ok", "llama-swap", f"{url} answers (no bench config, entries not checked)")]
    missing = [entry for entry in config.entries if entry not in listed]
    if not missing:
        return [Check("ok", "llama-swap", f"{url} lists every bench entry ({len(config.entries)})")]
    return [
        Check("fail", f"llama-swap entry {entry}", f"{url} does not list it",
              f"add it to the llama-swap config, or remove it from {config_path}")
        for entry in missing
    ]


# Each harness's version as a run records it in versions.harness, found as its launcher finds it.
HARNESS_VERSIONS: dict[str, Callable[[], str]] = {
    "pi": lambda: f"pi {launch_pi.pi_version()}",
    "claude-code": lambda: f"claude-code {launch_cc.claude_version(launch_cc.find_claude())}",
}


def _harnesses(config: arms.Config | None, versions: Mapping[str, Callable[[], str]]) -> list[Check]:
    names = config.harnesses if config else arms.HARNESSES
    checks = []
    for name in names:
        try:
            checks.append(Check("ok", f"harness {name}", versions[name]()))
        except LaunchError as exc:
            checks.append(Check("skip", f"harness {name}", f"not usable: {exc}"))
    if all(c.status == "skip" for c in checks):
        checks.append(Check(
            "fail", "harnesses", f"none of {', '.join(names)} is installed",
            f"install pi or Claude Code (or name the claude binary with ${launch_cc.CLAUDE_ENV})",
        ))
    return checks


def _stamped(line: str) -> bool:
    """True when the line starts with a wall-clock stamp telemetry reads."""
    match = telemetry._STAMP.match(line)
    if not match:
        return False
    try:
        telemetry._as_time(match.group(1).replace(",", "."))
    except ValueError:
        return False
    return True


def _log(path: Path) -> Check:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(handle.tell() - LOG_TAIL_BYTES, 0))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError as exc:
        return Check("fail", "llama-swap log", f"{path} cannot be read ({exc}), so telemetry is unavailable",
                     "tee llama-swap's output to that file, or pass --llama-swap-log <path>")
    if not any(_stamped(line) for line in tail.splitlines()):
        return Check(
            "fail", "llama-swap log",
            f"{path} has no line with a wall-clock stamp in its last {LOG_TAIL_BYTES // 1024} KiB,"
            " so telemetry is unavailable",
            "stamp each line at the tee with an ISO-8601 date-time and one space",
        )
    return Check("ok", "llama-swap log", f"{path} has stamped lines")


def _herdr(available: Callable[[], bool]) -> Check:
    if available():
        return Check("ok", "herdr", "answers; each run gets a pane")
    return Check("skip", "herdr", "not available; runs go headless (or pass --headless)")


def _matplotlib() -> Check:
    try:
        graphs._require_matplotlib()
    except graphs.GraphError as exc:
        return Check("fail", "matplotlib", "cannot be imported, so `specflo bench graph` cannot draw",
                     str(exc).split(": ", 1)[-1])
    return Check("ok", "matplotlib", "importable")


def run_checks(
    root_option: Path | None,
    *,
    base_url: str = DEFAULT_BASE_URL,
    versions: Mapping[str, Callable[[], str]] = HARNESS_VERSIONS,
    llama_swap_log: Path = LLAMA_SWAP_LOG,
    herdr_available: Callable[[], bool] = lambda: HerdrAdapter().available(),
) -> list[Check]:
    """Every check, in report order."""
    root_check, root = _root(root_option)
    config_check, config = _config(root)
    config_path = root / HOME_DIRNAME / "arms.yaml" if root else None
    return [
        root_check,
        config_check,
        *_llama_swap(base_url, config, config_path),
        *_harnesses(config, versions),
        _herdr(herdr_available),
        _log(Path(llama_swap_log)),
        _matplotlib(),
    ]

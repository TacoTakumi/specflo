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
from pathlib import Path

import yaml

from specflo import settings
from specflo.bench import arms
from specflo.bench.preflight import DEFAULT_BASE_URL
from specflo.doctor import Check
from specflo.errors import SpecfloError

HOME_DIRNAME = "bench"
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


def run_checks(root_option: Path | None, *, base_url: str = DEFAULT_BASE_URL) -> list[Check]:
    """Every check, in report order."""
    root_check, root = _root(root_option)
    config_check, config = _config(root)
    config_path = root / HOME_DIRNAME / "arms.yaml" if root else None
    return [root_check, config_check, *_llama_swap(base_url, config, config_path)]

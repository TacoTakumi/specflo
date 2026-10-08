"""The pi arm's launcher: a frozen config directory, a per-run copy, env and argv.

`bench/configs/pi/` is the frozen pi config directory. It holds `models.json`
(one provider, `llama-swap`, with one model per arm entry, ids equal to the
llama-swap entry ids so every request names the entry), `settings.json` (no
packages, no project trust) and `frozen.json` (the pinned pi version, the
provider name and the deny list). It holds no credential: llama-swap needs no
key, and the provider's `apiKey` is the dummy `no-key` with `authHeader` off.

Extensions are not committed under the frozen directory. Each run gets a copy
of the frozen directory in its own run directory, and the copy gets exactly two
extensions, materialised from this checkout: specflo's pi extension (the same
files `specflo extension install` copies) and the pool deny list (deny.ts, as a
single-file extension). They come from the checkout because they are part of
the specflo under test, which the run record names in `versions.specflo`; a
committed copy would go stale without notice. The run copy also takes pi's own
writes (sessions, trust), so a run never dirties the repo.

The run copy declares only the run's entry and names it as `defaultModel`. pi
resolves `--model` again for every new session, and when that lookup fails it
falls back to the settings default or the first declared model; with one model
declared, the fallback is the run's own entry, never another arm's.

`config_hash` is a hash of the run copy taken before pi starts, so it covers
the committed frozen files and the committed extension sources, and nothing pi
writes. Two copies of the same checkout for the same entry hash the same.

The deny list blocks with no prompt: deny.ts returns a block result to pi's
tool_call event. Its rules are plain text matched as whole words, and they
cannot see paths, so "recursive deletes outside the workdir" is covered by
denying every recursive rm form, inside the workdir too. pi core has no
permission prompts; `--no-approve` makes pi ignore project-local `.pi` files
instead of asking to trust them, and `--no-skills` keeps skills found outside
the run copy (such as `~/.agents/skills`) out of the run. `--no-extensions`
turns off discovery and pi's built-in extensions (`builtin:llama.cpp`, which
can manage models on a llama.cpp router, and `builtin:mcp`), and the two run
copy extensions are then loaded by `-e`, so pi runs with exactly those two.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from specflo.extension_install import extension_source, installable_files

BENCH = Path(__file__).resolve().parents[1]
FROZEN_DIR = BENCH / "configs" / "pi"
FROZEN_FILE = "frozen.json"
MODELS_FILE = "models.json"

DENY_SOURCE = BENCH.parent / "src" / "specflo" / "pool" / "pi_extension" / "deny.ts"
SPECFLO_EXTENSION = "specflo"
DENY_EXTENSION = "specflo-pool-deny.ts"
EXTENSIONS = (SPECFLO_EXTENSION, DENY_EXTENSION)

AGENT_DIR_ENV = "PI_CODING_AGENT_DIR"
DENY_ENV = "SPECFLO_POOL_DENY"
AGENT_DIR_NAME = "pi-agent"

# Files a frozen directory must never hold: credentials and pi's own writes.
FORBIDDEN = ("auth.json", "mcp-auth.json", "sessions", "trust.json", "extensions")

# Variables dropped from the inherited environment: each would point pi or the
# deny list somewhere other than the run copy.
_DROPPED_ENV = ("PI_CODING_AGENT_SESSION_DIR", "PI_PACKAGE_DIR", DENY_ENV)

# Set on every run: no catalog refresh or tool download, no version request,
# no telemetry.
_FIXED_ENV = {"PI_OFFLINE": "1", "PI_SKIP_VERSION_CHECK": "1", "PI_TELEMETRY": "0"}


class LaunchError(RuntimeError):
    """pi must not be started as asked."""


@dataclass(frozen=True)
class Frozen:
    pi_version: str
    provider: str
    deny: tuple[str, ...]


@dataclass(frozen=True)
class PiLaunch:
    """Everything needed to start one pi run, and what the run record needs of it."""

    argv: list[str]
    env: dict[str, str] = field(repr=False)
    cwd: Path
    agent_dir: Path
    pi_version: str
    config_hash: str

    def report(self) -> dict[str, str]:
        """The fields the run record takes: `harness` goes to `versions.harness`."""
        return {
            "harness": f"pi {self.pi_version}",
            "pi_version": self.pi_version,
            "config_hash": self.config_hash,
            "agent_dir": str(self.agent_dir),
        }


def load_frozen(frozen_dir: Path | str = FROZEN_DIR) -> Frozen:
    """Read `frozen.json`; raise LaunchError naming a missing or bad field."""
    path = Path(frozen_dir) / FROZEN_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LaunchError(f"{path}: cannot be read ({exc})") from None
    version, provider, deny = data.get("pi_version"), data.get("provider"), data.get("deny")
    if not isinstance(version, str) or not version:
        raise LaunchError(f"{path}: pi_version missing")
    if not isinstance(provider, str) or not provider:
        raise LaunchError(f"{path}: provider missing")
    if not isinstance(deny, list) or not deny or not all(isinstance(r, str) for r in deny):
        raise LaunchError(f"{path}: deny must be a non-empty list of strings")
    return Frozen(pi_version=version, provider=provider, deny=tuple(deny))


def model_ids(frozen_dir: Path | str, provider: str) -> set[str]:
    """The model ids the frozen `models.json` declares under `provider`."""
    data = json.loads((Path(frozen_dir) / MODELS_FILE).read_text(encoding="utf-8"))
    models = data.get("providers", {}).get(provider, {}).get("models", [])
    return {m.get("id") for m in models if isinstance(m, dict)}


def pi_version(pi_bin: str = "pi") -> str:
    """What `pi --version` prints, stripped."""
    try:
        proc = subprocess.run(
            [pi_bin, "--version"], capture_output=True, text=True, timeout=60, check=False
        )
    except OSError as exc:
        raise LaunchError(f"pi binary {pi_bin!r} cannot be run ({exc})") from None
    if proc.returncode != 0:
        raise LaunchError(f"{pi_bin} --version exited {proc.returncode}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def tree_hash(path: Path | str) -> str:
    """Deterministic sha256 over every file under `path`, by sorted relative path."""
    root = Path(path)
    digest = hashlib.sha256()
    for file in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(file.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(file.read_bytes()).digest())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def materialise(
    run_dir: Path | str, frozen_dir: Path | str = FROZEN_DIR, *, entry: str | None = None
) -> Path:
    """Copy the frozen directory into `run_dir` and add the two extensions.

    With `entry`, the copy declares only that model and makes it the default.
    Returns the run copy, the directory pi gets as its agent dir. Refuses a
    frozen directory holding a credential, pi's own writes or extensions of its
    own, and a run copy that already exists.
    """
    frozen_dir = Path(frozen_dir)
    for name in FORBIDDEN:
        if (frozen_dir / name).exists():
            raise LaunchError(f"frozen pi dir {frozen_dir} must not hold {name}")
    agent_dir = Path(run_dir) / AGENT_DIR_NAME
    if agent_dir.exists():
        raise LaunchError(f"run copy {agent_dir} already exists")
    shutil.copytree(frozen_dir, agent_dir)
    extensions = agent_dir / "extensions"
    source = extension_source()
    for file in installable_files(source):
        target = extensions / SPECFLO_EXTENSION / file.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file, target)
    shutil.copyfile(DENY_SOURCE, extensions / DENY_EXTENSION)
    if entry is not None:
        pin_entry(agent_dir, load_frozen(frozen_dir).provider, entry)
    return agent_dir


def pin_entry(agent_dir: Path | str, provider: str, entry: str) -> None:
    """Keep only `entry` under `provider` in the run copy's models and make it the default model."""
    models_path = Path(agent_dir) / MODELS_FILE
    models = json.loads(models_path.read_text(encoding="utf-8"))
    spec = models["providers"][provider]
    spec["models"] = [m for m in spec["models"] if isinstance(m, dict) and m.get("id") == entry]
    models_path.write_text(json.dumps(models, indent=2) + "\n", encoding="utf-8")
    settings_path = Path(agent_dir) / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings["defaultProvider"], settings["defaultModel"] = provider, entry
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def extension_names(agent_dir: Path | str) -> list[str]:
    """The entries of `<agent_dir>/extensions`, sorted: what pi discovers there."""
    return sorted(p.name for p in (Path(agent_dir) / "extensions").iterdir())


def build_launch(
    entry: str,
    *,
    workdir: Path | str,
    run_dir: Path | str,
    args: Sequence[str] = (),
    pi_bin: str = "pi",
    base_env: Mapping[str, str] | None = None,
    extra_env: Mapping[str, str] | None = None,
    frozen_dir: Path | str = FROZEN_DIR,
) -> PiLaunch:
    """Check the entry and pi version, make the run copy, and build env and argv.

    `args` follow the fixed flags (mode, prompt, session flags: the caller's
    business). `base_env` defaults to this process's environment; `extra_env`
    is laid over it before the launcher's own variables.
    """
    frozen_dir = Path(frozen_dir).resolve()
    workdir = Path(workdir).resolve()
    if workdir == frozen_dir or frozen_dir in workdir.parents:
        raise LaunchError(f"workdir {workdir} is inside the frozen pi dir")
    frozen = load_frozen(frozen_dir)
    if entry not in model_ids(frozen_dir, frozen.provider):
        raise LaunchError(f"entry {entry!r} is not a model of {frozen.provider} in {MODELS_FILE}")
    found = pi_version(pi_bin)
    if found != frozen.pi_version:
        raise LaunchError(f"pi version {found} does not match the pinned {frozen.pi_version}")

    agent_dir = materialise(run_dir, frozen_dir, entry=entry)
    if extension_names(agent_dir) != sorted(EXTENSIONS):
        raise LaunchError(f"run copy extensions are {extension_names(agent_dir)}")
    config_hash = tree_hash(agent_dir)

    env: dict[str, str] = dict(os.environ if base_env is None else base_env)
    for name in _DROPPED_ENV:
        env.pop(name, None)
    env.update(extra_env or {})
    env.update(_FIXED_ENV)
    env[AGENT_DIR_ENV] = str(agent_dir)
    env[DENY_ENV] = json.dumps(list(frozen.deny))

    argv = [
        pi_bin,
        "--no-approve",
        "--no-skills",
        "--no-extensions",
        *[a for name in EXTENSIONS for a in ("-e", str(agent_dir / "extensions" / name))],
        "--provider", frozen.provider,
        "--model", entry,
        *args,
    ]
    return PiLaunch(
        argv=argv,
        env=env,
        cwd=workdir,
        agent_dir=agent_dir,
        pi_version=found,
        config_hash=config_hash,
    )


def start(launch: PiLaunch, **popen: Any) -> subprocess.Popen:
    """Start pi as `launch` describes; `popen` passes through (stdio and so on)."""
    return subprocess.Popen(launch.argv, env=launch.env, cwd=launch.cwd, **popen)

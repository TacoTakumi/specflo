"""The Claude Code arm's launcher: a dedicated config dir, a per-run copy, env and argv.

`bench/configs/claude/` is the dedicated config directory. It holds
`settings.json` (permissions and the few settings a run needs), `claude.json`
(the global state file, installed in the run copy as `.claude.json` so the
first-run onboarding never shows) and `frozen.json` (the pinned Claude Code
version, the real binary, the llama-swap base URL and where the deny rules
come from). It holds no credential; the launcher refuses a dir that does.

Each run gets a copy in its own run directory, which becomes CLAUDE_CONFIG_DIR,
so Claude Code's own writes (sessions, history, state) never reach the repo or
`~/.claude`. Two things are added to the copy from this checkout before it is
hashed, as the pi launcher adds its extensions: specflo's SessionStart hook
(`specflo.hook.settings_snippet`, what `specflo hook install` writes) and the
deny rules. The deny rules are read from the pi arm's `frozen.json`, so both
harnesses carry one list: each rule `r` becomes `Bash(r)` and `Bash(r *)`.
Claude Code matches those per subcommand of a compound command, where deny.ts
matches the words anywhere in the line; both block `git push`, `sudo` and every
recursive rm form.

Permission mode is `dontAsk`: anything that would prompt is denied instead,
so a run never waits for input, interactive or not. The allow list covers the
built-in tools a coding run needs, deny rules still block, and Claude Code
itself denies an `rm` of a critical path (root, home, the workdir) in this
mode. `bypassPermissions` was not used: in it a critical-path removal still
falls through to a prompt.

`config_hash` is a hash of the run copy taken before Claude Code starts and
before the workdir's trust entry is added (that entry names the per-run
workdir, so it would make every hash differ).

Model ids: every variable that names a model is set to the arm's entry, so no
background, subagent or fallback request names another llama-swap entry and
makes llama-swap swap models mid-run. The list was read from the strings of
the pinned binary and the Claude Code env-var docs.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from specflo.hook import settings_snippet

from modelbench import arms, launch_pi
from modelbench.launch_pi import LaunchError, tree_hash

BENCH = Path(__file__).resolve().parents[1]
CONFIG_DIR = BENCH / "configs" / "claude"
FROZEN_FILE = "frozen.json"
SETTINGS_FILE = "settings.json"
STATE_TEMPLATE = "claude.json"
STATE_FILE = ".claude.json"
CONFIG_DIR_NAME = "claude-config"

# Files a config dir must never hold: credentials, and Claude Code's own state
# file, which the launcher writes from the template.
FORBIDDEN = (".credentials.json", "credentials.json", "auth.json", STATE_FILE)

# Every variable that names a model; each is set to the arm's entry.
MODEL_VARS = (
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "ANTHROPIC_CUSTOM_MODEL_OPTION",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "CLAUDE_CODE_BG_CLASSIFIER_MODEL",
    "CLAUDE_CODE_AUTO_MODE_MODEL",
)

# Inherited variables with these prefixes are dropped: they could carry the
# user's account, base URL, model or config dir into the run.
_DROPPED_PREFIXES = ("ANTHROPIC_", "CLAUDE_", "CLAUDECODE", "AWS_BEARER_TOKEN_BEDROCK")

# llama-swap needs no key. Claude Code needs some credential to start without
# a login; a bearer token (not an API key) also avoids the interactive
# "use this API key?" question.
DUMMY_TOKEN = "no-key"

_FIXED_ENV = {
    "ANTHROPIC_AUTH_TOKEN": DUMMY_TOKEN,
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_AUTOUPDATER": "1",
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1",
    "CLAUDE_CODE_NO_MODEL_FALLBACK": "1",
}

PERMISSION_MODE = "dontAsk"


@dataclass(frozen=True)
class Frozen:
    claude_version: str
    claude_bin: str
    base_url: str
    deny: tuple[str, ...]


@dataclass(frozen=True)
class CcLaunch:
    """Everything needed to start one Claude Code run, and what the run record needs."""

    argv: list[str]
    env: dict[str, str] = field(repr=False)
    cwd: Path
    config_dir: Path
    claude_version: str
    config_hash: str

    def report(self) -> dict[str, str]:
        """The fields the run record takes: `harness` goes to `versions.harness`."""
        return {
            "harness": f"claude-code {self.claude_version}",
            "claude_version": self.claude_version,
            "config_hash": self.config_hash,
            "config_dir": str(self.config_dir),
        }


def load_frozen(config_dir: Path | str = CONFIG_DIR) -> Frozen:
    """Read `frozen.json` and the deny rules it names; raise LaunchError on a bad field."""
    path = Path(config_dir) / FROZEN_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LaunchError(f"{path}: cannot be read ({exc})") from None
    for name in ("claude_version", "claude_bin", "base_url", "deny_from"):
        if not isinstance(data.get(name), str) or not data[name]:
            raise LaunchError(f"{path}: {name} missing")
    deny = launch_pi.load_frozen((path.parent / data["deny_from"]).parent).deny
    return Frozen(
        claude_version=data["claude_version"],
        claude_bin=str(Path(data["claude_bin"]).expanduser()),
        base_url=data["base_url"],
        deny=deny,
    )


def deny_rules(rules: Sequence[str]) -> list[str]:
    """Claude Code deny rules for plain-text command rules: the command alone and with arguments."""
    return [form for rule in rules for form in (f"Bash({rule})", f"Bash({rule} *)")]


def claude_version(claude_bin: str) -> str:
    """The version `claude --version` reports (its first word)."""
    try:
        proc = subprocess.run(
            [claude_bin, "--version"], capture_output=True, text=True, timeout=60, check=False
        )
    except OSError as exc:
        raise LaunchError(f"claude binary {claude_bin!r} cannot be run ({exc})") from None
    if proc.returncode != 0 or not proc.stdout.split():
        raise LaunchError(f"{claude_bin} --version exited {proc.returncode}: {proc.stderr.strip()}")
    return proc.stdout.split()[0]


def check_real_binary(claude_bin: str) -> None:
    """Refuse a script: the PATH `claude` can be a wrapper that adds a system prompt."""
    try:
        with open(claude_bin, "rb") as handle:
            head = handle.read(2)
    except OSError as exc:
        raise LaunchError(f"claude binary {claude_bin!r} cannot be read ({exc})") from None
    if head == b"#!":
        raise LaunchError(f"{claude_bin} is a script (a wrapper), not the claude binary")


def materialise(
    run_dir: Path | str, deny: Sequence[str], config_dir: Path | str = CONFIG_DIR
) -> Path:
    """Copy the config dir into `run_dir`, add specflo's hook and the deny rules.

    Returns the run copy, which becomes CLAUDE_CONFIG_DIR. Refuses a config dir
    holding a credential or a state file, settings that already carry hooks,
    and a run copy that already exists.
    """
    config_dir = Path(config_dir)
    for name in FORBIDDEN:
        if (config_dir / name).exists():
            raise LaunchError(f"claude config dir {config_dir} must not hold {name}")
    settings = json.loads((config_dir / SETTINGS_FILE).read_text(encoding="utf-8"))
    if "hooks" in settings:
        raise LaunchError(f"{config_dir / SETTINGS_FILE}: hooks are added by the launcher")
    target = Path(run_dir) / CONFIG_DIR_NAME
    if target.exists():
        raise LaunchError(f"run copy {target} already exists")
    shutil.copytree(config_dir, target)
    (target / STATE_TEMPLATE).rename(target / STATE_FILE)
    settings["hooks"] = settings_snippet()["hooks"]
    settings.setdefault("permissions", {})["deny"] = deny_rules(deny)
    (target / SETTINGS_FILE).write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return target


def trust_workdir(config_dir: Path, workdir: Path) -> None:
    """Mark `workdir` trusted in the run copy's state file, so no trust dialog shows."""
    path = config_dir / STATE_FILE
    state = json.loads(path.read_text(encoding="utf-8"))
    project = state.setdefault("projects", {}).setdefault(str(workdir), {})
    project["hasTrustDialogAccepted"] = True
    project["hasCompletedProjectOnboarding"] = True
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def build_launch(
    entry: str,
    *,
    workdir: Path | str,
    run_dir: Path | str,
    args: Sequence[str] = (),
    claude_bin: str | None = None,
    base_url: str | None = None,
    base_env: Mapping[str, str] | None = None,
    extra_env: Mapping[str, str] | None = None,
    config_dir: Path | str = CONFIG_DIR,
) -> CcLaunch:
    """Check the binary and version, make the run copy, and build env and argv.

    `entry` must be an entry of the arm config; `args` follow the fixed flags
    (print mode, output format, prompt: the caller's business). `base_url`
    defaults to llama-swap's, from `frozen.json`.
    """
    config_dir = Path(config_dir).resolve()
    workdir = Path(workdir).resolve()
    if workdir == config_dir or config_dir in workdir.parents:
        raise LaunchError(f"workdir {workdir} is inside the claude config dir")
    if entry not in arms.load_config().entries:
        raise LaunchError(f"entry {entry!r} is not an entry of the arm config")
    frozen = load_frozen(config_dir)
    binary = claude_bin or frozen.claude_bin
    check_real_binary(binary)
    found = claude_version(binary)
    if found != frozen.claude_version:
        raise LaunchError(
            f"claude version {found} does not match the pinned {frozen.claude_version}"
        )

    target = materialise(run_dir, frozen.deny, config_dir)
    config_hash = tree_hash(target)
    trust_workdir(target, workdir)

    env: dict[str, str] = {
        k: v
        for k, v in (os.environ if base_env is None else base_env).items()
        if not k.startswith(_DROPPED_PREFIXES)
    }
    env.update(extra_env or {})
    env.update(_FIXED_ENV)
    env["CLAUDE_CONFIG_DIR"] = str(target)
    env["ANTHROPIC_BASE_URL"] = base_url or frozen.base_url
    for name in MODEL_VARS:
        env[name] = entry

    argv = [
        binary,
        "--model", entry,
        "--permission-mode", PERMISSION_MODE,
        "--setting-sources", "user",
        "--strict-mcp-config",
        *args,
    ]
    return CcLaunch(
        argv=argv,
        env=env,
        cwd=workdir,
        config_dir=target,
        claude_version=found,
        config_hash=config_hash,
    )


def start(launch: CcLaunch, **popen: Any) -> subprocess.Popen:
    """Start Claude Code as `launch` describes; `popen` passes through (stdio and so on)."""
    return subprocess.Popen(launch.argv, env=launch.env, cwd=launch.cwd, **popen)

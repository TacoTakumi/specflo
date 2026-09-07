"""The agent's seat: a client checkout under the daemon root, one per hosted project.

The daemon runs an agent for a hosted project as a CLI client of itself, so
the agent needs a directory to run in that knows the daemon and the project:
the same shape a developer's checkout has after ``remote add`` and ``new
--remote``. The seat is that shape and nothing more. It holds a config
whose active project is the hosted slug, the hosted registry naming this
daemon's remote for it, and one remote entry carrying the daemon's URL and
an agent token. No artifact lives here; every read and write goes over the
wire to the daemon, which keeps the project under its own projects
directory.

Scaffolding is idempotent: a second call with the same URL and token
rewrites the same bytes, and one with a new token or URL rebinds the remote.

Starting the agent runs ``specflo agent start`` in the seat, with the
transport the daemon's config names (the TUI in a herdr pane by default),
and records which agent name serves the project in one file under the
root. The agent subsystem is driven through its CLI, as a developer drives
it; the mapping is the daemon's own. The daemon keeps no process handle:
whether the agent is alive is asked of agent discovery, by name, each time
it matters, so an agent stopped from a shell is seen as gone on the next
look, and one still serving is found again by a fresh daemon process over
the same root. The daemon imports the agent client for that probe and
nothing else of the subsystem.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from pathlib import Path

from .. import config
from ..agent.client import HostUnreachableError, connect
from ..errors import SpecfloError
from ..projects import validate_slug
from ..service.local import LocalProjectService
from . import DEFAULT_BIND, DEFAULT_PORT, auth
from .routes import audit, project_lock
from .store import open_store
from .workitems import Spawned, WorkItems

# Where a seat reaches the daemon unless the daemon says otherwise.
DEFAULT_URL = f"http://{DEFAULT_BIND}:{DEFAULT_PORT}"
# The pi command the agent runs; None leaves the agent CLI's own default.
DEFAULT_PI_CMD: str | None = None

SEATS_DIRNAME = "seats"
# The one remote a seat knows: the daemon that scaffolded it.
REMOTE_NAME = "daemon"
# The project-to-agent mapping, beside the state store under the root.
AGENTS_FILENAME = "agents.json"
AGENT_NAME_PREFIX = "project-"
# How long a start may take, on top of the agent CLI's own socket deadline.
START_GRACE = 30.0
# The lifecycle states a freshly started agent may answer with and count as
# serving; exited or stopped means its pi is gone.
HEALTHY_STATES = frozenset({"starting", "idle", "working", "needs-attention"})
_PROBE_TIMEOUT = 15.0
# What liveness reports for a project with no agent on record.
MISSING_STATE = "missing"
# What it reports for a recorded agent whose socket does not answer.
DEAD_STATE = "dead"


def seat_dir(root: Path, slug: str) -> Path:
    """Where the daemon root keeps the seat for ``slug``."""
    return Path(root) / SEATS_DIRNAME / slug


def _remove_empty_tree(leaf: Path, *, stop: Path) -> None:
    """Remove ``leaf`` and each empty parent below ``stop``."""
    for directory in (leaf, *leaf.parents):
        if directory == stop or not directory.is_relative_to(stop):
            return
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
        else:
            return


def scaffold(root: Path, slug: str, url: str, token: str) -> Path:
    """Make, or refresh, the seat for ``slug`` and return its directory.

    ``url`` is where the daemon answers and ``token`` an agent token it
    minted. The slug is checked before it becomes a path.
    """
    slug = validate_slug(slug)
    workspace = seat_dir(root, slug)
    workspace.mkdir(parents=True, exist_ok=True)
    if not config.config_path(workspace).is_file():
        # init_config makes the projects directory; the seat keeps none, so
        # it goes straight back: a client seat holds no project of its own.
        cfg = config.init_config(workspace)
        _remove_empty_tree(workspace / cfg.projects_dir, stop=workspace)
    else:
        cfg = config.load_config(workspace)
    config.add_remote(workspace, REMOTE_NAME, url, token)
    config.record_hosted_project(workspace, slug, REMOTE_NAME)
    if cfg.active_project != slug:
        cfg.active_project = slug
        config.save_config(workspace, cfg)
    return workspace


# --- the agent ----------------------------------------------------------------


class AgentStartError(SpecfloError):
    """The agent did not come up: the CLI failed, or its socket never answered."""


def agent_name(slug: str) -> str:
    """The name the project's agent runs under: the slug, prefixed."""
    return AGENT_NAME_PREFIX + slug


def agents_path(root: Path) -> Path:
    return Path(root) / AGENTS_FILENAME


def agent_mapping(root: Path) -> dict[str, str]:
    """Every project's agent name by slug, in slug order."""
    path = agents_path(root)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(slug): str(name) for slug, name in sorted(data.items())}


def _write_mapping(root: Path, mapping: dict[str, str]) -> None:
    agents_path(root).write_text(json.dumps(dict(sorted(mapping.items())), indent=2) + "\n")


def record_agent(root: Path, slug: str, name: str) -> None:
    mapping = agent_mapping(root)
    mapping[slug] = name
    _write_mapping(root, mapping)


def forget_agent(root: Path, slug: str) -> bool:
    """Drop ``slug`` from the mapping; True when it was there."""
    mapping = agent_mapping(root)
    if slug not in mapping:
        return False
    del mapping[slug]
    _write_mapping(root, mapping)
    return True


def agent_for(root: Path, slug: str) -> str | None:
    return agent_mapping(root).get(slug)


@dataclasses.dataclass(frozen=True)
class Liveness:
    """What discovery says of a project's agent: its name, whether it serves, and its state.

    ``name`` is None for a project with no agent on record. ``alive`` is
    True only when the agent's socket answers with a state in which it
    serves; ``state`` is that state, or why it does not serve.
    """

    name: str | None
    alive: bool
    state: str


def _probe(name: str) -> Liveness:
    """Ask the agent named ``name`` for its state over its socket."""
    try:
        with connect(name, connect_timeout=_PROBE_TIMEOUT) as client:
            status = client.status(timeout=_PROBE_TIMEOUT)["status"]
    except (HostUnreachableError, TimeoutError, RuntimeError, OSError, KeyError, TypeError):
        return Liveness(name=name, alive=False, state=DEAD_STATE)
    state = str(status.get("state") or DEAD_STATE)
    return Liveness(name=name, alive=state in HEALTHY_STATES, state=state)


def liveness(root: Path, slug: str) -> Liveness:
    """Whether the project's agent serves right now, by discovery, not by memory."""
    name = agent_for(root, slug)
    if name is None:
        return Liveness(name=None, alive=False, state=MISSING_STATE)
    return _probe(name)


def _agent_cli(*args: str, cwd: Path | None = None, timeout: float) -> subprocess.CompletedProcess:
    """Run one ``specflo agent`` verb as a subprocess of this interpreter."""
    return subprocess.run(
        [sys.executable, "-c", "import sys; from specflo.cli import main; sys.exit(main())", "agent", *args],
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd is not None else None,
        timeout=timeout,
    )


def start_agent(
    root: Path,
    slug: str,
    *,
    transport: str | None = None,
    pi_cmd: str | None = None,
    timeout: float | None = None,
) -> str:
    """Start the project's agent in its seat and record the mapping; the agent's name.

    ``transport`` defaults to the root config's ``agent_transport``. The
    call returns only once ``agent status`` answers for the new agent; a
    start that fails, or whose socket never answers, raises and records
    nothing.
    """
    slug = validate_slug(slug)
    workspace = seat_dir(root, slug)
    if not config.config_path(workspace).is_file():
        raise SpecfloError(f"Project {slug!r} has no seat under the daemon root; scaffold it first.")
    transport = transport or config.load_config(root).agent_transport
    name = agent_name(slug)
    argv = ["start", name, "--transport", transport, "--cwd", str(workspace)]
    if pi_cmd is not None:
        argv += ["--pi-cmd", pi_cmd]
    if transport != "tui":
        argv.append("--no-herdr")
    deadline = (timeout or 0.0) + START_GRACE + _PROBE_TIMEOUT
    try:
        started = _agent_cli(*argv, cwd=workspace, timeout=deadline)
    except subprocess.TimeoutExpired as exc:
        raise AgentStartError(f"Starting agent {name!r} for {slug!r} did not finish in {deadline:.0f}s.") from exc
    if started.returncode != 0:
        detail = (started.stderr or started.stdout).strip()
        raise AgentStartError(f"Starting agent {name!r} for {slug!r} failed: {detail}")
    probe = _probe(name)
    if not probe.alive:
        # A host whose pi died at once answers as alive but exited; it is no
        # agent for the project, so it goes before the failure is reported.
        _agent_cli("stop", name, timeout=_PROBE_TIMEOUT)
        raise AgentStartError(
            f"Agent {name!r} for {slug!r} started but is {probe.state}, not serving."
        )
    record_agent(root, slug, name)
    return name


def start_project_agent(
    root: Path,
    slug: str,
    identity: str,
    *,
    url: str = DEFAULT_URL,
    pi_cmd: str | None = None,
) -> str:
    """Give a hosted project its agent again: refresh the seat, start, audit; the agent's name.

    This is the operation behind the start-agent control on a project whose
    agent is dead or was never started. An agent still serving is refused,
    so the control cannot start a second one beside it.
    """
    slug = validate_slug(slug)
    live = liveness(root, slug)
    if live.alive:
        raise SpecfloError(f"Agent {live.name!r} for {slug!r} is already serving.")
    scaffold(root, slug, url, auth.mint_token(root, "agent"))
    name = start_agent(root, slug, pi_cmd=pi_cmd if pi_cmd is not None else DEFAULT_PI_CMD)
    audit(root, identity, "start_agent", slug, None)
    return name


# --- start project: the one operation behind the control ----------------------


@dataclasses.dataclass(frozen=True)
class Started:
    """What starting a project made: the spawn, the seat, and the agent's name."""

    spawned: Spawned
    seat: Path
    agent: str


def start_project(
    root: Path,
    item_id: int,
    identity: str,
    *,
    url: str = DEFAULT_URL,
    pi_cmd: str | None = None,
) -> Started:
    """Spawn the work item's hosted project, scaffold its seat, and start its agent.

    The three steps run in that order as ``identity``. The spawn runs under
    the root lock like the spawn route and is audited as one; the start is
    audited once the agent answers. A spawn the work item refuses (another
    dev path, a project already spawned) stops before any seat exists. A
    start that fails leaves the spawned project and its seat and raises,
    so the failure is reported and the project can get its agent later.
    """
    service = LocalProjectService(root, config.load_config(root), actor=identity, hosted=True)
    with project_lock(root, None), open_store(root) as store:
        spawned = WorkItems(store).spawn(item_id, service)
        audit(root, identity, "workitem_spawn", spawned.project.slug, str(item_id))
    slug = spawned.project.slug
    workspace = scaffold(root, slug, url, auth.mint_token(root, "agent"))
    name = start_agent(root, slug, pi_cmd=pi_cmd if pi_cmd is not None else DEFAULT_PI_CMD)
    audit(root, identity, "start_project", slug, str(item_id))
    return Started(spawned=spawned, seat=workspace, agent=name)

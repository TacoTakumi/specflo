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
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from .. import config
from ..agent.client import HostUnreachableError, connect
from ..errors import SpecfloError
from ..projects import INITIAL_STATUS, Project, validate_slug
from ..service.local import LocalProjectService
from ..workflow import PHASES
from . import DEFAULT_BIND, DEFAULT_PORT, auth, chatlog
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
# The phase in which a hosted project has an agent: it starts with the
# project and lives until the project advances out of it.
CHAT_PHASE = PHASES[0]
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
# The seats a project's conversation can be held from.
REQUESTER_SEAT = "requester"
DEVELOPER_SEAT = "developer"
# What a fresh agent hears first: which seat it serves, which project, and
# how to run the brainstorm for someone who is not a developer.
OPENING_PROMPT = (
    "You are the agent for the project {name!r} ({slug}). The seat you serve is "
    f"the {REQUESTER_SEAT}: not a developer. Run the brainstorm in requester mode, "
    "as the specflo-brainstorm skill's requester-mode section describes: plain "
    "language, what and why rather than how, decisions recorded in plain words, "
    "one landscape scan early presented plainly, and the gate opened with a note "
    "of the open points when the requester says they are done."
)
# What the agent hears when the gate is taken: which seat took it, and what
# that means for the mode. A developer's take ends requester mode; a gate
# the requester took back leaves the conversation with the requester.
TAKEOVER_MESSAGE = (
    "The {role} seat has taken the gate on project {slug!r}: {taker} is now in "
    "the conversation. {directive}"
)
TAKEOVER_DIRECTIVES = {
    DEVELOPER_SEAT: "Leave requester mode and continue the brainstorm by its normal process.",
    REQUESTER_SEAT: "Stay in requester mode: the conversation is still with the requester.",
}
# The host's refusal of a plain prompt while a run is on; the send retries as a steer.
_BUSY = "busy"
# The agent CLI's refusal to start a name whose socket already serves: that
# agent is someone else's live start, never something to clean up.
_ALREADY_RUNNING = "already running"
# How long a stop may wait for the host to go down, and the grace on top of
# it before the subprocess itself is given up on.
STOP_TIMEOUT = 30.0
# How long a message send may wait for the agent's answer.
_SEND_TIMEOUT = 10.0
# What a start calls once the agent serves and is on record, before the
# opening prompt goes out: the daemon passes its chat pump's subscribe, so
# the opening exchange is in the log from its first frame.
Subscriber = Callable[[str, str], object]


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


def seat_token(root: Path, slug: str) -> str:
    """The agent token the seat for ``slug`` should hold: its own while that still resolves to agent, else a fresh one.

    A seat keeps one credential for its life. Minting on every start would
    leave a live token behind per start with nothing to retire it; reusing
    the seat's own keeps the count at one, and a seat with no token or one
    the daemon no longer knows gets a fresh mint.
    """
    workspace = seat_dir(root, slug)
    if config.config_path(workspace).is_file():
        try:
            token = config.load_remote(workspace, REMOTE_NAME).token
        except SpecfloError:
            token = ""
        if token and auth.identity_for(root, token) == "agent":
            return token
    return auth.mint_token(root, "agent")


# --- the agent ----------------------------------------------------------------


class AgentStartError(SpecfloError):
    """The agent did not come up: the CLI failed, or its socket never answered."""


class AgentMessageError(SpecfloError):
    """A message did not reach the agent: it is not serving, or it refused the prompt."""


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


# Every read-modify-write of the mapping runs under this lock, so two starts
# in flight at once cannot drop each other's entry.
_MAPPING_LOCK = threading.RLock()
# One lock per project across its liveness check and its start, so two
# overlapping starts run one after the other and the second sees the first.
_START_LOCKS: dict[str, threading.Lock] = {}
_START_REGISTRY = threading.Lock()


def _start_lock(slug: str) -> threading.Lock:
    with _START_REGISTRY:
        lock = _START_LOCKS.get(slug)
        if lock is None:
            lock = _START_LOCKS[slug] = threading.Lock()
    return lock


def _write_mapping(root: Path, mapping: dict[str, str]) -> None:
    """Replace the mapping file whole: a crash mid-write leaves the old file, never a partial one."""
    path = agents_path(root)
    scratch = path.with_name(path.name + ".tmp")
    scratch.write_text(json.dumps(dict(sorted(mapping.items())), indent=2) + "\n")
    os.replace(scratch, path)


def record_agent(root: Path, slug: str, name: str) -> None:
    with _MAPPING_LOCK:
        mapping = agent_mapping(root)
        mapping[slug] = name
        _write_mapping(root, mapping)


def forget_agent(root: Path, slug: str) -> bool:
    """Drop ``slug`` from the mapping; True when it was there."""
    with _MAPPING_LOCK:
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
    serves; ``state`` is that state, or why it does not serve. ``transport``
    is the one the agent's status names, for an agent that answered.
    """

    name: str | None
    alive: bool
    state: str
    transport: str | None = None


def _probe(name: str) -> Liveness:
    """Ask the agent named ``name`` for its state over its socket."""
    try:
        with connect(name, connect_timeout=_PROBE_TIMEOUT) as client:
            status = client.status(timeout=_PROBE_TIMEOUT)["status"]
    except (HostUnreachableError, TimeoutError, RuntimeError, OSError, KeyError, TypeError):
        return Liveness(name=name, alive=False, state=DEAD_STATE)
    state = str(status.get("state") or DEAD_STATE)
    transport = status.get("transport")
    return Liveness(
        name=name,
        alive=state in HEALTHY_STATES,
        state=state,
        transport=str(transport) if transport else None,
    )


def send_message(name: str, text: str) -> None:
    """Deliver ``text`` to the agent named ``name`` as a prompt.

    An agent in the middle of a run gets it with steering behaviour, so
    the message lands in the run instead of being refused; one that does
    not serve, or refuses, raises.
    """
    try:
        with connect(name, connect_timeout=_PROBE_TIMEOUT) as client:
            state = str(client.status(timeout=_PROBE_TIMEOUT)["status"].get("state"))
            if state not in HEALTHY_STATES:
                raise AgentMessageError(f"Agent {name!r} is {state}, not serving.")
            command: dict = {"type": "prompt", "message": text}
            if state == "working":
                command["streamingBehavior"] = "steer"
            response = client.request(command, timeout=_SEND_TIMEOUT)
            if (
                not response.get("success")
                and _BUSY in str(response.get("error", ""))
                and "streamingBehavior" not in command
            ):
                # A run began between the probe and the send, and the host
                # refuses a plain prompt mid-run: it goes again as a steer.
                command["streamingBehavior"] = "steer"
                response = client.request(command, timeout=_SEND_TIMEOUT)
    except (HostUnreachableError, TimeoutError, RuntimeError, OSError, KeyError, TypeError) as exc:
        raise AgentMessageError(f"Agent {name!r} did not take the message: {exc}") from exc
    if not response.get("success"):
        raise AgentMessageError(f"Agent {name!r} refused the message: {response.get('error')}")


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


def _agent_stop(name: str) -> bool:
    """Stop the agent named ``name`` through the CLI; True when it reported the stop done.

    A stop that is refused or outruns its bound is reported, not raised:
    the callers decide what a still-running agent means for their state.
    """
    try:
        stopped = _agent_cli(
            "stop", name, "--timeout", str(STOP_TIMEOUT), timeout=STOP_TIMEOUT + _PROBE_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        return False
    return stopped.returncode == 0


def start_agent(
    root: Path,
    slug: str,
    *,
    transport: str | None = None,
    pi_cmd: str | None = None,
    timeout: float | None = None,
    subscribe: Subscriber | None = None,
) -> str:
    """Start the project's agent in its seat and record the mapping; the agent's name.

    ``transport`` defaults to the root config's ``agent_transport``. The
    call returns only once ``agent status`` answers for the new agent; a
    start that fails, or whose socket never answers, raises and records
    nothing. Once the agent serves, the mapping is recorded and
    ``subscribe`` is called with the slug and the agent's name, before the
    opening prompt is sent, so a subscriber sees the whole exchange; an
    agent that takes no opening prompt is stopped and forgotten again.
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
        if _ALREADY_RUNNING in detail:
            # Another start brought this agent up first; it is theirs, and it stays.
            raise AgentStartError(f"Starting agent {name!r} for {slug!r} failed: {detail}")
        # The CLI may have brought up a host that never answered in time
        # and left it running; it goes, so the name is free for the next start.
        _agent_stop(name)
        raise AgentStartError(f"Starting agent {name!r} for {slug!r} failed: {detail}")
    probe = _probe(name)
    if not probe.alive:
        # A host whose pi died at once answers as alive but exited; it is no
        # agent for the project, so it goes before the failure is reported.
        _agent_stop(name)
        raise AgentStartError(
            f"Agent {name!r} for {slug!r} started but is {probe.state}, not serving."
        )
    project = LocalProjectService(root, config.load_config(root)).load_project(slug)
    record_agent(root, slug, name)
    if subscribe is not None:
        subscribe(slug, name)
    try:
        send_message(name, chatlog.label(chatlog.DAEMON_AUTHOR, OPENING_PROMPT.format(name=project.name, slug=slug)))
    except AgentMessageError as exc:
        # An agent that never heard which seat it serves is no agent for the
        # project: it goes, and the start is reported as failed.
        _agent_stop(name)
        forget_agent(root, slug)
        raise AgentStartError(f"Agent {name!r} for {slug!r} started but took no opening prompt: {exc}") from exc
    return name


def announce_take(root: Path, project: Project) -> bool:
    """Tell the project's agent its gate was taken; True when a serving agent heard it.

    A project with no agent, or one that no longer serves, hears nothing:
    the take stands either way, and the page shows the agent's state.
    """
    live = liveness(root, project.slug)
    if not live.alive or live.name is None:
        return False
    gate = project.gate
    role = (gate.role if gate is not None else "") or DEVELOPER_SEAT
    taker = (gate.taken_by if gate is not None else "") or role
    message = TAKEOVER_MESSAGE.format(
        role=role, slug=project.slug, taker=taker,
        directive=TAKEOVER_DIRECTIVES.get(role, TAKEOVER_DIRECTIVES[DEVELOPER_SEAT]),
    )
    try:
        send_message(live.name, chatlog.label(chatlog.DAEMON_AUTHOR, message))
    except AgentMessageError:
        return False
    return True


def start_project_agent(
    root: Path,
    slug: str,
    identity: str,
    *,
    url: str = DEFAULT_URL,
    pi_cmd: str | None = None,
    subscribe: Subscriber | None = None,
) -> str:
    """Give a hosted project its agent again: refresh the seat, start, audit; the agent's name.

    This is the operation behind the start-agent control on a project whose
    agent is dead or was never started. An agent still serving is refused,
    so the control cannot start a second one beside it.
    """
    slug = validate_slug(slug)
    with _start_lock(slug):
        live = liveness(root, slug)
        if live.alive:
            raise SpecfloError(f"Agent {live.name!r} for {slug!r} is already serving.")
        scaffold(root, slug, url, seat_token(root, slug))
        name = start_agent(
            root, slug, pi_cmd=pi_cmd if pi_cmd is not None else DEFAULT_PI_CMD, subscribe=subscribe
        )
    audit(root, identity, "start_agent", slug, None)
    return name


def stop_agent(root: Path, slug: str) -> str | None:
    """Stop the project's agent, if one is on record, and clear the mapping; the name stopped.

    The stop goes through the agent CLI like a developer's; an agent
    already gone is forgotten all the same, since the mapping is only
    worth keeping for an agent discovery can find. A stop the CLI refuses
    or that outruns its bound leaves the mapping in place and returns None:
    an agent that may still serve stays on record, for the page to show and
    a developer to stop by hand.
    """
    name = agent_for(root, slug)
    if name is None:
        return None
    if not _agent_stop(name) and _probe(name).alive:
        return None
    forget_agent(root, slug)
    return name


def release_on_advance(root: Path, project: Project) -> str | None:
    """After an advance: a project no longer in the chat phase loses its agent; the name stopped."""
    if project.status == INITIAL_STATUS and project.phase == CHAT_PHASE:
        return None
    return stop_agent(root, project.slug)


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
    subscribe: Subscriber | None = None,
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
    with _start_lock(slug):
        workspace = scaffold(root, slug, url, seat_token(root, slug))
        name = start_agent(
            root, slug, pi_cmd=pi_cmd if pi_cmd is not None else DEFAULT_PI_CMD, subscribe=subscribe
        )
    audit(root, identity, "start_project", slug, str(item_id))
    return Started(spawned=spawned, seat=workspace, agent=name)

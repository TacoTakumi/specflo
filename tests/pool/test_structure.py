"""Structure: pi's transport stays inside the agent host package.

Only the agent host starts pi and only it reads and writes pi's RPC frames.
The pool, the daemon and the pipeline reach a member through the host's
socket or through the ``specflo agent`` CLI, so leases and the ledger hold no
assumption about how pi is talked to. A scan of the Python sources keeps it
so. It reads the tree it is pointed at, which lets the tests here point it at
a temporary tree with a violation planted in it and see it fail.

The TypeScript under ``extension`` and ``pool/pi_extension`` is loaded by pi
itself, inside pi's process; it is not part of this scan. Which modules may
import the agent subsystem at all is the agent tests' structural guard; this
one adds what no module outside the package may do with pi.
"""

from __future__ import annotations

import ast
import shlex
import shutil
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "specflo"

# The package that owns the pi process and the frames.
AGENT_PACKAGE = "agent"
# Inside it, the two modules that carry pi's transport: the frames, and the
# host that starts pi and holds its pipes. Nothing outside the package imports
# either one. The client is not among them: it talks to the host's socket.
TRANSPORT_MODULES = ("specflo.agent.protocol", "specflo.agent.host")
PROTOCOL_MODULE = "specflo.agent.protocol"

# The modules that hold leases and their record: the pool service, the
# ledger, the expiry and the store. A module of these that is not written yet
# is covered from the day it exists; the ones below it must exist now, so that
# a rename cannot empty the check.
LEASE_MODULES = ("pool/service.py", "pool/ledger.py", "pool/expiry.py", "daemon/poolstore.py")
LEASE_MODULES_PRESENT = ("pool/service.py", "daemon/poolstore.py")

# The calls that start a process, or replace this one with another program.
SUBPROCESS_CALLS = {
    "subprocess.Popen", "subprocess.run", "subprocess.call", "subprocess.check_call",
    "subprocess.check_output", "subprocess.getoutput", "subprocess.getstatusoutput",
    "asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell",
    "os.system", "os.popen", "pty.spawn",
}
OS_CALL_PREFIXES = ("os.exec", "os.spawn", "os.posix_spawn")

# The one call outside the agent package whose program is not written in the
# source. The agent host is given a one-line command to run as its pi; that
# command is the pool runner's launcher, which reads the member's command line
# and scoped environment from a private file and execs it. The launcher is
# started by the host, as the host's pi, and the exec replaces it: pi is still
# the host's child, on the host's pipes, and the launcher never reads or
# writes a frame. Only an exec is excused there - a process started from that
# function would be a child of the launcher, not of the host.
LAUNCHER = ("pool/runner.py", "_become_pi")


def python_files(root: Path, *, outside_agent: bool = True) -> list[Path]:
    agent_dir = root / AGENT_PACKAGE
    return sorted(
        p for p in root.rglob("*.py") if not (outside_agent and agent_dir in p.parents)
    )


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def imported_names(root: Path, path: Path) -> set[str]:
    """Every module *path* imports, and every name it imports from one, dotted
    in full; a relative import is resolved against the file's place under *root*."""
    package_parts = ["specflo", *path.relative_to(root).parent.parts]
    names: set[str] = set()
    for node in ast.walk(parse(path)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base_parts = package_parts[: len(package_parts) - (node.level - 1)]
                module = ".".join(base_parts + ([node.module] if node.module else []))
            else:
                module = node.module or ""
            names.add(module)
            names.update(f"{module}.{alias.name}" for alias in node.names)
    return names


def imports_of(root: Path, path: Path, modules: tuple[str, ...]) -> set[str]:
    return {
        n for n in imported_names(root, path)
        if any(n == m or n.startswith(m + ".") for m in modules)
    }


def frame_names(root: Path) -> set[str]:
    """What the protocol module defines: the names the frames are spoken with."""
    return {
        node.name
        for node in parse(root / AGENT_PACKAGE / "protocol.py").body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and not node.name.startswith("_")
    }


def dotted(node: ast.expr, aliases: dict[str, str]) -> str | None:
    """``a.b.c`` for an attribute chain on a name, with the name's import undone."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(aliases.get(node.id, node.id))
    return ".".join(reversed(parts))


def import_aliases(tree: ast.Module) -> dict[str, str]:
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom) and not node.level:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases


def program_of(call: ast.Call, aliases: dict[str, str]) -> str | None:
    """The program *call* starts, when the source says it: the first word of a
    command written out, or ``sys.executable``. None when it is worked out at
    run time, which is how a member's pi command would arrive."""
    command = call.args[0] if call.args else next(
        (k.value for k in call.keywords if k.arg in ("args", "cmd", "command", "argv")), None
    )
    if isinstance(command, (ast.List, ast.Tuple)) and command.elts:
        command = command.elts[0]
    if isinstance(command, ast.Constant) and isinstance(command.value, str):
        words = shlex.split(command.value)
        return words[0] if words else None
    if command is not None and dotted(command, aliases) == "sys.executable":
        return "sys.executable"
    return None


class ProcessStarts(ast.NodeVisitor):
    """Every call in a module that starts a process, with the function it is in."""

    def __init__(self, aliases: dict[str, str]) -> None:
        self.aliases = aliases
        self.functions = ["<module>"]
        self.found: list[tuple[str, str, ast.Call]] = []

    def visit_FunctionDef(self, node) -> None:
        self.functions.append(node.name)
        self.generic_visit(node)
        self.functions.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        name = dotted(node.func, self.aliases)
        if name and (name in SUBPROCESS_CALLS or name.startswith(OS_CALL_PREFIXES)):
            self.found.append((self.functions[-1], name, node))
        self.generic_visit(node)


def pi_spawns(root: Path) -> list[str]:
    """The places outside the agent package that start pi, or may: a process
    whose program is pi, or whose program the source does not name."""
    found = []
    for path in python_files(root):
        tree = parse(path)
        aliases = import_aliases(tree)
        starts = ProcessStarts(aliases)
        starts.visit(tree)
        relative = path.relative_to(root).as_posix()
        for function, name, call in starts.found:
            program = program_of(call, aliases)
            if program is None:
                if (relative, function) == LAUNCHER and name.startswith("os.exec"):
                    continue
                found.append(f"{relative}:{call.lineno} {function}: {name} of an unnamed program")
            elif Path(program).name == "pi":
                found.append(f"{relative}:{call.lineno} {function}: {name} of pi")
    return found


def frame_speakers(root: Path) -> list[str]:
    """The places outside the agent package that speak pi's frames: an import
    of the protocol module or of the host, or a use of one of the protocol
    module's names, however it was reached."""
    names = frame_names(root)
    found = []
    for path in python_files(root):
        relative = path.relative_to(root).as_posix()
        for module in sorted(imports_of(root, path, TRANSPORT_MODULES)):
            found.append(f"{relative}: imports {module}")
        for node in ast.walk(parse(path)):
            used = (
                node.id if isinstance(node, ast.Name)
                else node.attr if isinstance(node, ast.Attribute)
                else node.name if isinstance(node, (ast.FunctionDef, ast.ClassDef))
                else None
            )
            if used in names:
                found.append(f"{relative}:{node.lineno} uses {used}")
    return found


def lease_modules_importing_protocol(root: Path) -> list[str]:
    return [
        f"{relative}: imports {module}"
        for relative in LEASE_MODULES
        if (root / relative).is_file()
        for module in sorted(imports_of(root, root / relative, (PROTOCOL_MODULE,)))
    ]


# -- the tree as built ------------------------------------------------------


def test_nothing_outside_the_agent_package_starts_pi():
    assert python_files(SRC), "sources not found"
    assert pi_spawns(SRC) == []


def test_nothing_outside_the_agent_package_speaks_pi_frames():
    assert frame_names(SRC), "the protocol module defines no frame names"
    assert frame_speakers(SRC) == []


def test_lease_modules_import_nothing_from_the_protocol_module():
    for relative in LEASE_MODULES_PRESENT:
        assert (SRC / relative).is_file(), f"{relative} not found"
    assert lease_modules_importing_protocol(SRC) == []


def test_the_launcher_is_where_the_runner_says():
    # the excuse above names a function; it must be there to be excused
    path, function = LAUNCHER
    defined = {
        n.name for n in ast.walk(parse(SRC / path)) if isinstance(n, ast.FunctionDef)
    }
    assert function in defined


# -- a planted violation ----------------------------------------------------


def planted(tmp_path: Path, files: dict[str, str]) -> Path:
    """A source tree under *tmp_path* with the real protocol module in its
    agent package and *files* planted around it."""
    root = tmp_path / "specflo"
    (root / AGENT_PACKAGE).mkdir(parents=True)
    shutil.copy(SRC / AGENT_PACKAGE / "protocol.py", root / AGENT_PACKAGE / "protocol.py")
    for relative, source in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return root


def test_a_tree_that_keeps_to_the_agent_cli_passes(tmp_path):
    root = planted(tmp_path, {
        # the host itself starts pi and speaks the frames
        "agent/host.py": (
            "import subprocess\n"
            "from specflo.agent.protocol import write_frame\n"
            "def start(argv):\n"
            "    return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE)\n"
        ),
        "pool/runner.py": (
            "import os, subprocess, sys\n"
            "from ..agent.client import connect\n"
            "def _agent_cli(*args):\n"
            "    return subprocess.run([sys.executable, '-c', 'pass', 'agent', *args])\n"
            "def _become_pi(spec):\n"
            "    os.execvpe(spec['argv'][0], spec['argv'], spec['env'])\n"
        ),
        "index.py": (
            "import subprocess\n"
            "def head():\n"
            "    return subprocess.run(['git', 'rev-parse', 'HEAD'])\n"
        ),
        "pool/service.py": "from . import runner\n",
    })
    assert pi_spawns(root) == []
    assert frame_speakers(root) == []
    assert lease_modules_importing_protocol(root) == []


def test_a_planted_pi_command_line_is_found(tmp_path):
    root = planted(tmp_path, {
        "pool/rogue.py": (
            "import subprocess\n"
            "def member():\n"
            "    return subprocess.Popen(['pi', '--mode', 'rpc'], stdin=subprocess.PIPE)\n"
        ),
        "daemon/rogue.py": (
            "from subprocess import run as go\n"
            "go('/usr/local/bin/pi --mode rpc', shell=True)\n"
        ),
    })
    assert pi_spawns(root) == [
        "daemon/rogue.py:2 <module>: subprocess.run of pi",
        "pool/rogue.py:3 member: subprocess.Popen of pi",
    ]


def test_a_planted_start_of_a_members_own_command_is_found(tmp_path):
    # a member's pi command comes from the configuration, so no source names
    # it: a program worked out at run time is what starting pi looks like
    root = planted(tmp_path, {
        "pool/rogue.py": (
            "import shlex\n"
            "import subprocess as sp\n"
            "def member(command):\n"
            "    return sp.Popen(shlex.split(command), stdin=sp.PIPE, stdout=sp.PIPE)\n"
        ),
    })
    assert pi_spawns(root) == [
        "pool/rogue.py:4 member: subprocess.Popen of an unnamed program",
    ]


def test_the_launcher_is_excused_an_exec_and_nothing_else(tmp_path):
    root = planted(tmp_path, {
        "pool/runner.py": (
            "import os, subprocess\n"
            "def _become_pi(spec):\n"
            "    subprocess.Popen(spec['argv'])\n"
            "def start(spec):\n"
            "    os.execvpe(spec['argv'][0], spec['argv'], spec['env'])\n"
        ),
    })
    assert pi_spawns(root) == [
        "pool/runner.py:3 _become_pi: subprocess.Popen of an unnamed program",
        "pool/runner.py:5 start: os.execvpe of an unnamed program",
    ]


def test_a_planted_frame_speaker_is_found(tmp_path):
    root = planted(tmp_path, {
        "pool/rogue.py": "from ..agent.protocol import encode_frame\n",
        "daemon/rogue.py": "from specflo.agent.host import PiHost\n",
        "rogue.py": (
            "import importlib\n"
            "def send(stream, frame):\n"
            "    importlib.import_module('specflo.agent.protocol').write_frame(stream, frame)\n"
        ),
    })
    assert frame_speakers(root) == [
        "daemon/rogue.py: imports specflo.agent.host",
        "daemon/rogue.py: imports specflo.agent.host.PiHost",
        "pool/rogue.py: imports specflo.agent.protocol",
        "pool/rogue.py: imports specflo.agent.protocol.encode_frame",
        "rogue.py:3 uses write_frame",
    ]


def test_a_planted_protocol_import_in_a_lease_module_is_found(tmp_path):
    for relative in LEASE_MODULES:
        root = planted(tmp_path / Path(relative).stem, {
            relative: "from specflo.agent import protocol\n",
        })
        assert lease_modules_importing_protocol(root) == [
            f"{relative}: imports specflo.agent.protocol",
        ]

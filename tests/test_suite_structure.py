"""Structure: the tests wait through one helper, with no fixed sleeps.

``tests/waits.py`` holds the one ``wait_until``; every other test module
imports it (or takes it from a module that does) and defines none of its own.
The test modules under ``tests/pool`` and ``tests/agent`` call no
``time.sleep``: a test waits for something with ``wait_until``, and gives
something the chance to happen with ``settle``, both scaled by the same
setting.

The scans read the syntax tree, so text in a string (a script a test writes
for a child process to run) is no call and no definition. They read the tree
they are pointed at, which lets the tests here point them at a temporary tree
with a violation planted in it and see it found.
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parent

# The shared helper: the one module that defines wait_until.
HELPER = "waits.py"
WAIT_NAME = "wait_until"

# The folders whose modules call no time.sleep. The helper sits outside them,
# in tests/ itself, so the sleep scan never reads it.
SLEEP_FREE_DIRS = ("pool", "agent")

# The modules in those folders that the sleep scan does not read: the fake
# programs a test runs in place of a real one. stub_pi.py is run as pi, in a
# process of its own; stub_provider.py stands in for the provider's server.
SLEEP_EXEMPT = frozenset({"agent/stub_pi.py", "pool/stub_provider.py"})


def python_files(root: Path) -> list[Path]:
    """Every Python source under *root*, compiled caches left out."""
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def wait_until_definitions(source: str) -> list[int]:
    """The lines where *source* defines a function named wait_until, at any depth."""
    return sorted(
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == WAIT_NAME
    )


def sleep_names(tree: ast.Module) -> tuple[set[str], set[str]]:
    """The names *tree* has for the time module, and for time.sleep itself."""
    modules, sleeps = {"time"}, set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.asname or a.name for a in node.names if a.name == "time")
        elif isinstance(node, ast.ImportFrom) and node.module == "time" and not node.level:
            sleeps.update(a.asname or a.name for a in node.names if a.name == "sleep")
    return modules, sleeps


def sleep_calls(source: str) -> list[int]:
    """The lines where *source* calls time.sleep, by whatever name it imported."""
    tree = ast.parse(source)
    modules, sleeps = sleep_names(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "sleep"
            and isinstance(func.value, ast.Name)
            and func.value.id in modules
        ) or (isinstance(func, ast.Name) and func.id in sleeps):
            found.append(node.lineno)
    return sorted(found)


def stray_wait_untils(root: Path) -> list[str]:
    """Each definition of wait_until under *root* outside the helper."""
    found = []
    for path in python_files(root):
        relative = path.relative_to(root).as_posix()
        if relative == HELPER:
            continue
        for line in wait_until_definitions(path.read_text(encoding="utf-8")):
            found.append(f"{relative}:{line}")
    return found


def sleep_scanned(root: Path) -> list[Path]:
    """The modules the sleep scan reads: those in the sleep-free folders, less the exempt."""
    return [
        path
        for folder in SLEEP_FREE_DIRS
        for path in python_files(root / folder)
        if path.relative_to(root).as_posix() not in SLEEP_EXEMPT
    ]


def fixed_sleeps(root: Path) -> list[str]:
    """Each call of time.sleep in the modules the sleep scan reads under *root*."""
    return [
        f"{path.relative_to(root).as_posix()}:{line}"
        for path in sleep_scanned(root)
        for line in sleep_calls(path.read_text(encoding="utf-8"))
    ]


# -- the tree as built ------------------------------------------------------


def test_only_the_helper_defines_wait_until():
    assert wait_until_definitions((TESTS / HELPER).read_text(encoding="utf-8")), (
        f"{HELPER} defines no {WAIT_NAME}"
    )
    assert stray_wait_untils(TESTS) == []


def test_the_pool_and_agent_tests_call_no_time_sleep():
    for folder in SLEEP_FREE_DIRS:
        assert python_files(TESTS / folder), f"no sources found in tests/{folder}"
    assert fixed_sleeps(TESTS) == []


def test_each_exempt_module_is_there():
    # an exemption names a file; it must be there to be exempt
    for relative in SLEEP_EXEMPT:
        assert (TESTS / relative).is_file(), f"{relative} not found"


# -- a planted violation ----------------------------------------------------


def planted(tmp_path: Path, files: dict[str, str]) -> Path:
    """A test tree under *tmp_path* with *files* planted in it."""
    root = tmp_path / "tests"
    for relative, source in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return root


def test_a_planted_copy_of_wait_until_is_found(tmp_path):
    root = planted(tmp_path, {
        HELPER: "def wait_until(cond):\n    return cond()\n",
        "test_top.py": "def wait_until(cond, timeout=5):\n    return cond()\n",
        "pool/test_nested.py": (
            "class Rig:\n"
            "    def check(self):\n"
            "        async def wait_until(cond):\n"
            "            return cond()\n"
            "        return wait_until\n"
        ),
        "agent/test_imports.py": (
            "from waits import wait_until\n"
            "from .test_runner import wait_until as wait\n"
        ),
    })
    assert stray_wait_untils(root) == [
        "pool/test_nested.py:3",
        "test_top.py:1",
    ]


def test_a_planted_time_sleep_is_found(tmp_path):
    root = planted(tmp_path, {
        "pool/test_plain.py": "import time\n\ndef test_x():\n    time.sleep(1)\n",
        "pool/test_from.py": "from time import sleep\n\nsleep(1)\n",
        "agent/test_alias.py": (
            "import time as t\n"
            "from time import sleep as nap\n"
            "def test_x():\n"
            "    t.sleep(1)\n"
            "    nap(1)\n"
        ),
        "agent/conftest.py": "import time\ntime.sleep(0.1)\n",
    })
    assert fixed_sleeps(root) == [
        "pool/test_from.py:3",
        "pool/test_plain.py:4",
        "agent/conftest.py:2",
        "agent/test_alias.py:4",
        "agent/test_alias.py:5",
    ]


def test_a_sleep_in_a_string_is_not_a_call():
    source = (
        '"""Waits with time.sleep(1), the docstring says."""\n'
        "import asyncio\n"
        "SCRIPT = 'import time\\ntime.sleep(30)\\n'\n"
        "PROBE = f'''import time\n"
        "time.sleep({0.05})\n"
        "'''\n"
        "HELPER = 'def wait_until(cond):\\n    return cond()\\n'\n"
        "async def pause():\n"
        "    await asyncio.sleep(0.01)\n"
    )
    assert sleep_calls(source) == []
    assert wait_until_definitions(source) == []


def test_exempt_modules_and_other_folders_are_not_scanned_for_sleeps(tmp_path):
    root = planted(tmp_path, {
        HELPER: "import time\ntime.sleep(1)\n",
        "test_top.py": "import time\ntime.sleep(1)\n",
        **{relative: "import time\ntime.sleep(1)\n" for relative in SLEEP_EXEMPT},
        "pool/test_clean.py": "from waits import settle\nsettle(0.1)\n",
    })
    assert [p.relative_to(root).as_posix() for p in sleep_scanned(root)] == [
        "pool/test_clean.py",
    ]
    assert fixed_sleeps(root) == []

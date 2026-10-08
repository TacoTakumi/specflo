"""Model-bench dispatcher: `mb.py <cmd> args` runs modelbench.cmd_<cmd>.main(argv)."""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("usage: mb.py <command> [args...]", file=sys.stderr)
        return 2
    cmd, rest = argv[0], argv[1:]
    module_name = f"modelbench.cmd_{cmd.replace('-', '_')}"
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", cmd):
        print(f"mb.py: unknown command {cmd!r}", file=sys.stderr)
        return 2
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name not in (module_name, "modelbench"):
            raise
        print(f"mb.py: unknown command {cmd!r}", file=sys.stderr)
        return 2
    return int(module.main(rest) or 0)


if __name__ == "__main__":
    sys.exit(main())

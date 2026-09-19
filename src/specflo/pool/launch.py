"""Starting a member: a definition becomes pi startup flags.

A definition reaches pi by one route only, the command line: its tools go to
``--tools``, each skill to ``--skill`` and the prompt body to
``--append-system-prompt``. The project's AGENTS.md and CLAUDE.md are kept out
with ``--no-context-files`` unless the definition asks for them, so a project
cannot silently rewrite a member's role.
"""

from __future__ import annotations

import shlex

from .config import Member
from .definitions import AgentDefinition


def pi_argv(definition: AgentDefinition, member: Member) -> list[str]:
    """The command line that starts *member* in the role *definition* gives.

    The member's harness command comes first, split the way the agent host
    splits its pi command. Nothing is read from or written to the environment.
    """
    argv = shlex.split(member.command)
    if definition.tools:
        argv += ["--tools", ",".join(definition.tools)]
    else:
        # With no --tools pi enables its default tools, which is more than a
        # definition that lists none allows.
        argv.append("--no-tools")
    for skill in definition.skills:
        argv += ["--skill", skill]
    argv += ["--append-system-prompt", definition.prompt]
    if not definition.project_context:
        argv.append("--no-context-files")
    return argv

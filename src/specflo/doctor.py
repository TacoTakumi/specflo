"""`specflo doctor`: check that specflo is set up on this machine.

Two things go wrong when someone else installs specflo: the ``specflo`` command
is not on PATH, and an agent harness has no skills, some of them, or a stale or
mis-linked copy. Every problem names the command that fixes it.
"""

from __future__ import annotations

import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from agentsquire.harnesses import HarnessRegistry, default_registry
from agentsquire.hashing import skill_content_hash
from agentsquire.sources import SkillSource
from agentsquire.verbs import SkillState, status


@dataclass(frozen=True)
class Check:
    """One line of the report: ``status`` is ``ok``, ``fail`` or ``skip``."""

    status: str
    subject: str
    detail: str
    fix: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _link_matches(path: Path, content_hash: str) -> bool:
    """True when the symlink at ``path`` leads to a skill with this content."""
    target = path.resolve()
    if not (target / "SKILL.md").is_file():
        return False
    try:
        return skill_content_hash(target) == content_hash
    except OSError:
        return False


def _harness_scope_check(name: str, scope: str, entries, statuses) -> tuple[Check | None, bool]:
    """The check for one harness and scope, and whether it holds every skill.

    ``None`` when no specflo skill is there at all.
    """
    hashes = {e.name: e.content_hash for e in entries}
    good, links, missing, stale, modified, bad_links = 0, 0, [], [], [], []
    for s in statuses:
        if s.path.is_symlink():
            if _link_matches(s.path, hashes[s.name]):
                good += 1
                links += 1
            else:
                bad_links.append(s.name)
        elif s.state is SkillState.UP_TO_DATE:
            good += 1
        elif s.state is SkillState.NOT_INSTALLED:
            missing.append(s.name)
        elif s.state is SkillState.UPDATE_AVAILABLE:
            stale.append(s.name)
        else:
            modified.append(s.name)
    if len(missing) == len(statuses):
        return None, False
    subject = f"{name} ({scope})"
    target = f"--harness {name}:{scope}"
    if not (missing or stale or modified or bad_links):
        detail = f"{good} of {len(statuses)} skills installed"
        if links:
            detail += f" ({links} as links)"
        return Check("ok", subject, detail), True
    details, fixes = [], []
    if bad_links:
        details.append(f"broken or mismatched link: {', '.join(bad_links)}")
        fixes.append(f"remove the link, then run `specflo skills install {target}`")
    if missing:
        details.append(f"missing: {', '.join(missing)}")
        fixes.append(f"`specflo skills install {target}`")
    if stale:
        details.append(f"stale: {', '.join(stale)}")
        fixes.append(f"`specflo skills update {target}`")
    if modified:
        details.append(f"locally modified: {', '.join(modified)}")
        fixes.append(f"`specflo skills update --force {target}`")
    return Check("fail", subject, "; ".join(details), "; ".join(fixes)), False


def run_checks(
    *,
    home: Path,
    project: Path,
    source: SkillSource,
    which: Callable[[str], str | None] = shutil.which,
    registry: HarnessRegistry | None = None,
) -> list[Check]:
    """Every check, in report order: the command, each harness, the summary."""
    checks = []
    found = which("specflo")
    if found:
        checks.append(Check("ok", "specflo on PATH", found))
    else:
        checks.append(Check(
            "fail", "specflo on PATH", "the `specflo` command is not on PATH",
            "`uv tool install specflo`, or add the directory it is installed in to PATH",
        ))

    registry = registry or default_registry()
    backends = registry.detect(home=home, project=project)
    if not backends:
        checks.append(Check(
            "fail", "skills",
            f"No agent harness found (looked for {', '.join(registry.names())}).",
            "install an agent harness, then run `specflo skills install`",
        ))
        return checks

    entries = source.list_skills()
    complete = False
    for backend in backends:
        found_any = False
        for scope in backend.supported_scopes():
            statuses = status(source, backend, scope=scope, home=home, project=project)
            check, whole = _harness_scope_check(backend.name, scope, entries, statuses)
            if check is not None:
                checks.append(check)
                found_any = True
            complete = complete or whole
        if not found_any:
            checks.append(Check("skip", backend.name, "detected, no specflo skills"))
    if not complete:
        checks.append(Check(
            "fail", "skills", "No agent harness has every specflo skill.",
            "`specflo skills install`",
        ))
    return checks


def render(checks: list[Check]) -> str:
    """The human report: one line per check, then the verdict."""
    labels = {"ok": "ok  ", "fail": "FAIL", "skip": "skip"}
    lines = []
    for c in checks:
        line = f"{labels[c.status]}  {c.subject}: {c.detail}"
        if c.fix:
            line += ("" if c.detail.endswith(".") else ".") + f" Fix: {c.fix}"
        lines.append(line)
    problems = sum(1 for c in checks if c.status == "fail")
    if problems:
        lines.append(f"{problems} problem{'s' if problems > 1 else ''} found.")
    else:
        lines.append("All checks passed.")
    return "\n".join(lines)

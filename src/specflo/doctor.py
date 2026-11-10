"""`specflo doctor`: check that specflo is set up on this machine.

Two things go wrong when someone else installs specflo: the ``specflo`` command
is not on PATH, and an agent harness has no skills, some of them, or a stale or
mis-linked copy. Every problem names the command that fixes it. The report ends
with one line on whether a newer release exists, which is advice and never a
problem.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from agentsquire.harnesses import HarnessRegistry, default_registry
from agentsquire.hashing import skill_content_hash
from agentsquire.sources import SkillSource
from agentsquire.verbs import SkillState, status


@dataclass(frozen=True)
class Check:
    """One line of the report: ``status`` is ``ok``, ``fail``, ``skip`` or ``note``.

    A ``note`` is advice: it is shown and never counts as a problem.
    """

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


# A non-empty value of either switches the release notices off (the gate the
# startup notice uses), so doctor then asks nobody.
_RELEASE_SWITCHES = ("CI", "AGENTSQUIRE_NO_UPDATE_CHECK")


def release_check(
    check: Callable[[], object], *, environ: Mapping[str, str] | None = None
) -> Check:
    """The release line: is a newer specflo on PyPI?

    ``check`` asks PyPI and returns agentsquire's result. It is not called when
    a switch in ``environ`` is set. No outcome is a failure: a setup with an old
    release works, and a machine with no route to PyPI is not broken.
    """
    environ = os.environ if environ is None else environ
    subject = "release"
    switched_off = next((name for name in _RELEASE_SWITCHES if environ.get(name)), None)
    if switched_off:
        return Check("skip", subject, f"not checked, {switched_off} is set")
    try:
        result = check()
        status = getattr(result.status, "value", result.status)
        installed, newest = result.installed, result.newest
        if status == "check-failed":
            return Check("skip", subject, f"could not check: {result.error or 'check failed'}")
        if status == "update-available":
            fix = f"`{result.upgrade_command}`" if result.upgrade_command else None
            return Check(
                "note", subject, f"a new release is available: {installed} -> {newest}", fix
            )
        if status == "up-to-date":
            return Check("ok", subject, f"{installed} is the newest release")
        if status == "ahead":
            return Check(
                "ok", subject, f"{installed} is newer than the newest PyPI release {newest}"
            )
        return Check(
            "skip", subject,
            f"installed {installed}, newest PyPI release {newest}; the versions cannot be compared",
        )
    except Exception as exc:
        return Check("skip", subject, f"could not check: {exc}")


def run_checks(
    *,
    home: Path,
    project: Path,
    source: SkillSource,
    which: Callable[[str], str | None] = shutil.which,
    registry: HarnessRegistry | None = None,
    release: Callable[[], object] | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[Check]:
    """Every check, in report order: the command, each harness, the summary, the release.

    The release line is added when ``release`` is given; it is the last line
    whatever the setup checks found.
    """
    checks = _setup_checks(
        home=home, project=project, source=source, which=which, registry=registry
    )
    if release is not None:
        checks.append(release_check(release, environ=environ))
    return checks


def _setup_checks(
    *,
    home: Path,
    project: Path,
    source: SkillSource,
    which: Callable[[str], str | None],
    registry: HarnessRegistry | None,
) -> list[Check]:
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
    labels = {"ok": "ok  ", "fail": "FAIL", "skip": "skip", "note": "note"}
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

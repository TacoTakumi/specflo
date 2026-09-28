"""Review rounds: the ``review-N.md`` artifacts a project accrues.

A round is one end-of-execute whole-branch review, minted from a skeleton by
``specflo review start`` and closed by ``specflo review done``. Every piece of
review state - the round number, whether a round is open, its verdict, date and
sha - lives in these files and nowhere else (D-03), so the read path is a glob
over the project directory plus a frontmatter parse.

Numbers are allocated one above the highest file present, so a deleted round
leaves a permanent gap rather than a reused identity (D-07).
"""

from __future__ import annotations

import dataclasses
import datetime
import re
import subprocess
from pathlib import Path

import yaml

from . import markdown
from .config import SpecfloConfig
from .errors import SpecfloError
from .locking import lock_path_for, locked
from .projects import project_dir

_ROUND_RE = re.compile(r"^review-(\d+)\.md$")
# A finding's severity. A blocker or a should-fix item asks for changes; a
# nit never does.
SEVERITIES = ("blocker", "should-fix", "nit")
FINDINGS_HEADER = "## Findings"
# Any F-NN in any round file counts for numbering, so a new ID can never
# repeat one a round already names, in a finding line or in its prose.
_ANY_FINDING_ID = re.compile(r"\bF-(\d+)\b")
# One finding line, as `review finding add` writes it and as a reviewer with
# no shell may write it by hand.
_FINDING_LINE = re.compile(
    r"^- (F-(\d+)) \((" + "|".join(map(re.escape, SEVERITIES)) + r")\) (\S.*)$"
)
# The whole Findings section of a round that found nothing. A section with
# neither this nor a finding is refused, so a free-form report never passes
# as a clean one.
NONE_LINE = "- none"
# What a refused close can do next, named in every refusal of its findings.
_WAYS_ON = (
    " To go on: rewrite the line in the '- F-NN (severity) text' form, re-add"
    " the finding with `specflo review finding add`, or waive the round with"
    " `specflo review waive --reason <why>`."
)
# The whole verdict vocabulary (D-06). ready-to-merge and waived pass the
# completion gate; changes-requested blocks it.
READY = "ready-to-merge"
CHANGES_REQUESTED = "changes-requested"
WAIVED = "waived"
VERDICTS = (READY, CHANGES_REQUESTED, WAIVED)
# The verdicts that clear the completion gate (D-08). ``waived`` is a
# deliberate, reasoned way past it, not an accident.
PASSING = (READY, WAIVED)
# Frontmatter key order, pinned so a close rewrites a round file in the
# shape `review start` minted it.
_FIELDS = ("round", "verdict", "date", "sha", "reason")
# Minting reads every round file then writes one; the lock covers that whole
# critical section so two processes cannot mint the same number. It is named
# for the series, not for a file, because the file's name is what is being
# decided inside it.
_LOCK_NAME = "review"

_TEMPLATE = """\
---
round: {number}
verdict: ''
date: '{today}'
sha: ''
reason: ''
---

# Review round {number}

## Scope reviewed

## Findings

## Verdict
"""


def number_of(path: Path) -> int | None:
    """The round number in a round file's name, or None if it is not one."""
    match = _ROUND_RE.match(Path(path).name)
    return int(match.group(1)) if match else None


def round_files(root: Path, cfg: SpecfloConfig, slug: str) -> list[tuple[int, Path]]:
    """Every round file in the project directory, ascending by number.

    Each path is carried alongside its number rather than rebuilt from it. A
    hand-created ``review-007.md`` parses as round 7, and reconstructing
    ``review-7.md`` from that number would name a file nobody wrote - which is
    how a single stray filename could otherwise take down every read path.
    """
    directory = project_dir(root, cfg, slug)
    if not directory.is_dir():
        return []
    found = [
        (number, entry)
        for entry in directory.iterdir()
        if entry.is_file() and (number := number_of(entry)) is not None
    ]
    return sorted(found, key=lambda item: (item[0], item[1].name))


def frontmatter(path: Path) -> dict:
    """A round file's frontmatter mapping; ``{}`` when it has none to read.

    A hand-mangled round file must not take the CLI down: whatever is wrong with
    it - no fence, unparseable YAML, or frontmatter that is not a mapping at all
    - it reads as a round with no verdict, which is the same as an open one, and
    the session is steered back into it rather than past it.
    """
    parts = path.read_text().split("---", 2)
    if len(parts) < 3 or parts[0].strip():
        return {}
    try:
        fields = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return {}
    # A scalar or a list parses fine and is truthy, so `or {}` is not enough:
    # only a mapping has the keys the caller is about to ask for.
    return fields if isinstance(fields, dict) else {}


def open_round(root: Path, cfg: SpecfloConfig, slug: str) -> Path | None:
    """The open round's path, or None - when no round exists, or when the latest
    one carries a verdict.

    Derived from :func:`review_state`, never decided again here: a round is open
    exactly while the latest round - the highest-numbered one (REQ-19) - carries
    an empty verdict (REQ-03), and that judgement has one owner. An older
    unclosed file in a hand-edited directory is stale, not current, so it neither
    blocks the next mint nor gets reported as open. Two copies of the rule would
    be two chances for this and ``review_state`` to name different rounds as
    current - the disagreement derived state exists to prevent.

    The state carries the file's own name, so a hand-created ``review-007.md``
    is returned as itself rather than rebuilt as ``review-7.md``.
    """
    state = review_state(root, cfg, slug)
    if state is None or not state["open"]:
        return None
    return project_dir(root, cfg, slug) / state["file"]


def start_round(
    root: Path, cfg: SpecfloConfig, slug: str, today: str | None = None
) -> tuple[Path, bool]:
    """Mint the next round file, or hand back the open one (REQ-01..REQ-03).

    Returns ``(path, created)``; ``created`` is False when a round was already
    open, which is never an error - reusing it is how an abandoned review is
    resumed, and there is deliberately no way to discard one.
    """
    today = today or datetime.date.today().isoformat()
    directory = project_dir(root, cfg, slug)
    with locked(lock_path_for(root, slug, _LOCK_NAME)):
        existing = open_round(root, cfg, slug)
        if existing is not None:
            return existing, False
        number = max((n for n, _ in round_files(root, cfg, slug)), default=0) + 1
        path = directory / f"review-{number}.md"
        path.write_text(_TEMPLATE.format(number=number, today=today))
    return path, True


def skeleton_body(number: int) -> str:
    """The body ``review start`` mints for round ``number``."""
    return _TEMPLATE.format(number=number, today="").split("---", 2)[2].lstrip("\n")


def body_of(path: Path) -> str:
    """A round file's body: everything after its frontmatter."""
    parts = path.read_text().split("---", 2)
    if len(parts) < 3 or parts[0].strip():
        return path.read_text()
    return parts[2].lstrip("\n")


def _render(fields: dict, body: str) -> str:
    """A round file from its frontmatter mapping and body.

    Keys keep their minted order; any key a human added survives after them,
    since the CLI reads only the five it wrote.
    """
    ordered = {key: fields.get(key, "") or "" for key in _FIELDS}
    ordered.update({k: v for k, v in fields.items() if k not in _FIELDS})
    frontmatter_text = yaml.safe_dump(ordered, sort_keys=False).strip()
    return f"---\n{frontmatter_text}\n---\n\n{body}"


def review_state(root: Path, cfg: SpecfloConfig, slug: str) -> dict | None:
    """The project's derived review state, or None while no round file exists.

    The latest round is the highest-numbered one, open or closed (REQ-19); its
    verdict is what every surface reports, so an open round after a passing one
    reads as open rather than as that earlier pass. ``passing`` says whether that
    verdict clears the completion gate, so every reader shares one judgement. Read fresh from the files on
    every call - nothing is cached and nothing is mirrored (REQ-09).
    """
    files = round_files(root, cfg, slug)
    if not files:
        return None
    latest, path = files[-1]
    fields = frontmatter(path)
    verdict = str(fields.get("verdict", "") or "")
    return {
        "rounds": len(files),
        "latest": latest,
        "verdict": verdict,
        "open": not verdict,
        # Whether this round clears the completion gate, decided here so the
        # next-step hint does not re-derive it. `workflow` cannot import this
        # module (review -> projects -> workflow would close a cycle), and two
        # copies of the rule are two chances to disagree.
        "passing": verdict in PASSING,
        "date": str(fields.get("date", "") or ""),
        "sha": str(fields.get("sha", "") or ""),
        "reason": str(fields.get("reason", "") or ""),
        "file": path.name,
    }


def completion_issues(root: Path, cfg: SpecfloConfig, slug: str) -> list[str]:
    """What the review still owes before the project may complete (REQ-21).

    Empty means the latest round is closed with a passing verdict. The gate
    reads that verdict and nothing else - never the round's findings (REQ-16),
    and never how old the round is (REQ-08). A reviewer that weighed some nits
    and still said ready-to-merge is not second-guessed here.
    """
    state = review_state(root, cfg, slug)
    if state is None:
        return [
            "no review round recorded: run the final whole-branch review, then"
            " `specflo review start` and `specflo review done --verdict <v>`."
        ]
    if state["open"]:
        return [
            f"review round {state['latest']} is still open ({state['file']}):"
            " close it with `specflo review done --verdict <v>`."
        ]
    if state["verdict"] not in PASSING:
        return [
            f"the latest review round ({state['file']}) is {state['verdict']}:"
            " address the findings, then run another round with"
            " `specflo review start`."
        ]
    return []


def head_sha(root: Path) -> str:
    """The short HEAD sha, or "" wherever git cannot answer (REQ-07).

    Degrades exactly as ``index.py`` does: git missing, the directory untracked,
    a repo with no commits yet, or a hung call all give the empty stamp, and the
    close carries on. Nothing derives validity from this (REQ-08) - it is
    evidence for a human judgement call, never a verdict.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _round_number(path: Path, raw) -> int:
    """The number to write back into ``path``'s frontmatter.

    The filename is the authority: it is what allocated the round and what every
    read path counts, so a file whose frontmatter never carried a number keeps
    its own rather than being stamped 0. A frontmatter number that is not a
    number is a hand-edit the CLI will not guess at.
    """
    if raw is None or raw == "":
        return number_of(path) or 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise SpecfloError(
            f"{path.name} has a non-numeric round ({raw!r}) in its frontmatter."
            " Fix it by hand, then close the round."
        ) from None


@dataclasses.dataclass(frozen=True)
class ClosedRound:
    """What a close decided: the round, its verdict, and its findings per severity.

    ``findings`` is None for a waived round, whose findings are never read.
    """

    path: Path
    verdict: str
    findings: dict[str, int] | None


def _findings_lines(doc: str) -> list[str] | None:
    """The non-blank lines under a document's Findings heading, comments dropped.

    None when the document has no Findings heading at all.
    """
    section = markdown.section_body(doc, FINDINGS_HEADER)
    if section is None:
        return None
    return [
        line.strip()
        for line in markdown.strip_comments(section).splitlines()
        if line.strip()
    ]


def _defined_numbers(doc: str) -> set[int]:
    """The number of each F-NN a finding line under the Findings heading defines."""
    return {
        int(match.group(2))
        for line in _findings_lines(doc) or []
        if (match := _FINDING_LINE.match(line))
    }


def parse_findings(
    root: Path, cfg: SpecfloConfig, slug: str, path: Path, body: str
) -> list[tuple[str, str, str]]:
    """The round's findings as ``(F-NN, severity, text)``, or a refusal.

    Every line under Findings is a finding line, or the section is exactly
    ``- none``. Refused: a line in neither form, an F-NN that this round or
    another round of the project already defines, a section with neither,
    and ``- none`` beside findings. Each refusal names the line or the ID.
    """
    lines = _findings_lines(body)
    if not lines:
        raise SpecfloError(
            f"{path.name} records no findings and no '{NONE_LINE}' under"
            f" '{FINDINGS_HEADER}'; a clean round says '{NONE_LINE}' there."
            + _WAYS_ON
        )
    if set(lines) == {NONE_LINE}:
        return []
    if NONE_LINE in lines:
        raise SpecfloError(
            f"{path.name} says '{NONE_LINE}' under '{FINDINGS_HEADER}' and also"
            " records findings." + _WAYS_ON
        )
    elsewhere = {
        number
        for _, other in round_files(root, cfg, slug)
        if other != path
        for number in _defined_numbers(other.read_text())
    }
    seen: set[int] = set()
    findings = []
    for line in lines:
        match = _FINDING_LINE.match(line)
        if match is None:
            raise SpecfloError(
                f"{path.name} has a line under '{FINDINGS_HEADER}' that is not a"
                f" finding: {line!r}." + _WAYS_ON
            )
        finding_id, number, severity, text = match.groups()
        if int(number) in seen or int(number) in elsewhere:
            where = (
                "earlier in this round" if int(number) in seen
                else "in another round of the project"
            )
            raise SpecfloError(
                f"{path.name} records {finding_id}, an ID already used {where}."
                + _WAYS_ON
            )
        seen.add(int(number))
        findings.append((finding_id, severity, text))
    return findings


def derive_verdict(findings: list[tuple[str, str, str]]) -> tuple[str, dict[str, int]]:
    """The verdict a round's findings give, and how many there are per severity.

    Any blocker or should-fix finding asks for changes; nits never do.
    """
    counts = {severity: 0 for severity in SEVERITIES}
    for _, severity, _ in findings:
        counts[severity] += 1
    blocking = counts["blocker"] + counts["should-fix"]
    return (CHANGES_REQUESTED if blocking else READY), counts


def close_round(
    root: Path,
    cfg: SpecfloConfig,
    slug: str,
    verdict: str | None = None,
    reason: str | None = None,
    today: str | None = None,
    report_text: str | None = None,
) -> ClosedRound:
    """Close the open round with the verdict its findings give, date and sha.

    The verdict is derived from the round's Findings section (see
    :func:`parse_findings` and :func:`derive_verdict`). An explicit
    ``verdict`` is accepted only when it is the derived one, except
    ``waived``, which closes the round without reading its findings.

    The date and sha stamp the close, overwriting the mint-time date: what
    matters is when the review was decided, not when its file appeared.

    ``report_text`` becomes the round's body (REQ-10) - the escape hatch for
    a reviewer that returns its report as text rather than writing into the
    file. The caller reads the report file itself and passes the text, so
    this never opens a file the caller named. It is refused once the body
    has been written into, so an ingest can never overwrite a review someone
    already recorded.

    Raises ``SpecfloError`` - leaving every file untouched - when the verdict is
    not one of :data:`VERDICTS`, when ``waived`` comes without a reason
    (REQ-06), when the body is already written, when no round is open, when
    the findings are malformed, or when an explicit verdict is not the
    derived one.
    """
    if verdict is not None and verdict not in VERDICTS:
        raise SpecfloError(
            f"Unknown verdict {verdict!r}. Valid values: " + ", ".join(VERDICTS) + "."
        )
    if verdict == WAIVED and not (reason or "").strip():
        raise SpecfloError(
            "Verdict 'waived' needs a --reason, so a project that skipped review"
            " records why."
        )
    with locked(lock_path_for(root, slug, _LOCK_NAME)):
        path = open_round(root, cfg, slug)
        if path is None:
            raise SpecfloError(
                "No review is open. Start one with `specflo review start`."
            )
        fields = frontmatter(path)
        fields["round"] = _round_number(path, fields.get("round"))
        body = body_of(path)
        if report_text is not None:
            if body.strip() != skeleton_body(int(fields.get("round") or 0)).strip():
                raise SpecfloError(
                    f"{path.name} already has content; ingesting a report would"
                    " overwrite it. Close the round without --file instead."
                )
            body = report_text
        counts = None
        if verdict != WAIVED:
            findings = parse_findings(root, cfg, slug, path, body)
            derived, counts = derive_verdict(findings)
            if verdict is not None and verdict != derived:
                blocking = [f for f, severity, _ in findings if severity != "nit"]
                raise SpecfloError(
                    f"The findings in {path.name} make it {derived}"
                    + (f" ({', '.join(blocking)})" if blocking else "")
                    + f", not {verdict}. Run `specflo review done` without"
                    " --verdict, or waive the round with `--verdict waived"
                    " --reason <why>`."
                )
            verdict = derived
        fields["verdict"] = verdict
        fields["date"] = today or datetime.date.today().isoformat()
        fields["sha"] = head_sha(root)
        if reason is not None:
            fields["reason"] = reason
        path.write_text(_render(fields, body))
    return ClosedRound(path=path, verdict=verdict, findings=counts)


def _next_finding_id(root: Path, cfg: SpecfloConfig, slug: str) -> str:
    """One above the highest F-NN any round file of the project names."""
    numbers = [
        int(match.group(1))
        for _, path in round_files(root, cfg, slug)
        for match in _ANY_FINDING_ID.finditer(path.read_text())
    ]
    return f"F-{max(numbers, default=0) + 1:02d}"


def add_finding(
    root: Path, cfg: SpecfloConfig, slug: str, severity: str, text: str
) -> tuple[str, Path]:
    """Append ``- F-NN (severity) text`` to the open round's Findings section.

    Returns ``(F-NN, round path)``. The ID is numbered across every round of
    the project, and the lock spans the read that picks it and the write that
    records it, so two adds at once never share an ID.

    Raises ``SpecfloError`` - leaving every file untouched - on a severity
    outside :data:`SEVERITIES`, an empty or multi-line text, no open round,
    or an open round with no Findings section.
    """
    if severity not in SEVERITIES:
        raise SpecfloError(
            f"Unknown severity {severity!r}. Valid values: " + ", ".join(SEVERITIES) + "."
        )
    if not text.strip():
        raise SpecfloError("A finding needs a non-empty --text.")
    # Any line break the round file is later split on, not only \n and \r.
    if text.splitlines() != [text]:
        raise SpecfloError("A finding's --text must be one line.")
    with locked(lock_path_for(root, slug, _LOCK_NAME)):
        path = open_round(root, cfg, slug)
        if path is None:
            raise SpecfloError(
                "No review is open. Start one with `specflo review start`."
            )
        doc = path.read_text()
        body = markdown.section_body(doc, FINDINGS_HEADER)
        if body is None:
            raise SpecfloError(
                f"{path.name} has no '{FINDINGS_HEADER}' section. Add the heading"
                " by hand, then add the finding again."
            )
        finding_id = _next_finding_id(root, cfg, slug)
        line = f"- {finding_id} ({severity}) {text.strip()}"
        kept = body.strip("\n")
        path.write_text(
            markdown.replace_section_body(
                doc, FINDINGS_HEADER, f"{kept}\n{line}" if kept else line
            )
        )
    return finding_id, path

"""Close review rounds in tests the way a reviewer records them.

``review done`` derives its verdict from the findings in the open round, so a
test that closes a round records findings first: ``- none`` for a
ready-to-merge round, one blocker for a changes-requested one. A waived round
needs neither.
"""

from pathlib import Path

from specflo import config, markdown, review

NONE_LINE = "- none"
BLOCKER_TEXT = "A blocker the next round must see fixed"


def latest_round(project_dir: Path) -> Path:
    """The highest-numbered round file in ``project_dir``."""
    rounds = [
        (number, path)
        for path in Path(project_dir).iterdir()
        if (number := review.number_of(path)) is not None
    ]
    return max(rounds)[1]


def write_none(path: Path) -> None:
    """Make ``- none`` the whole Findings section of the round at ``path``.

    A hand-written round with no Findings heading gets one at its end.
    """
    doc = Path(path).read_text()
    if markdown.section_body(doc, review.FINDINGS_HEADER) is None:
        doc = doc.rstrip("\n") + f"\n\n{review.FINDINGS_HEADER}\n"
    Path(path).write_text(markdown.replace_section_body(doc, review.FINDINGS_HEADER, NONE_LINE))


def _active_project_dir() -> Path:
    root = config.find_root(Path.cwd())
    cfg = config.load_config(root)
    return root / cfg.projects_dir / cfg.active_project


def record(root, cfg, slug, verdict) -> None:
    """Record, in the open round, the findings that make ``verdict`` the derived one."""
    if verdict == review.READY:
        write_none(review.open_round(root, cfg, slug))
    elif verdict == review.CHANGES_REQUESTED:
        review.add_finding(root, cfg, slug, "blocker", BLOCKER_TEXT)


def close_round(root, cfg, slug, verdict, **kwargs) -> Path:
    """``review.close_round`` after recording the findings ``verdict`` needs."""
    record(root, cfg, slug, verdict)
    return review.close_round(root, cfg, slug, verdict, **kwargs)


def review_done(runner, app, verdict="ready-to-merge", *extra, project_dir=None):
    """``specflo review done --verdict <verdict>`` after recording its findings.

    A blocker goes through ``review finding add``; ``- none`` is written into
    the latest round in ``project_dir`` (the active project's directory by
    default), which is where a hosted test passes the daemon's copy.
    """
    if verdict == review.CHANGES_REQUESTED:
        added = runner.invoke(
            app, ["review", "finding", "add", "--severity", "blocker", "--text", BLOCKER_TEXT]
        )
        assert added.exit_code == 0, added.output
    elif verdict == review.READY:
        write_none(latest_round(project_dir or _active_project_dir()))
    return runner.invoke(app, ["review", "done", "--verdict", verdict, *extra])

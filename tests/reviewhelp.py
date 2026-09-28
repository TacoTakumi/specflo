"""Close review rounds in tests the way a reviewer records them.

``review done`` derives its verdict from the findings in the open round, so a
test that closes a round records findings first: ``- none`` for a
ready-to-merge round, one blocker for a changes-requested one. It also checks
every earlier item closed, since a round closes only once each is checked. A
waived round needs none of it. ``review start`` opens a round only once each
open item has a done fix task, which :func:`fix_open_items` adds.
"""

import json
from pathlib import Path

from specflo import config, markdown, plan, review

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


def fix_open_items(root, cfg, slug) -> list[str]:
    """Add a done task fixing each open item that has none; the tasks' IDs.

    ``review start`` opens no round while an open item has no done fix task.
    A project with no plan gets one, and only when an item needs a task.
    """
    unfixed = review.unfixed_items(root, cfg, slug)
    if unfixed:
        plan.start_plan(root, cfg, slug)
    added = []
    for item in unfixed:
        task = plan.add_task(root, cfg, slug, f"Fix {item}", f"{item} is fixed",
                             "uv run pytest", implements=[], fixes=[item])
        plan.start_task(root, cfg, slug, task.id)
        plan.done_task(root, cfg, slug, task.id)
        added.append(task.id)
    return added


def fix_by_cli(runner, app, *items: str) -> list[str]:
    """Add, start and finish a task fixing each of ``items`` through the CLI; the tasks' IDs.

    For a hosted project, whose plan only the daemon holds. A plan is started
    first; one already started is kept.
    """
    started = runner.invoke(app, ["plan", "start"])
    assert started.exit_code == 0, started.output
    added = []
    for item in items:
        result = runner.invoke(app, [
            "task", "add", "--text", f"Fix {item}", "--acceptance", f"{item} is fixed",
            "--verify", "uv run pytest", "--fixes", item,
        ])
        assert result.exit_code == 0, result.output
        task_id = result.output.split()[1]
        for verb in ("start", "done"):
            moved = runner.invoke(app, ["task", verb, task_id])
            assert moved.exit_code == 0, moved.output
        added.append(task_id)
    return added


def fix_active_open_items() -> list[str]:
    """:func:`fix_open_items` for the active project of the current checkout."""
    root = config.find_root(Path.cwd())
    cfg = config.load_config(root)
    return fix_open_items(root, cfg, cfg.active_project)


def _active_project_dir() -> Path:
    root = config.find_root(Path.cwd())
    cfg = config.load_config(root)
    return root / cfg.projects_dir / cfg.active_project


def record(root, cfg, slug, verdict) -> None:
    """Record, in the open round, the findings that make ``verdict`` the derived one."""
    if verdict != review.WAIVED:
        for item in review.review_scope(root, cfg, slug)["items"]:
            review.check_finding(root, cfg, slug, item, "closed")
    if verdict == review.READY:
        write_none(review.open_round(root, cfg, slug))
    elif verdict == review.CHANGES_REQUESTED:
        review.add_finding(root, cfg, slug, "blocker", BLOCKER_TEXT)


def close_round(root, cfg, slug, verdict, **kwargs) -> Path:
    """``review.close_round`` after recording the findings ``verdict`` needs; the round's path."""
    record(root, cfg, slug, verdict)
    return review.close_round(root, cfg, slug, verdict, **kwargs).path


def review_done(runner, app, verdict="ready-to-merge", *extra, project_dir=None):
    """``specflo review done --verdict <verdict>`` after recording its findings.

    A blocker goes through ``review finding add``; ``- none`` is written into
    the latest round in ``project_dir`` (the active project's directory by
    default), which is where a hosted test passes the daemon's copy.
    """
    if verdict != review.WAIVED:
        scope = runner.invoke(app, ["review", "start", "--json"])
        assert scope.exit_code == 0, scope.output
        for item in json.loads(scope.stdout)["items"]:
            checked = runner.invoke(app, ["review", "finding", "check", item, "closed"])
            assert checked.exit_code == 0, checked.output
    if verdict == review.CHANGES_REQUESTED:
        added = runner.invoke(
            app, ["review", "finding", "add", "--severity", "blocker", "--text", BLOCKER_TEXT]
        )
        assert added.exit_code == 0, added.output
    elif verdict == review.READY:
        write_none(latest_round(project_dir or _active_project_dir()))
    return runner.invoke(app, ["review", "done", "--verdict", verdict, *extra])

"""The hosted pipeline parity scenario.

One scenario drives the whole pipeline twice: once against a project in the
checkout, once against a project on a daemon. Every command must exit the
same way and print the same stdout, once the two things that legitimately
differ are normalized away: the one line of ``status`` that says where the
project lives (``Dir:`` for the checkout, ``Remote:`` for a daemon), and the
``- Actor:`` line ``task show`` prints for an entry added through the daemon,
which a local add does not carry. Both are documented differences; the
hosted fixture asserts each is really there before it is folded. Everything
else is identical.
"""

import json
import re
import subprocess
import threading
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from specflo import config, daemon
from specflo.cli import app
from test_review_regression import SLUG as MARKED_SLUG
from test_review_regression import _findings, _hand_written_run, _regression_run

runner = CliRunner()

SLUG = "parity-thing"


def _pipeline():
    """The steps, in order. A step is ``(args, stdin)``; an argument callable
    is resolved against the ids captured from earlier output."""
    return [
        (["brainstorm", "start"], None),
        (["egress", "local"], None),
        (["decision", "add", "--text", "Use one facade", "--rationale", "one seam"], None),
        (["validate", "brainstorm"], None),
        (["section", "set", "brainstorm", "Out of scope / Deferred", "--stdin"], "No auth.\n"),
        (["validate", "brainstorm"], None),
        (["advance"], None),
        (["spec", "start"], None),
        (["requirement", "add", "--text", "Prints help", "--acceptance", "a no-arg run exits 0",
          "--from", lambda ids: ids["decision"]], None),
        (["section", "set", "spec", "In scope", "--stdin"], "- the CLI.\n"),
        (["section", "set", "spec", "Out of scope", "--stdin"], "- the GUI.\n"),
        (["validate", "spec"], None),
        (["advance"], None),
        (["plan", "start"], None),
        (["task", "add", "--text", "Build help", "--acceptance", "help prints",
          "--verify", "uv run pytest", "--from", lambda ids: ids["requirement"]], None),
        (["validate", "plan"], None),
        (["advance"], None),
        (["task", "show"], None),
        (["task", "start", lambda ids: ids["task"]], None),
        (["task", "done", lambda ids: ids["task"]], None),
        (["review", "start"], None),
        (["review", "done", "--verdict", "ready-to-merge", "--file", "report.md"], None),
        (["doc", "show", "review-1"], None),
        (["validate", "execute"], None),
        (["checkpoint"], None),
        (["status"], None),
        (["hook", "reseed"], None),
        (["hook", "reseed", "--continue"], None),
        (["doc", "show", "project"], None),
        (["gate", "open", "developer", "--note", "Open points: the name"], None),
        (["gate", "open", "requester"], None),
        (["gate", "take"], None),
        (["gate", "take"], None),
        (["advance"], None),
        (["status"], None),
        (["hook", "reseed"], None),
        (["egress", "local", "--json"], None),
    ]


def _recorded_id(output: str) -> str:
    """The id a `<verb> add` command minted: ``Recorded X-NN...`` -> ``X-NN``."""
    assert output.startswith("Recorded "), output
    return output.split()[1].rstrip(".")


# The one line of the status block that says where the project lives.
_WHERE_LINE = r"^(Dir:     .*|Remote:  .*)$"


def _normalize(text: str, project_dirs: list[str]) -> str:
    """Fold what may differ by locality and nothing else.

    A path to an artifact in the checkout (``docs/projects/x/spec.md``) reads
    as the locator a hosted run prints for it (``x/spec``); the one line that
    says where the project lives (``Dir:`` here, ``Remote:`` there) becomes a
    placeholder; and the Actor lines only a daemon-held entry carries go.
    Anything else that differs is a real difference and fails the comparison.
    """
    spellings = "|".join(re.escape(d) for d in sorted(project_dirs, key=len, reverse=True))
    text = re.sub(rf"(?:{spellings})/([A-Za-z0-9_-]+)\.md", rf"{SLUG}/\1", text)
    text = re.sub(_WHERE_LINE, "<where>", text, flags=re.MULTILINE)
    return re.sub(r"^- Actor: .*\n", "", text, flags=re.MULTILINE)


def _run(
    checkout: Path, project_dirs: list[str], new_args: list[str], raw: list | None = None,
    steps: list | None = None,
):
    """Create the project with ``new_args`` and drive ``steps`` (the pipeline by
    default); returns the exit code and normalized stdout of every step, the
    creation first. ``raw`` collects each step's stdout before normalization
    when given."""
    ids = {}
    results = []
    # The review report a step ingests with --file: a file of the checkout,
    # read by the client wherever the round itself lives.
    (checkout / "report.md").write_text("# Round 1\n\n## Findings\n\n- none\n")
    result = runner.invoke(app, ["new", "Parity Thing", "--summary", "One line", *new_args])
    results.append((result.exit_code, _normalize(result.stdout, project_dirs)))
    if raw is not None:
        raw.append((["new"], result.stdout))
    for args, stdin in (_pipeline() if steps is None else steps):
        resolved = [arg(ids) if callable(arg) else arg for arg in args]
        result = runner.invoke(app, resolved, input=stdin)
        if resolved[:2] == ["decision", "add"]:
            ids["decision"] = _recorded_id(result.stdout)
        elif resolved[:2] == ["requirement", "add"]:
            ids["requirement"] = _recorded_id(result.stdout)
        elif resolved[:2] == ["task", "add"]:
            ids["task"] = _recorded_id(result.stdout)
        results.append((resolved, result.exit_code, _normalize(result.stdout, project_dirs)))
        if raw is not None:
            raw.append((resolved, result.stdout))
    return results


def _recording_responses(monkeypatch) -> list[tuple[str, str]]:
    """Every response body the CLI's HTTP client receives, by route, as it arrives."""
    bodies = []
    real_send = httpx.Client.send

    def recording_send(self, request, **kwargs):
        response = real_send(self, request, **kwargs)
        bodies.append((request.url.path, response.text))
        return response

    monkeypatch.setattr(httpx.Client, "send", recording_send)
    return bodies


@pytest.fixture
def local_run(tmp_path, monkeypatch):
    checkout = tmp_path / "local"
    checkout.mkdir()
    config.init_config(checkout)
    monkeypatch.chdir(checkout)
    project_dir = checkout / "docs" / "projects" / SLUG
    return _run(checkout, [str(project_dir), f"docs/projects/{SLUG}"], [])


@pytest.fixture
def hosted_run(tmp_path, monkeypatch, live_daemon):
    checkout = tmp_path / "hosted"
    checkout.mkdir()
    config.init_config(checkout)
    monkeypatch.chdir(checkout)
    registered = runner.invoke(
        app, ["remote", "add", "home", live_daemon["url"], "--token", live_daemon["token"]]
    )
    assert registered.exit_code == 0, registered.output
    project_dir = live_daemon["root"] / daemon.PROJECTS_DIRNAME / SLUG
    bodies = _recording_responses(monkeypatch)
    raw = []
    results = _run(checkout, [str(project_dir), f"projects/{SLUG}"], ["--remote", "home"], raw)
    assert not list((checkout / "docs" / "projects").glob(f"{SLUG}*"))
    assert (project_dir / "checkpoint.md").is_file()
    # The wire is a boundary too: what the daemon answers names nothing of its
    # host, neither its root nor any absolute path, whatever a client does with it.
    assert bodies, "the hosted run reached the daemon"
    for route, body in bodies:
        assert str(live_daemon["root"]) not in body, f"{route} carries the daemon root:\n{body}"
        assert not re.search(r'"/', body), f"{route} carries an absolute path:\n{body}"
    # The daemon's directory layout is the daemon's business: no command
    # names it on the client, by its absolute path or by its spelling relative
    # to the daemon root. What a hosted run prints for an artifact is its
    # locator, and status says which remote holds the project.
    for args, stdout in raw:
        for spelling in (str(project_dir), f"projects/{SLUG}"):
            assert spelling not in stdout, f"{' '.join(args)} names the daemon dir:\n{stdout}"
    # The two documented differences from a local run, present before the
    # normalizer folds them: nothing else may differ.
    by_args = {tuple(args): stdout for args, stdout in raw}
    assert "Remote:  home\n" in by_args[("status",)]
    assert "- Actor: developer\n" in by_args[("task", "show")]
    return results


def test_the_pipeline_is_identical_for_a_local_and_a_hosted_project(local_run, hosted_run):
    assert len(local_run) == len(hosted_run) == len(_pipeline()) + 1
    for local, hosted in zip(local_run[1:], hosted_run[1:]):
        args = local[0]
        assert local[1:] == hosted[1:], f"{' '.join(args)}:\nlocal:\n{local[2]}\nhosted:\n{hosted[2]}"
    assert local_run[0] == hosted_run[0]


def test_the_scenario_reaches_completion_and_prints_every_seam(local_run):
    codes = [code for _, code, _ in local_run[1:]]
    # The first validate fails on purpose (an empty Out of scope section), so
    # do the second gate open (one is already open) and the second take (none
    # is open); every other step passes: a scenario that never exercised a
    # refusal would prove less than one that did.
    assert codes.count(1) == 3 and set(codes) <= {0, 1}
    outputs = "\n".join(text for _, _, text in local_run[1:])
    for expected in (
        "Recorded ",
        "Advanced 'parity-thing' from brainstorm to spec.",
        "Advanced 'parity-thing' from spec to plan.",
        "Advanced 'parity-thing' from plan to execute.",
        "parity-thing/review-1 closed ready-to-merge",
        "Completed project 'parity-thing'.",
        "Opened a gate for developer on 'parity-thing'.",
        "Took the gate for developer on 'parity-thing'.",
        "Checkpoint saved: parity-thing/checkpoint",
        "- parity-thing/brainstorm",
        "<where>",
    ):
        assert expected in outputs, expected


def _steps(run, *args):
    """The ``(exit code, stdout)`` of every step of ``run`` invoked with ``args``."""
    return [(code, text) for step, code, text in run[1:] if step == list(args)]


def test_the_egress_pin_is_recorded_and_shown_for_a_local_and_a_hosted_project(
    local_run, hosted_run, live_daemon
):
    for run in (local_run, hosted_run):
        assert _steps(run, "egress", "local") == [(0, "Egress class for 'parity-thing': local\n")]
        # The pin was set in the first phase; the record shown three advances
        # later still carries it, and setting it again changes nothing.
        ((code, shown),) = _steps(run, "doc", "show", "project")
        assert code == 0 and "\negress: local\n" in shown
        assert _steps(run, "egress", "local", "--json") == [
            (0, '{"egress": "local", "changed": false}\n')
        ]
    # The hosted pin is on the daemon's record of the project, where the
    # daemon reads it; the pipeline ran on to completion with it in place.
    record = live_daemon["root"] / daemon.PROJECTS_DIRNAME / SLUG / "project.md"
    assert "\negress: local\n" in record.read_text()
    assert "\nstatus: complete\n" in record.read_text()


def test_an_unknown_egress_class_is_refused_the_same_way_locally_and_hosted(
    tmp_path, monkeypatch, local_run, hosted_run, live_daemon
):
    refusals = []
    for checkout in ("hosted", "local"):
        monkeypatch.chdir(tmp_path / checkout)
        result = runner.invoke(app, ["egress", "public"])
        assert result.exit_code == 1, result.output
        refusals.append(result.output)
    assert refusals[0] == refusals[1]
    assert "'public'" in refusals[0]
    assert all(name in refusals[0] for name in ("local", "no-train", "open"))
    record = live_daemon["root"] / daemon.PROJECTS_DIRNAME / SLUG / "project.md"
    assert "\negress: local\n" in record.read_text()


# --- the review loop, local and hosted ---------------------------------------------------


def _fix(text: str, item: str, task_id: str) -> list:
    """The steps that add, start and finish ``task_id``, a task fixing ``item``."""
    return [
        (["task", "add", "--text", text, "--acceptance", "fixed", "--verify", "uv run pytest",
          "--fixes", item], None),
        (["task", "start", task_id], None),
        (["task", "done", task_id], None),
    ]


def _review_steps():
    """Every review command and flag, refusals included, over four rounds,
    with the fix task each round that asks for changes needs."""
    return [
        (["review", "start"], None),
        (["review", "prompt"], None),
        (["review", "finding", "add", "--severity", "blocker", "--at", "src/app.py:3", "--text", "The close drops the sha"], None),
        (["review", "finding", "add", "--severity", "nit", "--text", "A name reads oddly"], None),
        (["review", "finding", "add", "--severity", "major", "--text", "Refused"], None),
        (["review", "done", "--verdict", "ready-to-merge"], None),
        (["review", "done"], None),
        (["plan", "start"], None),
        *_fix("Keep the sha", "F-01", "T-01"),
        (["review", "start", "--full"], None),
        (["review", "prompt"], None),
        (["review", "finding", "check", "F-02", "closed"], None),
        (["review", "finding", "check", "F-01", "open"], None),
        (["review", "finding", "add", "--severity", "should-fix", "--at", "src/app.py:9-12", "--text", "A message names the wrong command"], None),
        (["review", "done"], None),
        *_fix("Name the right command", "F-03", "T-02"),
        (["review", "start"], None),
        (["review", "start", "--over-budget"], None),
        (["review", "done"], None),
        (["review", "finding", "check", "F-01", "closed"], None),
        (["review", "finding", "check", "F-03", "closed"], None),
        (["review", "finding", "add", "--severity", "nit", "--text", "A typo"], None),
        (["review", "done"], None),
        (["review", "waive", "--reason", ""], None),
        (["review", "waive", "--reason", "Checked by hand"], None),
        (["review", "prompt"], None),
        (["doc", "show", "review-1"], None),
        (["doc", "show", "review-2"], None),
        (["doc", "show", "review-3"], None),
        (["doc", "show", "review-4"], None),
        (["status"], None),
    ]


def _local_steps(tmp_path, monkeypatch, steps):
    """``steps`` against a project in a checkout; the results and the project dir."""
    checkout = tmp_path / "local"
    checkout.mkdir()
    config.init_config(checkout)
    monkeypatch.chdir(checkout)
    project_dir = checkout / "docs" / "projects" / SLUG
    dirs = [str(project_dir), f"docs/projects/{SLUG}"]
    return _run(checkout, dirs, [], steps=steps), project_dir


def _hosted_steps(tmp_path, monkeypatch, live_daemon, steps):
    """``steps`` against a project on a daemon; the results and the project dir."""
    checkout = tmp_path / "hosted"
    checkout.mkdir()
    config.init_config(checkout)
    monkeypatch.chdir(checkout)
    registered = runner.invoke(
        app, ["remote", "add", "home", live_daemon["url"], "--token", live_daemon["token"]]
    )
    assert registered.exit_code == 0, registered.output
    project_dir = live_daemon["root"] / daemon.PROJECTS_DIRNAME / SLUG
    dirs = [str(project_dir), f"projects/{SLUG}"]
    return _run(checkout, dirs, ["--remote", "home"], steps=steps), project_dir


@pytest.fixture
def local_review_run(tmp_path, monkeypatch):
    return _local_steps(tmp_path, monkeypatch, _review_steps())


@pytest.fixture
def hosted_review_run(tmp_path, monkeypatch, live_daemon):
    return _hosted_steps(tmp_path, monkeypatch, live_daemon, _review_steps())


def _without_follow_up_lines(brief: str) -> str:
    """A reviewer brief less the lines that say where follow-ups and nits go."""
    return "\n".join(
        line for line in brief.splitlines()
        if not any(word in line.lower() for word in ("follow-up", "followup", "stays listed"))
    )


def test_the_review_loop_is_identical_for_a_local_and_a_hosted_project(
    local_review_run, hosted_review_run
):
    local, local_dir = local_review_run
    hosted, hosted_dir = hosted_review_run
    assert len(local) == len(hosted) == len(_review_steps()) + 1
    for mine, theirs in zip(local[1:], hosted[1:]):
        args = mine[0]
        if args[:2] == ["review", "prompt"] and mine[1] == 0:
            # The one documented difference: follow-ups work only for projects
            # in a checkout, so a hosted brief asks for such problems in the
            # reply and keeps nits in the round.
            assert "specflo followup add" in mine[2] and "specflo followup add" not in theirs[2]
            mine = (args, mine[1], _without_follow_up_lines(mine[2]))
            theirs = (args, theirs[1], _without_follow_up_lines(theirs[2]))
        assert mine[1:] == theirs[1:], f"{' '.join(args)}:\nlocal:\n{mine[2]}\nhosted:\n{theirs[2]}"
    # The round files themselves, byte for byte.
    names = sorted(p.name for p in local_dir.glob("review-*.md"))
    assert names == ["review-1.md", "review-2.md", "review-3.md", "review-4.md"]
    for name in names:
        assert (local_dir / name).read_text() == (hosted_dir / name).read_text(), name


def test_the_review_scenario_exercises_each_outcome(local_review_run):
    results, _ = local_review_run
    steps = [(tuple(args), code, text) for args, code, text in results[1:]]

    def outcome(args, nth=0):
        return [(code, text) for step, code, text in steps if step == args][nth]

    assert outcome(("review", "finding", "add", "--severity", "major", "--text", "Refused"))[0] == 1
    assert outcome(("review", "done", "--verdict", "ready-to-merge"))[0] == 1
    assert "changes-requested (1 blocker, 0 should-fix, 1 nit)" in outcome(("review", "done"), 0)[1]
    assert "still open: F-01" in outcome(("review", "done"), 1)[1]
    assert outcome(("review", "start"), 1)[0] == 1              # the budget refuses round 3
    assert "Items to check: F-01, F-03" in outcome(("review", "start", "--over-budget"))[1]
    assert outcome(("review", "done"), 2)[0] == 1                # F-01 and F-03 unchecked
    assert "ready-to-merge (0 blocker, 0 should-fix, 1 nit)" in outcome(("review", "done"), 3)[1]
    assert outcome(("review", "waive", "--reason", ""))[0] == 1
    assert "review-4 closed waived" in outcome(("review", "waive", "--reason", "Checked by hand"))[1]
    assert outcome(("review", "prompt"), 2)[0] == 1             # no round open after the waive


TEST_COMMAND = "run-the-suite-sentinel"


def _test_command_steps():
    """Set test_command in the checkout, where the code and its suite live, and
    print the open round's brief."""
    return [
        (["config", "set", "test_command", TEST_COMMAND], None),
        (["review", "start"], None),
        (["review", "prompt"], None),
    ]


def test_a_set_test_command_gives_a_hosted_reviewer_the_same_brief(
    tmp_path, monkeypatch, live_daemon
):
    local, _ = _local_steps(tmp_path, monkeypatch, _test_command_steps())
    hosted, _ = _hosted_steps(tmp_path, monkeypatch, live_daemon, _test_command_steps())
    briefs = []
    for run in (local, hosted):
        ((code, brief),) = [
            (code, text) for args, code, text in run[1:] if args[:2] == ["review", "prompt"]
        ]
        assert code == 0, brief
        assert f"`{TEST_COMMAND}`" in brief.split("\n## Tests\n", 1)[1], brief
        briefs.append(_without_follow_up_lines(brief))
    assert briefs[0] == briefs[1], f"local:\n{briefs[0]}\nhosted:\n{briefs[1]}"


# The surfaces that print the next-step hint, the session-start ones included.
_HINT_SURFACES = [
    ["status"],
    ["checkpoint"],
    ["hook", "reseed"],
    ["hook", "reseed", "--format", "claude"],
]


def _whole_suite_steps(test_command: str | None = TEST_COMMAND):
    """Set ``test_command`` in the checkout (None leaves it unset), finish
    every task, and read the hint on each surface where it calls for the
    whole suite: before the first round, and once a round that asked for
    changes passes. The first round's brief is read too."""
    pipeline = _pipeline()
    last = next(i for i, (args, _) in enumerate(pipeline) if args[:2] == ["task", "done"])
    surfaces = [(args, None) for args in _HINT_SURFACES]
    setting = [] if test_command is None else [
        (["config", "set", "test_command", test_command], None)
    ]
    return [
        *setting,
        *pipeline[: last + 1],
        *surfaces,
        (["review", "start"], None),
        (["review", "prompt"], None),
        (["review", "finding", "add", "--severity", "blocker", "--at", "src/app.py:1", "--text", "One"], None),
        (["review", "done"], None),
        *_fix("Fix one", "F-01", "T-02"),
        (["review", "start"], None),
        (["review", "finding", "check", "F-01", "closed"], None),
        (["review", "finding", "add", "--severity", "nit", "--text", "A typo"], None),
        (["review", "done"], None),
        *surfaces,
    ]


def _session_start(text: str) -> str:
    """The Claude session-start JSON as its two texts, the status block's
    where line folded as the plain status output's is."""
    payload = json.loads(text)
    message = re.sub(_WHERE_LINE, "<where>", payload["systemMessage"], flags=re.MULTILINE)
    return f"{message}\n{payload['hookSpecificOutput']['additionalContext']}"


def _assert_whole_suite_alike(local, hosted, hint: str) -> None:
    """Every step of a whole-suite run prints alike locally and hosted, and
    each surface where the hint calls for the whole suite words it as ``hint``."""
    named = 0
    for mine, theirs in zip(local[1:], hosted[1:]):
        args = mine[0]
        claude = args[-2:] == ["--format", "claude"]
        if claude:
            mine = (args, mine[1], _session_start(mine[2]))
            theirs = (args, theirs[1], _session_start(theirs[2]))
        elif args[:2] == ["review", "prompt"]:
            # A brief's one documented difference: where follow-ups go.
            assert mine[1] == 0, mine[2]
            mine = (args, mine[1], _without_follow_up_lines(mine[2]))
            theirs = (args, theirs[1], _without_follow_up_lines(theirs[2]))
        assert mine[1:] == theirs[1:], f"{' '.join(args)}:\nlocal:\n{mine[2]}\nhosted:\n{theirs[2]}"
        # The pipeline's last task done; the fix task's done hints at the review.
        if args in _HINT_SURFACES or args == ["task", "done", "T-01"]:
            # The session-start JSON names it twice: in the status block and
            # in the checkpoint.
            assert mine[1] == 0, mine[2]
            assert mine[2].count(hint) == (2 if claude else 1), f"{' '.join(args)}:\n{mine[2]}"
            named += 1
    # The last task done, then each surface before the first round and after
    # the fixes.
    assert named == 1 + 2 * len(_HINT_SURFACES)


def test_a_set_test_command_names_the_whole_suite_alike_on_every_hosted_surface(
    tmp_path, monkeypatch, live_daemon
):
    steps = _whole_suite_steps()
    local, _ = _local_steps(tmp_path, monkeypatch, steps)
    hosted, _ = _hosted_steps(tmp_path, monkeypatch, live_daemon, steps)
    assert len(local) == len(hosted) == len(steps) + 1
    _assert_whole_suite_alike(local, hosted, f"whole test suite (`{TEST_COMMAND}`)")


DAEMON_TEST_COMMAND = "daemon-root-sentinel"


def test_a_test_command_in_the_daemon_root_is_named_on_no_hosted_surface(
    tmp_path, monkeypatch, live_daemon
):
    # The daemon root's config sets one and the checkout's none: the daemon
    # holds only the documents, so a hosted run reads as a local one with no
    # command set, on every surface.
    config.write_value(live_daemon["root"], config.field_for("test_command"), DAEMON_TEST_COMMAND)
    # The daemon rewrites its checkpoint file on each mutation: read the file
    # after each step that writes it while the hint calls for the whole suite.
    steps = []
    for step in _whole_suite_steps(None):
        steps.append(step)
        if step[0] == ["checkpoint"] or step[0][:2] == ["task", "done"]:
            steps.append((["doc", "show", "checkpoint"], None))
    local, _ = _local_steps(tmp_path, monkeypatch, steps)
    hosted, _ = _hosted_steps(tmp_path, monkeypatch, live_daemon, steps)
    assert len(local) == len(hosted) == len(steps) + 1
    _assert_whole_suite_alike(local, hosted, "whole test suite once")
    for args, _, text in hosted[1:]:
        assert DAEMON_TEST_COMMAND not in text, f"{' '.join(args)}:\n{text}"


# --- finding locations and regression marks, local and hosted ----------------------------


def _recording_git(monkeypatch) -> list[tuple[bool, list[str]]]:
    """Every git process this test process starts from now on, as it starts:
    whether the test's own thread started it, and its argv.

    The CLI runs on the test's thread. The live daemon serves each request on
    a thread of its own, so a git process started on any other thread is one
    the daemon ran.
    """
    calls = []
    client = threading.current_thread()
    real_popen = subprocess.Popen

    class RecordingPopen(real_popen):
        def __init__(self, args, *rest, **kwargs):
            argv = [str(arg) for arg in args] if isinstance(args, (list, tuple)) else [str(args)]
            if Path(argv[0]).name == "git":
                calls.append((threading.current_thread() is client, argv))
            super().__init__(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", RecordingPopen)
    return calls


def _assert_only_the_client_ran_git(git, daemon_root: Path) -> None:
    """The client blamed the lines in its checkout; the daemon, which holds no
    git repository, ran no git at all and took the marks as data."""
    assert not (daemon_root / ".git").exists()
    assert [argv for on_client, argv in git if not on_client] == []
    assert any("blame" in argv for on_client, argv in git if on_client), git


def _marked_rounds(checkout: Path) -> Path:
    return checkout / "docs" / "projects" / MARKED_SLUG


def test_an_add_on_a_line_a_fix_changed_is_marked_alike_and_only_the_client_runs_git(
    tmp_path, monkeypatch, live_daemon
):
    local, _ = _regression_run(tmp_path, monkeypatch, "local", [])
    git = _recording_git(monkeypatch)
    hosted, sent = _regression_run(
        tmp_path, monkeypatch, "hosted", ["--remote", "home"],
        register=[live_daemon["url"], "--token", live_daemon["token"]],
    )

    for mine, theirs in zip(local, hosted, strict=True):
        assert mine == theirs, f"{' '.join(mine[0])}:\nlocal: {mine[1:]}\nhosted: {theirs[1:]}"
    # The add on the line the fix changed succeeds with no note: the client
    # could blame it, though the daemon holds no code.
    add = ("review", "finding", "add", "--severity", "blocker", "--at", "src/x.py:10",
           "--text", "The fix broke it")
    assert (add, 0, "Recorded F-02 in thing/review-2.\n", "") in hosted
    hosted_rounds = live_daemon["root"] / daemon.PROJECTS_DIRNAME / MARKED_SLUG
    for rounds in (_marked_rounds(tmp_path / "local"), hosted_rounds):
        assert _findings(rounds / "review-2.md")[0] == (
            "- F-02 (blocker, regression) [src/x.py:10] The fix broke it"
        )
    assert sent[1]["regression"] is True
    _assert_only_the_client_ran_git(git, live_daemon["root"])


def test_a_hosted_review_done_marks_a_hand_written_line_as_a_local_one_does(
    tmp_path, monkeypatch, live_daemon
):
    local, _, local_rounds = _hand_written_run(
        tmp_path, monkeypatch, "local", [], _marked_rounds,
    )
    git = _recording_git(monkeypatch)
    hosted, sent, hosted_rounds = _hand_written_run(
        tmp_path, monkeypatch, "hosted", ["--remote", "home"],
        lambda _: live_daemon["root"] / daemon.PROJECTS_DIRNAME / MARKED_SLUG,
        register=[live_daemon["url"], "--token", live_daemon["token"]],
    )

    for mine, theirs in zip(local, hosted, strict=True):
        assert mine == theirs, f"{' '.join(mine[0])}:\nlocal: {mine[1:]}\nhosted: {theirs[1:]}"
    assert [code for _, code, _, _ in hosted] == [0] * len(hosted)
    # A line written in the round file and one ingested with --file, each on
    # a line a fix changed, are marked as the round closes.
    for rounds in (local_rounds, hosted_rounds):
        assert _findings(rounds / "review-2.md")[0] == (
            "- F-02 (blocker, regression) [src/x.py:10] The fix broke it"
        )
        assert _findings(rounds / "review-3.md")[0] == (
            "- F-05 (should-fix, regression) [src/x.py:19-20] And here"
        )
    for name in ("review-2.md", "review-3.md"):
        assert (local_rounds / name).read_text() == (hosted_rounds / name).read_text()
    assert sent == [[], ["F-02"], ["F-05"]]
    _assert_only_the_client_ran_git(git, live_daemon["root"])

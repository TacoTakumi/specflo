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

from specflo import config, daemon, followup, review
from specflo.cli import app
from test_review_location import _commit, _git
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


def _assert_the_daemon_ran_no_git(git, daemon_root: Path) -> None:
    """The daemon holds no git repository and ran no git at all."""
    assert not (daemon_root / ".git").exists()
    assert [argv for on_client, argv in git if not on_client] == []


def _assert_only_the_client_ran_git(git, daemon_root: Path) -> None:
    """The client blamed the lines in its checkout; the daemon, which holds no
    git repository, ran no git at all and took the marks as data."""
    _assert_the_daemon_ran_no_git(git, daemon_root)
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


# --- settling a finding without a fix, local and hosted ----------------------------------
# The settle suite's scenario and helpers live in test_review_settle, which
# imports this module, so each test here imports them as it runs.

_LOCK_REASON = "The caller holds the lock"
_REJECT_VERB = ["review", "finding", "reject"]


def _reject_steps():
    """Round one asks for changes for should-fix F-01, and a fix for it is
    done; round two checks F-01 open, records blocker F-02 and asks for
    changes. Each item is then rejected, and F-01 once more, and the rounds
    and the status are shown."""
    from test_review_settle import REASON, _round_one

    return [
        *_round_one(),
        *_fix("Name the right command", "F-01", "T-02"),
        (["review", "start"], None),
        (["review", "finding", "check", "F-01", "open"], None),
        (["review", "finding", "add", "--severity", "blocker", "--at", "src/app.py:20",
          "--text", "The lock is dropped early"], None),
        (["review", "done"], None),
        ([*_REJECT_VERB, "F-01", "--reason", REASON], None),
        ([*_REJECT_VERB, "F-02", "--reason", _LOCK_REASON], None),
        ([*_REJECT_VERB, "F-01", "--reason", "Again"], None),
        (["doc", "show", "review-1"], None),
        (["doc", "show", "review-2"], None),
        (["status"], None),
    ]


def _settled(path: Path) -> list[str]:
    """The lines under the one Settled heading of the round file at ``path``."""
    text = path.read_text()
    assert text.count("\n## Settled\n") == 1, text
    return text.split("\n## Settled\n", 1)[1].strip("\n").splitlines()


def test_a_hosted_reject_writes_the_same_settled_line_as_a_local_one_and_the_daemon_runs_no_git(
    tmp_path, monkeypatch, live_daemon
):
    from test_review_settle import REASON

    steps = _reject_steps()
    local, local_dir = _local_steps(tmp_path, monkeypatch, steps)
    git = _recording_git(monkeypatch)
    hosted, hosted_dir = _hosted_steps(tmp_path, monkeypatch, live_daemon, steps)

    assert len(local) == len(hosted) == len(steps) + 1
    for mine, theirs in zip(local[1:], hosted[1:], strict=True):
        assert mine == theirs, f"{' '.join(mine[0])}:\nlocal:\n{mine[2]}\nhosted:\n{theirs[2]}"
    rejects = [(code, text) for args, code, text in hosted[1:] if args[:3] == _REJECT_VERB]
    assert rejects == [
        (0, f"Rejected F-01 in {SLUG}/review-1.\n"),
        (0, f"Rejected F-02 in {SLUG}/review-2.\n"),
        (1, ""),
    ]
    # The pipeline's first validate fails on purpose; past it only the second
    # reject of F-01 is refused.
    assert [args for args, code, _ in hosted[1:] if code != 0] == [
        ["validate", "brainstorm"], [*_REJECT_VERB, "F-01", "--reason", "Again"]
    ]
    # Each line lands in the round that recorded its finding, once, the same
    # locally and hosted.
    for rounds in (local_dir, hosted_dir):
        assert _settled(rounds / "review-1.md") == [f"- F-01 rejected: {REASON}"]
        assert _settled(rounds / "review-2.md") == [f"- F-02 rejected: {_LOCK_REASON}"]
    for name in ("review-1.md", "review-2.md"):
        assert (local_dir / name).read_text() == (hosted_dir / name).read_text(), name
    _assert_the_daemon_ran_no_git(git, live_daemon["root"])


def test_a_hosted_defer_is_refused_naming_the_follow_up_that_routes_it_and_changes_no_document(
    tmp_path, monkeypatch, live_daemon
):
    from test_review_settle import DO, _DEFER, _round_one, _snapshot

    local, local_dir = _local_steps(tmp_path, monkeypatch, [*_round_one(), (_DEFER, None)])
    hosted, hosted_dir = _hosted_steps(tmp_path, monkeypatch, live_daemon, _round_one())

    # The runs are alike up to the defer, which a local project takes.
    for mine, theirs in zip(local[1:-1], hosted[1:], strict=True):
        assert mine == theirs, f"{' '.join(mine[0])}:\nlocal:\n{mine[2]}\nhosted:\n{theirs[2]}"
    assert local[-1] == (
        _DEFER, 0, f"Deferred F-01 in {SLUG}/review-1 to FU-01 in {SLUG}/followup.\n"
    )
    daemon_root, docs = live_daemon["root"], tmp_path / "hosted" / "docs"
    before = (_snapshot(daemon_root), _snapshot(docs))
    git = _recording_git(monkeypatch)

    # Whatever else it asks, a hosted defer is refused, and the refusal names
    # the follow-up that will route a hosted project's follow-ups.
    for args in (
        _DEFER,
        [*_DEFER, "--json"],
        ["review", "finding", "defer", "F-09", "--do", DO],
        ["review", "finding", "defer", "F-01", "--do", ""],
    ):
        result = runner.invoke(app, args)
        assert result.exit_code == 1, (args, result.output)
        assert "FU-90" in result.output, (args, result.output)
        assert result.stdout == "", (args, result.stdout)

    # No document changed, on the daemon or in the checkout, and the daemon
    # ran no git: the hosted round is the local one as it stood before its
    # defer.
    assert (_snapshot(daemon_root), _snapshot(docs)) == before
    _assert_the_daemon_ran_no_git(git, daemon_root)
    assert (local_dir / "review-1.md").read_text() == (
        (hosted_dir / "review-1.md").read_text() + "\n## Settled\n\n- F-01 deferred FU-01\n"
    )
    assert list(daemon_root.rglob("followup.md")) == []


# --- every new verb and option in one scenario, local and hosted -------------------------
# Each run starts from a git checkout whose commits carry fixed dates, so the
# local and the hosted checkout share every sha: a round's sha, a location
# checked at it and a regression mark blamed there read alike. A step's
# stderr is compared too, so a refusal must read alike, not only fail alike.

# Thirty lines, committed as src/app.py.
APP_PY = "".join(f"line {n}\n" for n in range(1, 31))


def _git_checkout(path: Path) -> Path:
    """A checkout at ``path`` whose one commit holds src/app.py."""
    path.mkdir()
    _git(path, "init", "-q")
    _commit(path, "src/app.py", APP_PY)
    config.init_config(path)
    return path


def _fix_commit(*lines: int):
    """A step of the checkout, not of the CLI: commit src/app.py with each of
    ``lines`` rewritten, as a fix does. Its stdout is the commit's short sha."""

    def commit(checkout: Path) -> str:
        text = (checkout / "src" / "app.py").read_text().splitlines(keepends=True)
        for line in lines:
            text[line - 1] = f"line {line} fixed\n"
        return _commit(checkout, "src/app.py", "".join(text)) + "\n"

    return commit


def _drive(checkout: Path, project_dirs: list[str], new_args: list[str], steps: list):
    """Create the project with ``new_args`` and drive ``steps`` in ``checkout``:
    each step's ``(args, exit code, stdout, stderr)``, normalized, the creation
    first. A step whose args are a callable is a commit, its sha its stdout; an
    argument callable is resolved against the ids the earlier adds recorded."""
    ids = {}
    results = []
    created = runner.invoke(app, ["new", "Parity Thing", "--summary", "One line", *new_args])
    results.append((["new"], created.exit_code, _normalize(created.stdout, project_dirs),
                    _normalize(created.stderr, project_dirs)))
    for args, stdin in steps:
        if callable(args):
            results.append((["commit"], 0, args(checkout), ""))
            continue
        resolved = [arg(ids) if callable(arg) else arg for arg in args]
        result = runner.invoke(app, resolved, input=stdin)
        if resolved[1:2] == ["add"] and result.stdout.startswith("Recorded "):
            ids[resolved[0]] = _recorded_id(result.stdout)
        results.append((resolved, result.exit_code, _normalize(result.stdout, project_dirs),
                        _normalize(result.stderr, project_dirs)))
    return results


def _local_drive(tmp_path, monkeypatch, steps, new_args=()):
    """``steps`` against a project in a git checkout; the results and the project dir."""
    checkout = _git_checkout(tmp_path / "local")
    monkeypatch.chdir(checkout)
    project_dir = checkout / "docs" / "projects" / SLUG
    dirs = [str(project_dir), f"docs/projects/{SLUG}"]
    return _drive(checkout, dirs, list(new_args), steps), project_dir


def _hosted_drive(tmp_path, monkeypatch, live_daemon, steps, new_args=()):
    """``steps`` against a project on a daemon, from a git checkout like the
    local one; the results and the project dir."""
    checkout = _git_checkout(tmp_path / "hosted")
    monkeypatch.chdir(checkout)
    registered = runner.invoke(
        app, ["remote", "add", "home", live_daemon["url"], "--token", live_daemon["token"]]
    )
    assert registered.exit_code == 0, registered.output
    project_dir = live_daemon["root"] / daemon.PROJECTS_DIRNAME / SLUG
    dirs = [str(project_dir), f"projects/{SLUG}"]
    return _drive(checkout, dirs, [*new_args, "--remote", "home"], steps), project_dir


def _assert_driven_alike(local, hosted) -> None:
    """Every step of the two runs exits and prints alike, stderr included. A
    reviewer brief is compared less its one documented difference: where a
    problem the round does not own goes, a follow-up in a checkout and the
    reply for a daemon's project."""
    assert len(local) == len(hosted)
    for mine, theirs in zip(local, hosted, strict=True):
        args = mine[0]
        if args[:2] == ["review", "prompt"] and mine[1] == 0:
            assert "specflo followup add" in mine[2] and "specflo followup add" not in theirs[2]
            mine = (args, mine[1], _without_follow_up_lines(mine[2]), mine[3])
            theirs = (args, theirs[1], _without_follow_up_lines(theirs[2]), theirs[3])
        assert mine == theirs, (
            f"{' '.join(args)}:\nlocal:\n{mine[2]}{mine[3]}\nhosted:\n{theirs[2]}{theirs[3]}"
        )


def _documents(project_dir: Path) -> dict[str, str]:
    """Every document of the project at ``project_dir``, by file name, folded
    as a step's stdout is: paths read as locators and Actor lines go."""
    dirs = [str(project_dir), f"docs/projects/{SLUG}", f"projects/{SLUG}"]
    return {path.name: _normalize(path.read_text(), dirs) for path in project_dir.glob("*.md")}


def _assert_documents_alike(local: dict[str, str], hosted: dict[str, str]) -> None:
    """The two projects hold the same documents, each reading alike."""
    assert sorted(local) == sorted(hosted)
    for name, text in hosted.items():
        assert local[name] == text, f"{name}:\nlocal:\n{local[name]}\nhosted:\n{text}"


def _driven(results, *args) -> list[tuple[int, str, str]]:
    """The ``(exit code, stdout, stderr)`` of each step of ``results`` run with ``args``."""
    return [(code, out, err) for step, code, out, err in results if step == list(args)]


def _next_hint(status: str) -> str:
    """The Next line of a ``status`` output, without its label."""
    (line,) = [line for line in status.splitlines() if line.startswith("Next:")]
    return line[len("Next:"):].strip()


_ADD_FINDING = ["review", "finding", "add", "--severity"]
_ADD_FIX = ["task", "add", "--acceptance", "fixed", "--verify", "uv run pytest"]
_REJECTED_WHY = "The caller holds the lock"


def _feature_steps():
    """A full-level project through T-01 done, then each new verb and option
    in turn. Gate round 1, at the first commit, asks for changes on blocker
    F-01 and should-fix F-02, each located, and keeps nit F-03; a fix task
    for the nit is refused, and round 2 is refused until the fix tasks are
    done. Round 2, after a fix commit, checks both closed and raises blocker
    F-04 on a line the fix changed, which the add marks a regression; it is
    then rejected. The gate budget is spent, so a plain start is refused; harden
    round 3 raises should-fix F-05, and harden round 4 checks its fix
    closed. The completion gate is read after each move."""
    pipeline = _pipeline()
    done = next(i for i, (args, _) in enumerate(pipeline) if args[:2] == ["task", "done"])
    return [
        *pipeline[: done + 1],
        (["review", "start"], None),
        ([*_ADD_FINDING, "blocker", "--at", "src/app.py:3",
          "--text", "The close drops the sha"], None),
        ([*_ADD_FINDING, "should-fix", "--at", "src/app.py:9-11",
          "--text", "A message names the wrong command"], None),
        ([*_ADD_FINDING, "nit", "--text", "A name reads oddly"], None),
        (["review", "done"], None),
        ([*_ADD_FIX, "--text", "Tidy the name", "--fixes", "F-03"], None),
        ([*_ADD_FIX, "--text", "Keep the sha", "--fixes", "F-01"], None),
        ([*_ADD_FIX, "--text", "Name the right command", "--fixes", "F-02"], None),
        (["review", "start"], None),
        (["task", "start", "T-02"], None),
        (["task", "done", "T-02"], None),
        (["task", "start", "T-03"], None),
        (["task", "done", "T-03"], None),
        (_fix_commit(3, 10), None),
        (["review", "start"], None),
        (["review", "prompt"], None),
        (["review", "finding", "check", "F-01", "closed"], None),
        (["review", "finding", "check", "F-02", "closed"], None),
        ([*_ADD_FINDING, "blocker", "--at", "src/app.py:10",
          "--text", "The fix drops the lock"], None),
        (["doc", "show", "review-2"], None),
        (["review", "done"], None),
        (["validate", "execute"], None),
        (["review", "finding", "reject", "F-04", "--reason", _REJECTED_WHY], None),
        (["validate", "execute"], None),
        (["review", "start"], None),
        (["review", "start", "--harden"], None),
        (["review", "prompt"], None),
        ([*_ADD_FINDING, "should-fix", "--at", "src/app.py:20",
          "--text", "The lock is taken twice"], None),
        ([*_ADD_FINDING, "nit", "--text", "A comment is stale"], None),
        (["review", "done"], None),
        (["validate", "execute"], None),
        (["status"], None),
        ([*_ADD_FIX, "--text", "Take the lock once", "--fixes", "F-05"], None),
        (["task", "start", "T-04"], None),
        (["task", "done", "T-04"], None),
        (_fix_commit(20), None),
        (["review", "start", "--harden"], None),
        (["review", "prompt"], None),
        (["review", "finding", "check", "F-05", "closed"], None),
        ([*_ADD_FINDING, "nit", "--text", "A test name is long"], None),
        (["review", "done"], None),
        (["validate", "execute"], None),
        (["status"], None),
        (["checkpoint"], None),
        (["doc", "show", "checkpoint"], None),
    ]


def test_every_new_verb_and_option_reads_alike_on_a_local_and_a_hosted_project(
    tmp_path, monkeypatch, live_daemon
):
    from test_plan_fixes import _entry
    from test_review_harden import _LEFT_OPEN
    from test_review_prompt import (
        _FIX_RULES, _HARDEN_RULES, _item_entry, _line_with, _settled_part,
    )

    steps = _feature_steps()
    local, local_dir = _local_drive(tmp_path, monkeypatch, steps)
    git = _recording_git(monkeypatch)
    hosted, hosted_dir = _hosted_drive(tmp_path, monkeypatch, live_daemon, steps)

    assert len(local) == len(hosted) == len(steps) + 1
    _assert_driven_alike(local, hosted)
    local_docs, hosted_docs = _documents(local_dir), _documents(hosted_dir)
    # Follow-ups work only in a checkout: the local gate round filed its nit
    # in one, and the daemon's round keeps it in the round file alone.
    assert "### FU-01 - Nits from review round 1" in local_docs.pop(followup.FOLLOWUP_FILENAME)
    _assert_documents_alike(local_docs, hosted_docs)

    # What each step did, read off the hosted run; the local run is the same.
    def outcome(*args):
        (driven,) = _driven(hosted, *args)
        return driven

    def round_file(number):
        return hosted_dir / f"review-{number}.md"

    # task add --fixes: a task for each item, refused for the nit.
    code, out, err = outcome(*_ADD_FIX, "--text", "Tidy the name", "--fixes", "F-03")
    assert (code, out) == (1, "") and "F-03 is a nit" in err
    assert [out for args, code, out, _ in hosted if "--fixes" in args and code == 0] == [
        "Recorded T-02 (fixes F-01).\n", "Recorded T-03 (fixes F-02).\n",
        "Recorded T-04 (fixes F-05).\n",
    ]
    plan_md = hosted_docs["plan.md"]
    for task_id, finding in (("T-02", "F-01"), ("T-03", "F-02"), ("T-04", "F-05")):
        assert f"- Fixes: {finding}" in _entry(plan_md, task_id)

    # Each round opened at the checkout's HEAD: the first commit, then each fix's.
    first_sha = review.frontmatter(round_file(1))["sha"]
    fix_shas = [out.strip() for code, out, _ in _driven(hosted, "commit")]
    assert [review.frontmatter(round_file(n))["sha"] for n in (2, 3, 4)] == [
        fix_shas[0], fix_shas[0], fix_shas[1]
    ]

    # review start: refused while a fix task is not done, open once each is.
    starts = _driven(hosted, "review", "start")
    assert starts[1][:2] == (1, "")
    assert "F-01 (T-02 is not done), F-02 (T-03 is not done)" in starts[1][2]
    assert starts[2] == (
        0, f"{SLUG}/review-2\nScope: {first_sha}..HEAD\nItems to check: F-01, F-02\n", ""
    )

    # finding add --at, and the regression mark on the line the fix changed.
    assert _findings(round_file(1)) == [
        "- F-01 (blocker) [src/app.py:3] The close drops the sha",
        "- F-02 (should-fix) [src/app.py:9-11] A message names the wrong command",
        "- F-03 (nit) A name reads oddly",
    ]
    marked = "- F-04 (blocker, regression) [src/app.py:10] The fix drops the lock"
    # The add marks it, before the close would mark a line that carries no mark.
    assert marked in outcome("doc", "show", "review-2")[1].splitlines()
    assert _findings(round_file(2)) == [marked]
    assert [out for _, out, _ in _driven(hosted, "review", "done")] == [
        f"{SLUG}/review-1 closed changes-requested (1 blocker, 1 should-fix, 1 nit)\n",
        f"{SLUG}/review-2 closed changes-requested (1 blocker, 0 should-fix, 0 nit;"
        " 1 regression)\n",
        f"{SLUG}/review-3 closed hardened (0 blocker, 1 should-fix, 1 nit; 1 new find)\n",
        f"{SLUG}/review-4 closed hardened (0 blocker, 0 should-fix, 1 nit; 0 new finds)\n",
    ]

    # review prompt: each item's fix tasks and the rules for checking it closed.
    delta, harden, harden_items = [out for _, out, _ in _driven(hosted, "review", "prompt")]
    assert "T-02 Keep the sha. Verify: `uv run pytest`" in _item_entry(delta, "F-01")
    assert "T-03 Name the right command." in _item_entry(delta, "F-02")
    assert "T-04 Take the lock once." in _item_entry(harden_items, "F-05")
    for rule in _FIX_RULES:
        assert rule in delta and rule in harden_items and rule not in harden, rule
    assert f"`{first_sha}`" in _line_with(delta, "fails on the source at")
    assert f"`{fix_shas[0]}`" in _line_with(harden_items, "fails on the source at")

    # review finding reject, and the settled list the next brief shows.
    assert outcome("review", "finding", "reject", "F-04", "--reason", _REJECTED_WHY) == (
        0, f"Rejected F-04 in {SLUG}/review-2.\n", ""
    )
    assert _settled(round_file(2)) == [f"- F-04 rejected: {_REJECTED_WHY}"]
    assert [line for line in _settled_part(harden).splitlines() if line.startswith("- ")] == [
        "- F-03 (nit) A name reads oddly",
        "- F-04 (blocker, regression) [src/app.py:10] The fix drops the lock - rejected:"
        f" {_REJECTED_WHY}",
    ]

    # review start --harden: two harden rounds past the spent budget.
    assert starts[3][:2] == (1, "")
    assert "has used its review budget (2 of 2 rounds)" in starts[3][2]
    assert _driven(hosted, "review", "start", "--harden") == [
        (0, f"{SLUG}/review-3\nKind: harden\nScope: whole branch\n", ""),
        (0, f"{SLUG}/review-4\nKind: harden\nScope: whole branch\nItems to check: F-05\n", ""),
    ]
    assert [review.round_kind(review.frontmatter(round_file(n))) for n in (1, 2, 3, 4)] == [
        review.GATE, review.GATE, review.HARDEN, review.HARDEN
    ]
    for rule in _HARDEN_RULES:
        assert rule in harden and rule in harden_items and rule not in delta, rule

    # The completion gate: the latest gate round's verdict, passing once its
    # blocking item is settled, then the item a later harden round raised.
    assert _driven(hosted, "validate", "execute") == [
        (1, "", "execute has issues:\n  - the latest review round (review-2.md) is"
                " changes-requested: address the findings, then run another round with"
                " `specflo review start`.\n"),
        (0, "ok - execute is ready.\n", ""),
        (1, "", f"execute has issues:\n  - {_LEFT_OPEN.format('review-3.md', 'F-05')}\n"),
        (0, "ok - execute is ready.\n", ""),
    ]
    raised, passed = [out for _, out, _ in _driven(hosted, "status")]
    assert _next_hint(raised).startswith("All tasks done - review-3.md leaves F-05 open:")
    (reviews,) = [line for line in passed.splitlines() if line.startswith("Reviews:")]
    assert reviews.startswith("Reviews: 4 rounds (2 gate, 2 harden); latest round 4 hardened")
    assert reviews.endswith("; passes")
    assert _next_hint(passed) == (
        "All tasks done and review-4.md is hardened after a round that asked for changes"
        " - run the whole test suite once more, then `specflo advance` to complete the"
        " project."
    )
    assert _next_hint(passed) in outcome("doc", "show", "checkpoint")[1]
    # The client blamed the lines for the mark; the daemon ran no git at all.
    _assert_only_the_client_ran_git(git, live_daemon["root"])


def _harden_level_steps():
    """A harden-level project from its brief to completion: the brief filled
    in, a harden round over the brief's Scope that raises blocker F-01, a
    fix task and a fix commit, and a harden round that checks it closed.
    Status is read at each state the next-step hint turns on."""
    return [
        (["status"], None),
        (["validate", "brief"], None),
        (["section", "set", "brief", "Scope", "--stdin"], "- src/app.py\n"),
        (["section", "set", "brief", "Focus", "--stdin"], "Error paths and refusals.\n"),
        (["section", "set", "brief", "Stop when", "--stdin"],
         "Two quiet harden rounds in a row.\n"),
        (["validate", "brief"], None),
        (["validate", "execute"], None),
        (["review", "start", "--harden"], None),
        (["review", "prompt"], None),
        (["status"], None),
        ([*_ADD_FINDING, "blocker", "--at", "src/app.py:5",
          "--text", "The loader drops the last line"], None),
        ([*_ADD_FINDING, "nit", "--text", "A name is vague"], None),
        (["review", "done"], None),
        (["status"], None),
        (["validate", "execute"], None),
        ([*_ADD_FIX, "--text", "Keep the last line", "--fixes", "F-01"], None),
        (["status"], None),
        (["task", "start", "T-01"], None),
        (["task", "done", "T-01"], None),
        (_fix_commit(5), None),
        (["status"], None),
        (["review", "start", "--harden"], None),
        (["review", "prompt"], None),
        (["review", "finding", "check", "F-01", "closed"], None),
        ([*_ADD_FINDING, "nit", "--text", "A comment is stale"], None),
        (["review", "done"], None),
        (["status"], None),
        (["validate", "execute"], None),
        (["checkpoint"], None),
        (["doc", "show", "checkpoint"], None),
        (["advance"], None),
        (["status"], None),
    ]


def test_a_harden_level_project_runs_from_brief_to_completion_alike_local_and_hosted(
    tmp_path, monkeypatch, live_daemon
):
    from test_level_harden import NO_HARDEN_ROUND, SECTIONS, START_HARDEN_ROUND, _open_round_hint
    from test_review_prompt import _FIX_RULES, _item_entry, _line_with, _part

    steps = _harden_level_steps()
    level = ["--level", "harden"]
    local, local_dir = _local_drive(tmp_path, monkeypatch, steps, level)
    git = _recording_git(monkeypatch)
    hosted, hosted_dir = _hosted_drive(tmp_path, monkeypatch, live_daemon, steps, level)

    assert len(local) == len(hosted) == len(steps) + 1
    _assert_driven_alike(local, hosted)
    hosted_docs = _documents(hosted_dir)
    # A harden round files no nits follow-up, in a checkout or on a daemon.
    assert sorted(hosted_docs) == [
        "brief.md", "checkpoint.md", "plan.md", "project.md", "review-1.md", "review-2.md"
    ]
    _assert_documents_alike(_documents(local_dir), hosted_docs)

    def round_file(number):
        return hosted_dir / f"review-{number}.md"

    # new --level harden starts at execute with a brief and an empty plan.
    assert hosted[0] == (["new"], 0, (
        f"Created project '{SLUG}' (now active). Phase: execute.\n"
        f"Scaffolded {SLUG}/brief and {SLUG}/plan (ready to work).\n"
    ), "")
    project_md = hosted_docs["project.md"]
    assert "\nlevel: harden\n" in project_md and "\nstatus: complete\n" in project_md

    # The brief validates once each section is filled in.
    (empty, filled) = _driven(hosted, "validate", "brief")
    assert empty[:2] == (1, "")
    for title in SECTIONS:
        assert f"  - {title} is empty" in empty[2], title
    assert filled == (0, "ok - brief is ready.\n", "")

    # Each harden round reviews the brief's Scope; the second checks the fix closed.
    assert _driven(hosted, "review", "start", "--harden") == [
        (0, f"{SLUG}/review-1\nKind: harden\nScope: the brief's Scope\n", ""),
        (0, f"{SLUG}/review-2\nKind: harden\nScope: the brief's Scope\nItems to check: F-01\n",
         ""),
    ]
    first, second = [out for _, out, _ in _driven(hosted, "review", "prompt")]
    for brief in (first, second):
        scope = _part(brief, "Scope")
        assert "> - src/app.py" in scope.splitlines(), scope
        assert "> Error paths and refusals." in scope.splitlines(), scope
        assert "already present in that scope is a finding" in scope
        assert "branch" not in brief
    assert "T-01 Keep the last line. Verify: `uv run pytest`" in _item_entry(second, "F-01")
    for rule in _FIX_RULES:
        assert rule in second and rule not in first, rule
    first_sha = review.frontmatter(round_file(1))["sha"]
    assert f"`{first_sha}`" in _line_with(second, "fails on the source at")
    assert _findings(round_file(1)) == [
        "- F-01 (blocker) [src/app.py:5] The loader drops the last line",
        "- F-02 (nit) A name is vague",
    ]
    assert _findings(round_file(2)) == ["- F-03 (nit) A comment is stale"]
    assert [out for _, out, _ in _driven(hosted, "review", "done")] == [
        f"{SLUG}/review-1 closed hardened (1 blocker, 0 should-fix, 1 nit; 1 new find)\n",
        f"{SLUG}/review-2 closed hardened (0 blocker, 0 should-fix, 1 nit; 0 new finds)\n",
    ]
    assert _driven(hosted, *_ADD_FIX, "--text", "Keep the last line", "--fixes", "F-01") == [
        (0, "Recorded T-01 (fixes F-01).\n", "")
    ]
    assert "- Fixes: F-01" in hosted_docs["plan.md"]

    # The gate needs a harden round closed hardened and an empty ledger.
    checks = _driven(hosted, "validate", "execute")
    assert [code for code, _, _ in checks] == [1, 1, 0]
    assert f"  - {NO_HARDEN_ROUND}" in checks[0][2]
    assert "  - F-01 is still open after the latest review round (review-1.md)" in checks[1][2]
    assert checks[2][1] == "ok - execute is ready.\n"
    (advanced,) = _driven(hosted, "advance")
    assert advanced[1].startswith(f"Completed project '{SLUG}'.\n")

    # The next-step hint names the next move in each state.
    hints = [_next_hint(out) for _, out, _ in _driven(hosted, "status")]
    assert hints[:2] == [START_HARDEN_ROUND, _open_round_hint("review-1.md")]
    assert hints[2].startswith(
        "F-01 is open with no fix task: add one with `specflo task add --fixes F-01`"
    )
    assert hints[3].startswith("Work the next fix task: T-01 (`specflo task show`)")
    assert hints[4].startswith("Every open item has a done fix task")
    assert "to check F-01" in hints[4]
    assert hints[5:] == [
        "No open item is left and a harden round closed hardened - run the whole test suite"
        " once, then run `specflo advance` to complete the project.",
        "Project complete. Start the next piece of work with `specflo new`.",
    ]
    assert hints[5] in _driven(hosted, "doc", "show", "checkpoint")[0][1]
    _assert_the_daemon_ran_no_git(git, live_daemon["root"])

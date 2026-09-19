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

import re
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from specflo import config, daemon
from specflo.cli import app

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
    text = re.sub(r"^(Dir:     .*|Remote:  .*)$", "<where>", text, flags=re.MULTILINE)
    return re.sub(r"^- Actor: .*\n", "", text, flags=re.MULTILINE)


def _run(checkout: Path, project_dirs: list[str], new_args: list[str], raw: list | None = None):
    """Create the project with ``new_args`` and drive the pipeline; returns the
    exit code and normalized stdout of every step, the creation first.
    ``raw`` collects each step's stdout before normalization when given."""
    ids = {}
    results = []
    # The review report a step ingests with --file: a file of the checkout,
    # read by the client wherever the round itself lives.
    (checkout / "report.md").write_text("# Round 1\n\n## Findings\n\n- one nit.\n")
    result = runner.invoke(app, ["new", "Parity Thing", "--summary", "One line", *new_args])
    results.append((result.exit_code, _normalize(result.stdout, project_dirs)))
    if raw is not None:
        raw.append((["new"], result.stdout))
    for args, stdin in _pipeline():
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

"""The hosted pipeline parity scenario.

One scenario drives the whole pipeline twice: once against a project in the
checkout, once against a project on a daemon. Every command must exit the
same way and print the same stdout, once the two things that legitimately
differ are normalized away: where the project's files live (a command that
prints a project directory prints the checkout's for a local project and the
daemon's for a hosted one) and the Actor line the daemon records on every
entry it adds, which a local add does not carry. Everything else is
identical.
"""

import re
from pathlib import Path

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
        (["review", "done", "--verdict", "ready-to-merge"], None),
        (["validate", "execute"], None),
        (["checkpoint"], None),
        (["status"], None),
        (["hook", "reseed"], None),
        (["hook", "reseed", "--continue"], None),
        (["doc", "show", "project"], None),
        (["advance"], None),
        (["status"], None),
        (["hook", "reseed"], None),
    ]


def _recorded_id(output: str) -> str:
    """The id a `<verb> add` command minted: ``Recorded X-NN...`` -> ``X-NN``."""
    assert output.startswith("Recorded "), output
    return output.split()[1].rstrip(".")


def _normalize(text: str, project_dirs: list[str]) -> str:
    """Replace every spelling of the project's directory with one placeholder
    and drop the Actor lines only a daemon-held entry carries."""
    for spelling in sorted(project_dirs, key=len, reverse=True):
        text = text.replace(spelling, "<project>")
    return re.sub(r"^- Actor: .*\n", "", text, flags=re.MULTILINE)


def _run(checkout: Path, project_dirs: list[str], new_args: list[str], raw: list | None = None):
    """Create the project with ``new_args`` and drive the pipeline; returns the
    exit code and normalized stdout of every step, the creation first.
    ``raw`` collects each step's stdout before normalization when given."""
    ids = {}
    results = []
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
    raw = []
    results = _run(checkout, [str(project_dir), f"projects/{SLUG}"], ["--remote", "home"], raw)
    assert not list((checkout / "docs" / "projects").glob(f"{SLUG}*"))
    assert (project_dir / "checkpoint.md").is_file()
    # The daemon's directory layout is the daemon's business: no command names
    # it on the client, the one exception being the Dir line of status, which
    # says where the project lives on purpose. Everything else the normalizer
    # above is allowed to fold is a locator already.
    for args, stdout in raw:
        if args[:1] != ["status"]:
            assert str(project_dir) not in stdout, f"{' '.join(args)} names the daemon dir:\n{stdout}"
    return results


def test_the_pipeline_is_identical_for_a_local_and_a_hosted_project(local_run, hosted_run):
    assert len(local_run) == len(hosted_run) == len(_pipeline()) + 1
    for local, hosted in zip(local_run[1:], hosted_run[1:]):
        args = local[0]
        assert local[1:] == hosted[1:], f"{' '.join(args)}:\nlocal:\n{local[2]}\nhosted:\n{hosted[2]}"
    assert local_run[0] == hosted_run[0]


def test_the_scenario_reaches_completion_and_prints_every_seam(local_run):
    codes = [code for _, code, _ in local_run[1:]]
    # The first validate fails on purpose (an empty Out of scope section), every
    # other step passes: a scenario that never exercised a refusal would prove
    # less than one that did.
    assert codes.count(1) == 1 and set(codes) <= {0, 1}
    outputs = "\n".join(text for _, _, text in local_run[1:])
    for expected in (
        "Recorded ",
        "Advanced 'parity-thing' from brainstorm to spec.",
        "Advanced 'parity-thing' from spec to plan.",
        "Advanced 'parity-thing' from plan to execute.",
        "parity-thing/review-1 closed ready-to-merge",
        "Completed project 'parity-thing'.",
        "Checkpoint saved: parity-thing/checkpoint",
    ):
        assert expected in outputs, expected

"""Finding locations: ``review finding add --at <file>:<line>[-<line>]``.

A blocker or should-fix finding names where its defect is; a nit may. The
CLI checks the location in the caller's checkout, where the code lives: the
file must be a file at the round's sha and the lines must lie inside it. The
service, which may be a daemon holding only the documents, takes the
location as data and writes it into the finding line. A refusal changes no
file. A location the checkout cannot check (no sha on the round, or a commit
this checkout does not have) is written with a note, and its form is still
checked.
"""

import json
import os
import subprocess

import httpx
import pytest
from typer.testing import CliRunner

from specflo import config, daemon, markdown, projects, review
from specflo.cli import app
from specflo.errors import SpecfloError
from specflo.service import LocalProjectService

runner = CliRunner()

SLUG = "thing"
# Ten lines, committed as src/x.py.
X_PY = "".join(f"line {n}\n" for n in range(1, 11))
# The same author, committer and dates in every checkout, so two checkouts
# holding the same files share a sha.
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_AUTHOR_DATE": "2026-09-01T00:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-09-01T00:00:00+00:00",
}


def _git(path, *args):
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args], cwd=path, capture_output=True,
        text=True, check=True, env={**os.environ, **_GIT_ENV},
    ).stdout.strip()


def _commit(path, name, text):
    (path / name).parent.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(text)
    _git(path, "add", name)
    _git(path, "commit", "-qm", name)
    return _git(path, "rev-parse", "--short", "HEAD")


def _repo(path):
    """A git checkout at ``path`` whose one commit holds src/x.py; its short sha."""
    _git(path, "init", "-q")
    return _commit(path, "src/x.py", X_PY)


def _project(tmp_path, monkeypatch, repo=True):
    """An active 'Thing' at ``tmp_path`` with round 1 open; the round file.

    With ``repo``, ``tmp_path`` is a git checkout and the round opens at its
    HEAD; without, the round records no sha.
    """
    if repo:
        _repo(tmp_path)
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing", created="2026-09-01")
    projects.switch_project(tmp_path, cfg, "Thing")
    monkeypatch.chdir(tmp_path)
    started = runner.invoke(app, ["review", "start"])
    assert started.exit_code == 0, started.output
    return tmp_path / "docs" / "projects" / SLUG / "review-1.md"


def _add(severity, text, at=None, *extra):
    args = ["review", "finding", "add", "--severity", severity, "--text", text]
    if at is not None:
        args += ["--at", at]
    return runner.invoke(app, [*args, *extra])


def _findings(path):
    return markdown.section_body(path.read_text(), review.FINDINGS_HEADER).strip()


# --- required for blocker and should-fix, optional for a nit -----------------


@pytest.mark.parametrize("severity", ["blocker", "should-fix"])
def test_a_blocker_or_should_fix_add_without_at_is_refused(tmp_path, monkeypatch, severity):
    path = _project(tmp_path, monkeypatch)
    before = path.read_bytes()

    result = _add(severity, "The close drops the sha")

    assert result.exit_code == 1, result.output
    assert f"A {severity} finding needs --at <file>:<line>" in result.output
    assert path.read_bytes() == before


def test_a_nit_needs_no_at(tmp_path, monkeypatch):
    path = _project(tmp_path, monkeypatch)

    result = _add("nit", "A typo")

    assert result.exit_code == 0, result.output
    assert _findings(path) == "- F-01 (nit) A typo"


# --- an accepted location is written in the finding line ---------------------


@pytest.mark.parametrize(
    "severity, at",
    [
        ("blocker", "src/x.py:3"),
        ("should-fix", "src/x.py:9-10"),
        ("blocker", "src/x.py:1-10"),
        ("nit", "src/x.py:10"),
        ("should-fix", "src/x.py:4-4"),
    ],
)
def test_an_accepted_at_is_written_in_the_finding_line(tmp_path, monkeypatch, severity, at):
    path = _project(tmp_path, monkeypatch)

    result = _add(severity, "The close drops the sha", at)

    assert result.exit_code == 0, result.output
    assert result.output == "Recorded F-01 in thing/review-1.\n"
    line = _findings(path)
    assert line == f"- F-01 ({severity}) [{at}] The close drops the sha"
    finding = review.parse_finding_line(line)
    assert (finding.severity, finding.location) == (severity, at)


def test_the_json_carries_the_location(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)

    located = json.loads(_add("blocker", "One", "src/x.py:2-3", "--json").stdout)
    plain = json.loads(_add("nit", "Two", None, "--json").stdout)

    assert (located["id"], located["location"]) == ("F-01", "src/x.py:2-3")
    assert (plain["id"], plain["location"]) == ("F-02", None)


# --- a bad location is refused and changes no file ----------------------------


@pytest.mark.parametrize(
    "at, says",
    [
        ("src/missing.py:1", "src/missing.py is not a file at the round's sha"),
        ("src:1", "src is not a file at the round's sha"),
        ("src/x.py:11", "src/x.py has 10 lines at the round's sha"),
        ("src/x.py:9-11", "src/x.py has 10 lines at the round's sha"),
        ("src/x.py:5-3", "the range ends before it starts"),
        ("src/x.py", "is not a location"),
        ("src/x.py:0", "is not a location"),
        ("src/x.py:3-", "is not a location"),
        ("src/x.py:a", "is not a location"),
        ("src/my file.py:3", "is not a location"),
        (":3", "is not a location"),
    ],
)
@pytest.mark.parametrize("severity", ["blocker", "nit"])
def test_a_bad_at_is_refused_and_changes_no_file(tmp_path, monkeypatch, severity, at, says):
    path = _project(tmp_path, monkeypatch)
    project_dir = path.parent
    before = {p.name: p.read_bytes() for p in project_dir.iterdir()}

    result = _add(severity, "The close drops the sha", at)

    assert result.exit_code == 1, result.output
    assert says in result.output
    assert {p.name: p.read_bytes() for p in project_dir.iterdir()} == before


def test_the_file_is_read_at_the_rounds_sha_not_at_head_or_on_disk(tmp_path, monkeypatch):
    path = _project(tmp_path, monkeypatch)
    sha = review.frontmatter(path)["sha"]
    # A file committed after the round opened, and src/x.py grown on disk.
    _commit(tmp_path, "src/later.py", X_PY)
    (tmp_path / "src" / "x.py").write_text(X_PY * 2)
    before = path.read_bytes()

    later = _add("blocker", "One", "src/later.py:1")
    grown = _add("blocker", "Two", "src/x.py:15")

    assert later.exit_code == 1, later.output
    assert f"src/later.py is not a file at the round's sha {sha}" in later.output
    assert grown.exit_code == 1, grown.output
    assert f"src/x.py has 10 lines at the round's sha {sha}" in grown.output
    assert path.read_bytes() == before


def test_an_add_with_no_open_round_is_refused_naming_review_start(tmp_path, monkeypatch):
    path = _project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["review", "waive", "--reason", "By hand"]).exit_code == 0
    before = path.read_bytes()

    result = _add("blocker", "One", "src/x.py:3")

    assert result.exit_code == 1, result.output
    assert "specflo review start" in result.output
    assert path.read_bytes() == before


# --- a location the checkout cannot check -----------------------------------


def test_a_round_with_no_sha_takes_a_well_formed_location_with_a_note(tmp_path, monkeypatch):
    path = _project(tmp_path, monkeypatch, repo=False)
    assert review.frontmatter(path)["sha"] == ""

    result = _add("blocker", "One", "src/nowhere.py:99")

    assert result.exit_code == 0, result.output
    assert result.stdout == "Recorded F-01 in thing/review-1.\n"
    assert "note: --at src/nowhere.py:99 was not checked: the round records no sha" in result.stderr
    assert _findings(path) == "- F-01 (blocker) [src/nowhere.py:99] One"

    before = path.read_bytes()
    reversed_range = _add("blocker", "Two", "src/nowhere.py:9-3")
    assert reversed_range.exit_code == 1, reversed_range.output
    assert "the range ends before it starts" in reversed_range.output
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "sha, why",
    [
        # A hosted round opened by someone else, at a commit not fetched here.
        ("deadbee", "this checkout has no commit deadbee"),
        # A hand-edited sha never reaches git's argv.
        ("--batch", "the round's sha '--batch' is not a commit id"),
    ],
)
def test_a_round_sha_this_checkout_cannot_read_takes_the_location_with_a_note(
    tmp_path, monkeypatch, sha, why
):
    path = _project(tmp_path, monkeypatch)
    fields = review.frontmatter(path)
    path.write_text(path.read_text().replace(f"sha: '{fields['sha']}'", f"sha: '{sha}'"))
    assert review.frontmatter(path)["sha"] == sha

    result = _add("should-fix", "One", "src/x.py:40")

    assert result.exit_code == 0, result.output
    assert f"was not checked: {why}" in result.stderr
    assert _findings(path) == "- F-01 (should-fix) [src/x.py:40] One"


# --- the service takes the location as data ----------------------------------


def test_the_service_writes_the_location_it_is_sent_and_runs_no_git(tmp_path, monkeypatch):
    path = _project(tmp_path, monkeypatch)
    svc = LocalProjectService(tmp_path, config.load_config(tmp_path))

    def no_git(*args, **kwargs):
        raise AssertionError(f"the service ran {args[0] if args else kwargs}")

    monkeypatch.setattr(subprocess, "run", no_git)
    # No such file at the round's sha: the service does not look.
    finding_id, _ = svc.add_finding(SLUG, "blocker", "One", location="src/nowhere.py:99")

    assert finding_id == "F-01"
    assert _findings(path) == "- F-01 (blocker) [src/nowhere.py:99] One"


@pytest.mark.parametrize(
    "severity, location, says",
    [
        ("blocker", None, "A blocker finding needs --at"),
        ("should-fix", None, "A should-fix finding needs --at"),
        ("blocker", "src/x.py:5-3", "the range ends before it starts"),
        ("nit", "src/x.py", "is not a location"),
    ],
)
def test_the_service_refuses_a_missing_or_malformed_location(
    tmp_path, monkeypatch, severity, location, says
):
    path = _project(tmp_path, monkeypatch)
    svc = LocalProjectService(tmp_path, config.load_config(tmp_path))
    before = path.read_bytes()

    with pytest.raises(SpecfloError, match=says):
        svc.add_finding(SLUG, severity, "One", location=location)

    assert path.read_bytes() == before


def test_the_scope_carries_the_rounds_sha(tmp_path, monkeypatch):
    path = _project(tmp_path, monkeypatch)
    svc = LocalProjectService(tmp_path, config.load_config(tmp_path))

    assert svc.review_scope(SLUG)["sha"] == review.frontmatter(path)["sha"] != ""


# --- a hosted project, as a local one ----------------------------------------


def _location_steps():
    """Each accepted and refused add, then the round as a document."""
    add = ["review", "finding", "add", "--severity"]
    return [
        ["review", "start"],
        [*add, "blocker", "--at", "src/x.py:3", "--text", "The close drops the sha"],
        [*add, "should-fix", "--at", "src/x.py:9-10", "--text", "A message names the wrong command"],
        [*add, "nit", "--text", "A typo"],
        [*add, "nit", "--at", "src/x.py:10", "--text", "A name reads oddly"],
        [*add, "blocker", "--text", "No location"],
        [*add, "should-fix", "--at", "src/missing.py:1", "--text", "No such file"],
        [*add, "blocker", "--at", "src/x.py:11", "--text", "Past the end"],
        [*add, "blocker", "--at", "src/x.py:5-3", "--text", "Reversed"],
        [*add, "blocker", "--at", "src/x.py", "--text", "No line"],
        ["doc", "show", "review-1"],
    ]


def _checkout_run(tmp_path, monkeypatch, name, new_args, register=None):
    """``_location_steps`` in a fresh git checkout ``name``; each step's
    ``(args, exit code, stdout, stderr)`` and the add_finding request bodies."""
    checkout = tmp_path / name
    checkout.mkdir()
    _repo(checkout)
    config.init_config(checkout)
    monkeypatch.chdir(checkout)
    if register is not None:
        registered = runner.invoke(app, ["remote", "add", "home", *register])
        assert registered.exit_code == 0, registered.output
    created = runner.invoke(app, ["new", "Thing", "--summary", "One line", *new_args])
    assert created.exit_code == 0, created.output
    sent = []
    real_send = httpx.Client.send

    def recording_send(self, request, **kwargs):
        if request.url.path.endswith("/add_finding"):
            sent.append(json.loads(request.content))
        return real_send(self, request, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", recording_send)
    results = []
    for args in _location_steps():
        result = runner.invoke(app, args)
        results.append((args, result.exit_code, result.stdout, result.stderr))
    monkeypatch.setattr(httpx.Client, "send", real_send)
    return results, sent


def test_an_add_with_at_is_the_same_for_a_local_and_a_hosted_project(
    tmp_path, monkeypatch, live_daemon
):
    local, _ = _checkout_run(tmp_path, monkeypatch, "local", [])
    hosted, sent = _checkout_run(
        tmp_path, monkeypatch, "hosted", ["--remote", "home"],
        register=[live_daemon["url"], "--token", live_daemon["token"]],
    )

    for mine, theirs in zip(local, hosted, strict=True):
        assert mine == theirs, f"{' '.join(mine[0])}:\nlocal: {mine[1:]}\nhosted: {theirs[1:]}"
    codes = [code for _, code, _, _ in local]
    assert codes == [0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 0]
    local_round = tmp_path / "local" / "docs" / "projects" / SLUG / "review-1.md"
    hosted_round = live_daemon["root"] / daemon.PROJECTS_DIRNAME / SLUG / "review-1.md"
    assert local_round.read_text() == hosted_round.read_text()
    assert _findings(hosted_round).splitlines() == [
        "- F-01 (blocker) [src/x.py:3] The close drops the sha",
        "- F-02 (should-fix) [src/x.py:9-10] A message names the wrong command",
        "- F-03 (nit) A typo",
        "- F-04 (nit) [src/x.py:10] A name reads oddly",
    ]
    # The daemon holds no git repository; the location reached it as data.
    assert not (live_daemon["root"] / ".git").exists()
    assert sent[0] == {
        "slug": SLUG, "severity": "blocker", "text": "The close drops the sha",
        "location": "src/x.py:3", "regression": False,
    }
    assert sent[2]["location"] is None

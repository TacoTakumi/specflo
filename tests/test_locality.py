"""Per-project locality: a project lives in the checkout or on one daemon.

``specflo new --remote <name>`` creates a project on that daemon and records
which remote holds it in the checkout's hosted registry; nothing for it is
written under the checkout. ``list`` shows each project's locality, the
active pointer may name either kind, and every command routes by the active
project's locality through one resolver.
"""

import json

import pytest
import reviewhelp
from typer.testing import CliRunner

from specflo import config, daemon
from specflo.cli import app
from specflo.service.local import LocalProjectService
from specflo.service.remote import RemoteProjectService
from specflo.service.resolve import local_service, remote_service, resolve_service

runner = CliRunner()


@pytest.fixture
def checkout(tmp_path, monkeypatch, live_daemon):
    """A checkout with the live daemon registered as remote ``home``."""
    root = tmp_path / "checkout"
    root.mkdir()
    config.init_config(root)
    monkeypatch.chdir(root)
    done = runner.invoke(
        app, ["remote", "add", "home", live_daemon["url"], "--token", live_daemon["token"]]
    )
    assert done.exit_code == 0, done.output
    return root


def _files_naming(root, slug):
    """Every path under ``root`` with ``slug`` as a path component."""
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if slug in p.parts)


# --- the hosted registry ----------------------------------------------------


def test_hosted_registry_round_trips_slug_to_remote(tmp_path):
    config.init_config(tmp_path)
    assert config.hosted_projects(tmp_path) == {}

    config.record_hosted_project(tmp_path, "thing", "home")
    config.record_hosted_project(tmp_path, "other", "work")

    assert config.hosted_projects(tmp_path) == {"other": "work", "thing": "home"}
    assert config.hosting_remote(tmp_path, "thing") == "home"
    assert config.hosting_remote(tmp_path, "nope") is None
    assert config.forget_hosted_project(tmp_path, "thing") is True
    assert config.forget_hosted_project(tmp_path, "thing") is False
    assert config.hosted_projects(tmp_path) == {"other": "work"}


# --- the resolver -----------------------------------------------------------


def test_resolver_picks_the_service_by_the_projects_locality(tmp_path):
    cfg = config.init_config(tmp_path)
    config.add_remote(tmp_path, "home", "http://127.0.0.1:9", "token")
    config.record_hosted_project(tmp_path, "hosted-one", "home")

    assert isinstance(local_service(tmp_path, cfg), LocalProjectService)
    assert isinstance(remote_service(tmp_path, "home"), RemoteProjectService)
    assert isinstance(resolve_service(tmp_path, cfg, "local-one"), LocalProjectService)
    assert isinstance(resolve_service(tmp_path, cfg, "hosted-one"), RemoteProjectService)
    assert isinstance(resolve_service(tmp_path, cfg), LocalProjectService)
    cfg.active_project = "hosted-one"
    assert isinstance(resolve_service(tmp_path, cfg), RemoteProjectService)
    assert resolve_service(tmp_path, cfg).url == "http://127.0.0.1:9"


# --- new --remote -----------------------------------------------------------


def test_new_remote_creates_the_project_on_the_daemon_and_nothing_in_the_checkout(
    checkout, live_daemon
):
    result = runner.invoke(app, ["new", "Hosted Thing", "--remote", "home"])

    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[:2] == [
        "Created project 'hosted-thing' (now active). Phase: brainstorm.",
        "Scaffolded hosted-thing/brainstorm (ready to work).",
    ]
    hosted = live_daemon["root"] / daemon.PROJECTS_DIRNAME / "hosted-thing"
    assert (hosted / "project.md").is_file()
    assert (hosted / "brainstorm.md").is_file()
    assert (hosted / "checkpoint.md").is_file()
    assert _files_naming(checkout, "hosted-thing") == []
    assert config.hosted_projects(checkout) == {"hosted-thing": "home"}
    assert config.load_config(checkout).active_project == "hosted-thing"


def test_new_remote_refuses_an_unknown_remote(checkout):
    result = runner.invoke(app, ["new", "Thing", "--remote", "nowhere"])

    assert result.exit_code == 1
    assert "No remote 'nowhere'" in result.stderr
    assert _files_naming(checkout, "thing") == []


def test_one_slug_has_one_locality(checkout, live_daemon):
    assert runner.invoke(app, ["new", "Alpha"]).exit_code == 0
    assert runner.invoke(app, ["new", "Bravo", "--remote", "home"]).exit_code == 0

    local_again = runner.invoke(app, ["new", "Bravo"])
    assert local_again.exit_code == 1
    assert "hosted on remote 'home'" in local_again.stderr

    remote_again = runner.invoke(app, ["new", "Alpha", "--remote", "home"])
    assert remote_again.exit_code == 1
    assert "already exists" in remote_again.stderr

    # A slug the daemon already holds, that this checkout knows nothing of,
    # is refused by the daemon; the refusal names the project, never the
    # daemon's directory.
    held = live_daemon["root"] / daemon.PROJECTS_DIRNAME / "charlie"
    held.mkdir()
    (held / "project.md").write_text("x")
    taken = runner.invoke(app, ["new", "Charlie", "--remote", "home"])
    assert taken.exit_code == 1 and "Project 'charlie' already exists" in taken.stderr
    assert str(live_daemon["root"]) not in taken.stderr and "projects/charlie" not in taken.stderr


# --- list, switch, status across localities --------------------------------


def test_list_shows_the_locality_of_every_project(checkout):
    runner.invoke(app, ["new", "Alpha"])
    runner.invoke(app, ["new", "Bravo", "--remote", "home"])

    result = runner.invoke(app, ["list"])

    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[:2] == [
        "  alpha  (brainstorm)",
        "* bravo  (brainstorm)  [hosted: home]",
    ]

    data = json.loads(runner.invoke(app, ["list", "--json"]).output)
    by_slug = {p["slug"]: p for p in data["projects"]}
    assert by_slug["alpha"]["locality"] == "local" and by_slug["alpha"]["remote"] is None
    assert by_slug["bravo"]["locality"] == "hosted" and by_slug["bravo"]["remote"] == "home"
    assert by_slug["bravo"]["phase"] == "brainstorm" and by_slug["bravo"]["active"] is True


def test_switch_and_status_work_across_localities(checkout, live_daemon):
    runner.invoke(app, ["new", "Alpha"])
    runner.invoke(app, ["new", "Bravo", "--remote", "home"])

    switched = runner.invoke(app, ["switch", "Alpha"])
    assert switched.exit_code == 0
    assert "Switched to 'alpha' (phase: brainstorm)." in switched.output
    status = runner.invoke(app, ["status", "--json"])
    assert json.loads(status.output)["active_project"] == "alpha"

    switched = runner.invoke(app, ["switch", "Bravo"])
    assert switched.exit_code == 0
    assert "Switched to 'bravo' (phase: brainstorm)." in switched.output
    status = runner.invoke(app, ["status", "--json"])
    payload = json.loads(status.output)
    assert payload["active_project"] == "bravo" and payload["phase"] == "brainstorm"
    assert payload["dir"] is None and payload["remote"] == "home"


def test_commands_on_the_active_hosted_project_reach_the_daemon(checkout, live_daemon):
    runner.invoke(app, ["new", "Bravo", "--remote", "home"])

    added = runner.invoke(app, ["decision", "add", "--text", "Use one facade"])
    assert added.exit_code == 0, added.output
    assert added.output.startswith("Recorded ")

    shown = runner.invoke(app, ["doc", "show", "brainstorm"])
    assert shown.exit_code == 0
    assert "Use one facade" in shown.output
    brainstorm = live_daemon["root"] / daemon.PROJECTS_DIRNAME / "bravo" / "brainstorm.md"
    assert "Use one facade" in brainstorm.read_text()
    assert _files_naming(checkout, "bravo") == []


def test_summary_shelve_and_resume_route_by_the_named_projects_locality(checkout, live_daemon):
    runner.invoke(app, ["new", "Alpha"])
    runner.invoke(app, ["new", "Bravo", "--remote", "home"])
    runner.invoke(app, ["switch", "Alpha"])

    summary = runner.invoke(app, ["summary", "Bravo", "Hosted and summarized"])
    assert summary.exit_code == 0, summary.output
    project = live_daemon["root"] / daemon.PROJECTS_DIRNAME / "bravo" / "project.md"
    assert "Hosted and summarized" in project.read_text()

    shelved = runner.invoke(app, ["shelve", "Bravo", "--reason", "later"])
    assert shelved.exit_code == 0, shelved.output
    assert "status: shelved" in project.read_text()
    resumed = runner.invoke(app, ["resume", "Bravo"])
    assert resumed.exit_code == 0, resumed.output
    assert "status: active" in project.read_text()
    assert config.load_config(checkout).active_project == "bravo"


def test_list_reports_an_unreachable_remote_without_failing(checkout):
    runner.invoke(app, ["new", "Alpha"])
    runner.invoke(app, ["remote", "add", "dead", "http://127.0.0.1:9", "--token", "x"])
    config.record_hosted_project(checkout, "ghost", "dead")

    result = runner.invoke(app, ["list"])

    assert result.exit_code == 0
    assert "  ghost  (?)  [hosted: dead, unreachable]" in result.output.splitlines()
    assert "note:" in result.stderr and "127.0.0.1:9" in result.stderr
    data = json.loads(runner.invoke(app, ["list", "--json"]).output)
    ghost = next(p for p in data["projects"] if p["slug"] == "ghost")
    assert ghost["locality"] == "hosted" and ghost["remote"] == "dead"
    assert ghost["phase"] is None and "error" in ghost


def test_index_and_the_projects_dir_guard_read_the_checkout_only(checkout, live_daemon):
    runner.invoke(app, ["new", "Alpha"])
    runner.invoke(app, ["new", "Bravo", "--remote", "home"])

    result = runner.invoke(app, ["index"])

    assert result.exit_code == 0, result.output
    assert "(1 project)" in result.output
    assert not (live_daemon["root"] / daemon.PROJECTS_DIRNAME / "specflo-index.md").exists()


def test_a_hosted_advance_names_the_checkpoint_by_locator_and_carries_no_path(
    checkout, live_daemon
):
    runner.invoke(app, ["new", "Hosted Thing", "--remote", "home"])
    runner.invoke(app, ["brainstorm", "start"])
    runner.invoke(app, ["decision", "add", "--text", "one", "--rationale", "why"])
    runner.invoke(
        app, ["section", "set", "brainstorm", "Out of scope / Deferred", "--stdin"], input="No auth.\n"
    )

    advanced = runner.invoke(app, ["advance"])

    assert advanced.exit_code == 0, advanced.output
    assert "Checkpoint saved: hosted-thing/checkpoint" in advanced.output
    assert str(live_daemon["root"]) not in advanced.output
    reopened = json.loads(runner.invoke(app, ["reopen", "--json"]).output)
    assert reopened["checkpoint"] is None
    assert reopened["checkpoint_locator"] == "hosted-thing/checkpoint"
    assert str(live_daemon["root"]) not in json.dumps(reopened)


def test_list_opens_one_client_per_remote(checkout, live_daemon, monkeypatch):
    from specflo import cli as cli_module

    runner.invoke(app, ["new", "Alpha", "--remote", "home"])
    runner.invoke(app, ["new", "Bravo", "--remote", "home"])
    opened = []
    real = cli_module.remote_service

    def counting(root, name, **kwargs):
        opened.append((name, kwargs.get("timeout")))
        return real(root, name, **kwargs)

    monkeypatch.setattr(cli_module, "remote_service", counting)

    result = runner.invoke(app, ["list"])

    assert result.exit_code == 0, result.output
    assert opened == [("home", cli_module.REMOTE_LIST_TIMEOUT)]
    assert 0 < cli_module.REMOTE_LIST_TIMEOUT <= 10


def _hosted_in_spec(slug_name="Hosted Thing"):
    runner.invoke(app, ["new", slug_name, "--remote", "home"])
    runner.invoke(app, ["brainstorm", "start"])
    runner.invoke(app, ["decision", "add", "--text", "one", "--rationale", "why"])
    runner.invoke(
        app, ["section", "set", "brainstorm", "Out of scope / Deferred", "--stdin"], input="No auth.\n"
    )
    advanced = runner.invoke(app, ["advance"])
    assert advanced.exit_code == 0, advanced.output
    runner.invoke(app, ["spec", "start"])


def test_status_of_a_hosted_project_names_the_remote_and_no_daemon_path(checkout, live_daemon):
    _hosted_in_spec()
    daemon_dir = str(live_daemon["root"] / daemon.PROJECTS_DIRNAME / "hosted-thing")

    human = runner.invoke(app, ["status"]).output
    data = json.loads(runner.invoke(app, ["status", "--json"]).output)

    assert "Remote:  home" in human.splitlines()[1]
    assert "Dir:" not in human and daemon_dir not in human and "projects/hosted-thing" not in human
    assert data["dir"] is None and data["remote"] == "home"
    assert data["checkpoint"] is None and data["checkpoint_locator"] == "hosted-thing/checkpoint"
    assert daemon_dir not in json.dumps(data) and "projects/hosted-thing" not in json.dumps(data)


def test_status_of_a_local_project_keeps_its_directory_and_paths(checkout):
    runner.invoke(app, ["new", "Local Thing"])

    human = runner.invoke(app, ["status"]).output
    data = json.loads(runner.invoke(app, ["status", "--json"]).output)

    assert human.splitlines()[1] == "Dir:     docs/projects/local-thing"
    assert data["dir"].endswith("docs/projects/local-thing") and data["remote"] is None
    assert data["checkpoint"] == "docs/projects/local-thing/checkpoint.md"
    assert data["checkpoint_locator"] == "local-thing/checkpoint"


def test_checkpoint_and_reseed_of_a_hosted_project_read_first_by_locator(checkout, live_daemon):
    _hosted_in_spec()
    daemon_dir = str(live_daemon["root"] / daemon.PROJECTS_DIRNAME / "hosted-thing")

    human = runner.invoke(app, ["checkpoint"]).output
    data = json.loads(runner.invoke(app, ["checkpoint", "--json"]).output)
    reseed = runner.invoke(app, ["hook", "reseed"]).output

    read_first = human.split("## Read first")[1].split("## Do next")[0]
    assert "- hosted-thing/project" in read_first
    assert "- hosted-thing/brainstorm" in read_first
    assert "- hosted-thing/spec" in read_first
    for text in (human, reseed, json.dumps(data)):
        assert daemon_dir not in text and "projects/hosted-thing" not in text and ".md" not in text
    assert data["read_first"][:2] == ["hosted-thing/project", "hosted-thing/brainstorm"]
    assert data["path"] is None and data["locator"] == "hosted-thing/checkpoint"
    assert "- hosted-thing/brainstorm" in reseed


def test_checkpoint_of_a_local_project_keeps_its_paths_and_gains_the_locator(checkout):
    runner.invoke(app, ["new", "Local Thing"])

    human = runner.invoke(app, ["checkpoint"]).output
    data = json.loads(runner.invoke(app, ["checkpoint", "--json"]).output)

    assert "- docs/projects/local-thing/project.md" in human
    assert data["path"] == "docs/projects/local-thing/checkpoint.md"
    assert data["locator"] == "local-thing/checkpoint"


def test_remote_remove_is_refused_while_it_hosts_projects_unless_forced(checkout):
    runner.invoke(app, ["new", "Alpha", "--remote", "home"])
    runner.invoke(app, ["new", "Bravo", "--remote", "home"])

    refused = runner.invoke(app, ["remote", "remove", "home"])

    assert refused.exit_code == 1
    assert "still hosts alpha, bravo" in refused.stderr and "--force" in refused.stderr
    assert config.load_remote(checkout, "home")

    forced = runner.invoke(app, ["remote", "remove", "home", "--force"])

    assert forced.exit_code == 0, forced.output
    assert config.hosted_projects(checkout) == {"alpha": "home", "bravo": "home"}


def test_a_hosted_project_whose_remote_is_gone_is_refused_cleanly_by_every_command(checkout):
    runner.invoke(app, ["new", "Alpha", "--remote", "home"])
    runner.invoke(app, ["remote", "remove", "home", "--force"])

    for args in (["status"], ["doc", "show", "brainstorm"], ["switch", "alpha"], ["decision", "add", "--text", "x"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 1, args
        assert "No remote 'home'" in result.stderr, (args, result.stderr)
        assert "Traceback" not in result.stderr, args
        assert result.stdout == "", args


def test_switch_to_a_hosted_project_reports_the_remote_error_not_no_project(checkout):
    config.add_remote(checkout, "dead", "http://127.0.0.1:9", "token")
    config.record_hosted_project(checkout, "ghost", "dead")

    result = runner.invoke(app, ["switch", "ghost"])

    assert result.exit_code == 1
    assert "Cannot reach the remote at http://127.0.0.1:9" in result.stderr
    assert "No project" not in result.stderr

    missing = runner.invoke(app, ["switch", "nothing"])

    assert missing.exit_code == 1
    assert "No project 'nothing'. Run `specflo list`" in missing.stderr


def test_review_done_file_reads_the_report_in_the_checkout_not_on_the_daemon(checkout, live_daemon):
    runner.invoke(app, ["new", "Hosted Thing", "--remote", "home"])
    assert runner.invoke(app, ["review", "start"]).exit_code == 0
    (checkout / "report.md").write_text("# Round 1\n\n## Findings\n\n- none\n")
    # A file that exists only beside the daemon is not the client's to read.
    (live_daemon["root"] / "secret.txt").write_text("the daemon's own file\n")
    daemon_only = str(live_daemon["root"] / "secret.txt")
    (checkout / "not-here").mkdir()

    refused = runner.invoke(
        app, ["review", "done", "--verdict", "ready-to-merge", "--file", "not-here/secret.txt"]
    )
    assert refused.exit_code == 1, refused.output
    assert "No report file at not-here/secret.txt" in refused.stderr
    assert "Traceback" not in refused.stderr

    done = runner.invoke(app, ["review", "done", "--verdict", "ready-to-merge", "--file", "report.md"])

    assert done.exit_code == 0, done.output
    assert done.output.strip() == (
        "hosted-thing/review-1 closed ready-to-merge (0 blocker, 0 should-fix, 0 nit)"
    )
    round_file = live_daemon["root"] / daemon.PROJECTS_DIRNAME / "hosted-thing" / "review-1.md"
    assert round_file.read_text().endswith((checkout / "report.md").read_text())
    assert "the daemon's own file" not in round_file.read_text()
    assert daemon_only not in done.output


def test_hosted_start_and_review_json_carry_the_locator_and_no_path(checkout, live_daemon):
    runner.invoke(app, ["new", "Hosted Thing", "--remote", "home"])
    daemon_root = str(live_daemon["root"])

    for args, locator, extra in (
        # `new` scaffolds the brainstorm, so starting it again locates it.
        (["brainstorm", "start"], "hosted-thing/brainstorm", {"created": False}),
        (["spec", "start"], "hosted-thing/spec", {"created": True}),
        (["plan", "start"], "hosted-thing/plan", {"created": True}),
        (["review", "start"], "hosted-thing/review-1", {"created": True}),
        (["review", "done", "--verdict", "ready-to-merge"], "hosted-thing/review-1",
         {"verdict": "ready-to-merge", "findings": {"blocker": 0, "should-fix": 0, "nit": 0}}),
    ):
        if args[:2] == ["review", "done"]:
            reviewhelp.write_none(
                live_daemon["root"] / daemon.PROJECTS_DIRNAME / "hosted-thing" / "review-1.md"
            )
        result = runner.invoke(app, [*args, "--json"])
        assert result.exit_code == 0, (args, result.output)
        data = json.loads(result.output)
        assert data == {"locator": locator, "path": None, **extra}, args
        assert daemon_root not in result.output and "projects/hosted-thing" not in result.output


def test_doc_show_checkpoint_of_a_hosted_project_is_what_checkpoint_prints(checkout, live_daemon):
    _hosted_in_spec()
    daemon_dir = str(live_daemon["root"] / daemon.PROJECTS_DIRNAME / "hosted-thing")
    assert runner.invoke(app, ["review", "start"]).exit_code == 0
    assert reviewhelp.review_done(runner, app, "changes-requested").exit_code == 0

    stored = runner.invoke(app, ["doc", "show", "checkpoint"]).output
    printed = runner.invoke(app, ["checkpoint"]).output

    # The artifact the daemon holds and the payload the client renders are
    # one checkpoint: both name every file by locator. `doc show` prints the
    # file's own bytes; `checkpoint` ends its output with a newline.
    assert printed == stored + "\n"
    read_first = stored.split("## Read first")[1].split("## Do next")[0]
    for line in ("- hosted-thing/project", "- hosted-thing/brainstorm", "- hosted-thing/spec",
                 "- hosted-thing/review-1"):
        assert line in read_first, read_first
    assert daemon_dir not in stored and "projects/hosted-thing" not in stored and ".md" not in stored

    # The round the checkpoint points at can be read by that locator.
    shown = runner.invoke(app, ["doc", "show", "review-1"])
    assert shown.exit_code == 0, shown.output
    assert "verdict: changes-requested" in shown.output
    assert shown.output == (live_daemon["root"] / daemon.PROJECTS_DIRNAME / "hosted-thing" / "review-1.md").read_text()


def test_a_hosted_project_the_daemon_no_longer_holds_is_refused_without_its_path(
    checkout, live_daemon
):
    import shutil

    runner.invoke(app, ["new", "Ghost", "--remote", "home"])
    shutil.rmtree(live_daemon["root"] / daemon.PROJECTS_DIRNAME / "ghost")
    daemon_root = str(live_daemon["root"])

    for args in (
        ["switch", "ghost"],
        ["summary", "ghost", "x"],
        ["shelve", "ghost"],
        ["status"],
        ["status", "--json"],
        ["doc", "show", "brainstorm"],
        ["checkpoint"],
        ["list"],
        ["hook", "reseed"],
        ["hook", "reseed", "--format", "claude"],
    ):
        result = runner.invoke(app, args)
        text = result.stdout + result.stderr
        assert daemon_root not in text, (args, text)
        assert "projects/ghost" not in text, (args, text)
        assert "Traceback" not in text, (args, text)
        if args == ["status", "--json"]:
            assert json.loads(result.stdout)["error"] == "No project 'ghost'."
        elif args[0] not in ("list", "hook"):
            assert result.exit_code == 1, (args, text)
            assert "No project 'ghost'." in text, (args, text)

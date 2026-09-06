"""Remote registration: ``specflo remote add|list|remove`` and the store behind it.

A client registers a daemon additively. Each remote is one file under
``.specflo/remotes/``, holding its URL and the bearer token the CLI presents,
kept out of git by a self-ignoring directory and out of every listing by
construction: ``remote list`` prints names and URLs, never a secret.
Registering a remote changes nothing about the local projects.
"""

import json
import stat

import pytest
from typer.testing import CliRunner

from specflo import config
from specflo.cli import app
from specflo.errors import SpecfloError

runner = CliRunner()


@pytest.fixture
def root(tmp_path, monkeypatch):
    config.init_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    return tmp_path


# --- the store ------------------------------------------------------------


def test_add_remote_writes_one_protected_file_per_remote(root):
    created = config.add_remote(root, "home", "http://127.0.0.1:8741", "s3cret")

    assert created
    path = config.remotes_dir(root) / "home.json"
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert (config.remotes_dir(root) / ".gitignore").read_text() == "*\n"
    assert config.load_remote(root, "home") == config.Remote(
        "home", "http://127.0.0.1:8741", "s3cret"
    )
    assert "s3cret" not in config.config_path(root).read_text()


def test_add_remote_again_updates_the_url_and_token(root):
    config.add_remote(root, "home", "http://127.0.0.1:8741", "old")

    created = config.add_remote(root, "home", "http://10.0.0.5:8741", "new")

    assert not created
    assert config.load_remote(root, "home") == config.Remote("home", "http://10.0.0.5:8741", "new")
    assert config.list_remotes(root) == {"home": "http://10.0.0.5:8741"}


def test_list_remotes_maps_names_to_urls_in_name_order(root):
    assert config.list_remotes(root) == {}
    config.add_remote(root, "work", "https://factory.example/", "a")
    config.add_remote(root, "home", "http://127.0.0.1:8741", "b")

    assert list(config.list_remotes(root).items()) == [
        ("home", "http://127.0.0.1:8741"),
        ("work", "https://factory.example/"),
    ]


def test_remove_remote_deletes_its_file_and_refuses_an_unknown_name(root):
    config.add_remote(root, "home", "http://127.0.0.1:8741", "s3cret")

    config.remove_remote(root, "home")

    assert config.list_remotes(root) == {}
    with pytest.raises(SpecfloError, match="No remote 'home'"):
        config.remove_remote(root, "home")
    with pytest.raises(SpecfloError, match="No remote 'home'"):
        config.load_remote(root, "home")


@pytest.mark.parametrize("name", ["", "a/b", "../x", "has space", "UPPER"])
def test_add_remote_refuses_a_name_that_is_not_a_plain_slug(root, name):
    with pytest.raises(SpecfloError, match="remote name"):
        config.add_remote(root, name, "http://127.0.0.1:8741", "s3cret")


@pytest.mark.parametrize("url", ["", "127.0.0.1:8741", "ftp://x", "http://"])
def test_add_remote_refuses_a_url_without_an_http_scheme_and_host(root, url):
    with pytest.raises(SpecfloError, match="URL"):
        config.add_remote(root, "home", url, "s3cret")


def test_add_remote_refuses_an_empty_token(root):
    with pytest.raises(SpecfloError, match="token"):
        config.add_remote(root, "home", "http://127.0.0.1:8741", "  ")


# --- the commands -----------------------------------------------------------


def test_remote_add_registers_and_prints_name_and_url_only(root):
    result = runner.invoke(
        app, ["remote", "add", "home", "http://127.0.0.1:8741", "--token", "s3cret"]
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == "Registered remote 'home' -> http://127.0.0.1:8741\n"
    assert "s3cret" not in result.output
    assert config.load_remote(root, "home").token == "s3cret"


def test_remote_add_on_an_existing_name_reports_an_update(root):
    runner.invoke(app, ["remote", "add", "home", "http://127.0.0.1:8741", "--token", "old"])

    result = runner.invoke(app, ["remote", "add", "home", "http://10.0.0.5:8741", "--token", "new"])

    assert result.exit_code == 0, result.output
    assert result.stdout == "Updated remote 'home' -> http://10.0.0.5:8741\n"


def test_remote_add_refuses_a_bad_url_with_exit_1(root):
    result = runner.invoke(app, ["remote", "add", "home", "nope", "--token", "s3cret"])

    assert result.exit_code == 1
    assert "error:" in result.stderr and "URL" in result.stderr
    assert config.list_remotes(root) == {}


def test_remote_list_prints_names_and_urls_never_secrets(root):
    runner.invoke(app, ["remote", "add", "work", "https://factory.example/", "--token", "w0rk"])
    runner.invoke(app, ["remote", "add", "home", "http://127.0.0.1:8741", "--token", "h0me"])

    result = runner.invoke(app, ["remote", "list"])

    assert result.exit_code == 0
    assert result.stdout == "home  http://127.0.0.1:8741\nwork  https://factory.example/\n"
    assert "h0me" not in result.output and "w0rk" not in result.output


def test_remote_list_json_carries_names_and_urls_only(root):
    runner.invoke(app, ["remote", "add", "home", "http://127.0.0.1:8741", "--token", "h0me"])

    result = runner.invoke(app, ["remote", "list", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "remotes": [{"name": "home", "url": "http://127.0.0.1:8741"}]
    }
    assert "h0me" not in result.stdout


def test_remote_list_with_nothing_registered_says_how_to_add_one(root):
    result = runner.invoke(app, ["remote", "list"])

    assert result.exit_code == 0
    assert "specflo remote add <name> <url> --token" in result.stdout


def test_remote_remove_forgets_the_remote_and_refuses_an_unknown_one(root):
    runner.invoke(app, ["remote", "add", "home", "http://127.0.0.1:8741", "--token", "s3cret"])

    result = runner.invoke(app, ["remote", "remove", "home"])

    assert result.exit_code == 0
    assert result.stdout == "Removed remote 'home'.\n"
    assert config.list_remotes(root) == {}
    again = runner.invoke(app, ["remote", "remove", "home"])
    assert again.exit_code == 1
    assert "No remote 'home'" in again.stderr


def test_remote_commands_need_an_initialized_checkout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["remote", "list"])

    assert result.exit_code == 1
    assert "specflo init" in result.stderr


def test_config_list_and_the_config_file_never_carry_a_token(root):
    runner.invoke(app, ["remote", "add", "home", "http://127.0.0.1:8741", "--token", "s3cret"])

    listing = runner.invoke(app, ["config", "list"])

    assert listing.exit_code == 0
    assert "s3cret" not in listing.output
    assert "s3cret" not in config.config_path(root).read_text()


# --- registering a remote changes nothing about local projects -------------


def _local_outputs(tmp_path, monkeypatch, commands):
    """Run ``commands`` in two identical checkouts, one with a remote registered."""
    outputs = {}
    for label, with_remote in (("plain", False), ("with_remote", True)):
        directory = tmp_path / label
        directory.mkdir()
        monkeypatch.chdir(directory)
        runner.invoke(app, ["init"])
        runner.invoke(app, ["new", "My Thing", "--summary", "One line"])
        if with_remote:
            done = runner.invoke(
                app, ["remote", "add", "home", "http://127.0.0.1:8741", "--token", "s3cret"]
            )
            assert done.exit_code == 0, done.output
        outputs[label] = [
            runner.invoke(app, command).output.replace(str(directory), "<checkout>")
            for command in commands
        ]
    return outputs


def test_local_projects_behave_identically_after_a_remote_is_registered(tmp_path, monkeypatch):
    outputs = _local_outputs(
        tmp_path,
        monkeypatch,
        [
            ["list"],
            ["status"],
            ["decision", "add", "--text", "Use one facade", "--rationale", "one seam"],
            ["status", "--json"],
            ["doc", "show", "brainstorm"],
        ],
    )

    assert outputs["plain"] == outputs["with_remote"]
    assert "my-thing" in outputs["plain"][0]
    assert "Recorded" in outputs["plain"][2]

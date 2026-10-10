"""Named users added and listed on the serve side.

`specflo serve user add` creates a named user with a role. A user name is
short, lowercase and safe to show anywhere; 'agent' is kept for the built-in
principal of the working agents. A password is asked for twice at the
terminal and never taken as an option value, so it stays out of shell
history and the process list. A user added without one is proxy-only: they
can act only through a session opened for them. `specflo serve user list`
shows who exists and what they hold, never a hash or a secret.
"""

import hashlib

import click
import pytest
import typer.main
from typer.testing import CliRunner

from specflo import daemon
from specflo.cli import app
from specflo.daemon import auth
from specflo.daemon.cli import user_app
from specflo.daemon.store import Token, open_store
from specflo.errors import SpecfloError

runner = CliRunner()


@pytest.fixture
def root(tmp_path):
    return daemon.prepare_root(tmp_path / "daemon")


def _user(root, *args, input=None):
    return runner.invoke(app, ["serve", "--root", str(root), "user", *args], input=input)


def _users(root):
    with open_store(root) as store:
        return store.list_users()


# --- names ------------------------------------------------------------------


@pytest.mark.parametrize("name", ["rob", "r", "rob.shortt", "rob_s", "rob-s", "rob@home", "7of9", "a" * 64])
def test_a_name_within_the_rule_is_kept(name):
    assert auth.validate_user_name(name) == name


def test_a_name_is_lowercased_before_it_is_checked():
    assert auth.validate_user_name("Rob") == "rob"
    assert auth.validate_user_name("ROB@Home") == "rob@home"


@pytest.mark.parametrize("name", ["", "-rob", ".rob", "_rob", "@rob", "r b", "rob!", "rob/x", "a" * 65, "rob\n"])
def test_a_name_outside_the_rule_is_refused_naming_the_rule(name):
    with pytest.raises(SpecfloError, match="1 to 64 characters"):
        auth.validate_user_name(name)


@pytest.mark.parametrize("name", ["agent", "Agent"])
def test_the_agent_name_is_reserved(name):
    with pytest.raises(SpecfloError, match="reserved"):
        auth.validate_user_name(name)


# --- adding through the module ----------------------------------------------


def test_add_user_stores_the_lowercased_name_and_a_password_hash(root):
    user = auth.add_user(root, "Rob", "developer", "s3cret", today="2026-10-10")

    assert (user.name, user.role, user.created) == ("rob", "developer", "2026-10-10")
    assert _users(root) == [user]
    assert auth.verify_password("s3cret", user.password_hash)


def test_add_user_without_a_password_is_proxy_only(root):
    user = auth.add_user(root, "ada", "requester", None)

    assert user.password_hash is None
    assert _users(root)[0].password_hash is None


def test_add_user_refuses_an_empty_password(root):
    with pytest.raises(SpecfloError, match="empty"):
        auth.add_user(root, "ada", "requester", "")

    assert _users(root) == []


def test_add_user_refuses_an_unknown_role(root):
    with pytest.raises(SpecfloError, match="developer, requester"):
        auth.add_user(root, "ada", "admin", None)

    assert _users(root) == []


def test_add_user_refuses_a_name_taken_in_any_case(root):
    auth.add_user(root, "rob", "developer", None)

    with pytest.raises(SpecfloError, match="already exists"):
        auth.add_user(root, "ROB", "requester", None)

    assert [user.role for user in _users(root)] == ["developer"]


# --- the add command ----------------------------------------------------------


def test_user_add_stores_the_name_in_lowercase_with_no_password(root):
    result = _user(root, "add", "Rob", "--role", "developer")

    assert result.exit_code == 0, result.output
    [user] = _users(root)
    assert (user.name, user.role, user.password_hash) == ("rob", "developer", None)
    assert "rob" in result.output and "proxy" in result.output


def test_user_add_prepares_a_root_that_does_not_exist_yet(tmp_path):
    root = tmp_path / "fresh"

    result = _user(root, "add", "ada", "--role", "requester")

    assert result.exit_code == 0, result.output
    assert (root / daemon.PROJECTS_DIRNAME).is_dir()
    assert [user.name for user in _users(root)] == ["ada"]


@pytest.mark.parametrize(
    "name, rule",
    [
        ("agent", "reserved"),
        ("-rob", "1 to 64 characters"),
        ("a" * 65, "1 to 64 characters"),
        ("r b", "1 to 64 characters"),
    ],
)
def test_user_add_refuses_a_bad_name_and_creates_nothing(root, name, rule):
    # After --, so a name starting with a dash reaches the rule and is not
    # read as an option.
    result = _user(root, "add", "--role", "developer", "--", name)

    assert result.exit_code == 1
    assert rule in result.stderr
    assert _users(root) == []


def test_user_add_refuses_a_duplicate_and_leaves_the_first(root):
    assert _user(root, "add", "rob", "--role", "developer").exit_code == 0

    result = _user(root, "add", "Rob", "--role", "requester")

    assert result.exit_code == 1
    assert "already exists" in result.stderr
    assert [(user.name, user.role) for user in _users(root)] == [("rob", "developer")]


def test_user_add_refuses_an_unknown_role_and_creates_nothing(root):
    result = _user(root, "add", "rob", "--role", "admin")

    assert result.exit_code == 1
    assert "developer, requester" in result.stderr
    assert _users(root) == []


def test_user_add_needs_a_role(root):
    result = _user(root, "add", "rob")

    assert result.exit_code != 0
    assert _users(root) == []


def test_user_add_with_password_asks_twice_and_stores_only_the_hash(root):
    result = _user(root, "add", "rob", "--role", "developer", "--password", input="s3cret\ns3cret\n")

    assert result.exit_code == 0, result.output
    [user] = _users(root)
    assert user.password_hash and "s3cret" not in user.password_hash
    assert auth.verify_password("s3cret", user.password_hash)
    assert result.stderr.count("assword") >= 2
    assert "s3cret" not in result.output


def test_user_add_with_two_different_passwords_creates_nothing(root):
    result = _user(root, "add", "rob", "--role", "developer", "--password", input="s3cret\nother\n")

    assert result.exit_code == 1
    assert "do not match" in result.stderr
    assert _users(root) == []


def test_user_add_refuses_an_empty_password_at_the_prompt(root):
    result = _user(root, "add", "rob", "--role", "developer", "--password", input="\n\n")

    assert result.exit_code == 1
    assert "empty" in result.stderr
    assert _users(root) == []


def test_user_add_asks_for_no_password_when_the_name_is_refused(root):
    result = _user(root, "add", "agent", "--role", "developer", "--password", input="s3cret\ns3cret\n")

    assert result.exit_code == 1
    assert "assword" not in result.stderr
    assert _users(root) == []


def test_user_add_takes_no_password_as_an_option_value(root):
    result = _user(root, "add", "rob", "--role", "developer", "--password", "s3cret")

    assert result.exit_code != 0
    assert _users(root) == []
    command = typer.main.get_command(user_app).commands["add"]
    for param in command.params:
        if isinstance(param, click.Option):
            assert param.is_flag or "password" not in param.name, param.name


# --- the list command ---------------------------------------------------------


def test_user_list_with_no_users_says_so(root):
    result = _user(root, "list")

    assert result.exit_code == 0, result.output
    assert "No users" in result.output


def test_user_list_shows_name_role_password_and_token_count_and_no_secret(root):
    rob = auth.add_user(root, "rob", "developer", "s3cret")
    auth.add_user(root, "ada", "requester", None)
    with open_store(root) as store:
        for owner, name in (("rob", "laptop"), ("rob", "desk"), ("agent", "runner")):
            secret = f"secret-{owner}-{name}"
            store.add_token(
                Token(
                    owner=owner,
                    name=name,
                    hash=hashlib.sha256(secret.encode()).hexdigest(),
                    last4=secret[-4:],
                    created="2026-10-10",
                )
            )

    result = _user(root, "list")

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    ada = next(line for line in lines if line.split()[:1] == ["ada"])
    rob_line = next(line for line in lines if line.split()[:1] == ["rob"])
    assert ada.split()[1:] == ["requester", "no", "0"]
    assert rob_line.split()[1:] == ["developer", "yes", "2"]
    assert not any(line.split()[:1] == ["agent"] for line in lines)
    assert rob.password_hash not in result.output
    assert rob.password_hash.split("$")[4] not in result.output
    assert "scrypt" not in result.output
    assert "secret-" not in result.output


# --- help ---------------------------------------------------------------------


@pytest.mark.parametrize("command", [["add"], ["list"]])
def test_user_commands_have_help_and_an_example(root, command):
    result = _user(root, *command, "--help")

    assert result.exit_code == 0, result.output
    assert "Example: specflo serve" in result.stdout

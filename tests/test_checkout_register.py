"""The operator's register of checkouts that hold tokens.

A checkout keeps lease tokens in ``.specflo/leases`` and daemon tokens in
``.specflo/remotes``. The pool hides those directories from a member whose
definition lists a directory above the checkout, and it finds them through
this register instead of walking the listed directory at every start.
"""

import ast
from pathlib import Path

from typer.testing import CliRunner

from specflo import checkouts, config
from specflo.cli import app
from specflo.pool import cli_lease

REPO = Path(__file__).resolve().parents[1]
FIXTURE = "_no_operator_register"


def _checkout(path: Path) -> Path:
    """A checkout made by hand, so that nothing records it."""
    (path / ".specflo").mkdir(parents=True)
    (path / ".specflo" / "config.yaml").write_text("projects_dir: docs\n", encoding="utf-8")
    return path


def test_storing_a_lease_token_records_the_checkout_once(tmp_path):
    root = _checkout(tmp_path / "proj")

    cli_lease.store_token(root, "coder", "tok-1")
    cli_lease.store_token(root, "coder", "tok-2")

    assert checkouts.recorded() == (str(root.resolve()),)


def test_adding_a_remote_records_the_checkout(tmp_path):
    root = _checkout(tmp_path / "proj")

    config.add_remote(root, "rig", "http://127.0.0.1:9", "secret")

    assert checkouts.recorded() == (str(root.resolve()),)


def test_a_command_run_in_a_checkout_that_already_holds_tokens_records_it(
    tmp_path, monkeypatch
):
    root = _checkout(tmp_path / "proj")
    (root / ".specflo" / "remotes").mkdir()
    (root / "sub").mkdir()
    monkeypatch.chdir(root / "sub")

    CliRunner().invoke(app, ["status"])

    assert checkouts.recorded() == (str(root.resolve()),)


def test_a_command_run_with_the_directory_option_records_that_checkout(tmp_path):
    root = _checkout(tmp_path / "proj")
    (root / ".specflo" / "leases").mkdir()

    CliRunner().invoke(app, ["-C", str(root), "status"])

    assert checkouts.recorded() == (str(root.resolve()),)


def test_a_command_run_in_a_checkout_with_no_token_directory_records_it(
    tmp_path, monkeypatch
):
    root = _checkout(tmp_path / "proj")
    monkeypatch.chdir(root)

    CliRunner().invoke(app, ["status"])

    assert checkouts.recorded() == (str(root.resolve()),)


def test_init_records_the_checkout_it_makes(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.chdir(root)

    done = CliRunner().invoke(app, ["init"])

    assert done.exit_code == 0, done.output
    assert checkouts.recorded() == (str(root.resolve()),)


def test_a_command_run_outside_any_checkout_records_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    CliRunner().invoke(app, ["status"])

    assert checkouts.recorded() == ()


def test_the_register_is_the_users_alone(tmp_path):
    root = _checkout(tmp_path / "proj")

    checkouts.record(root)

    assert checkouts.register_file().stat().st_mode & 0o777 == 0o600


def test_the_register_is_read_from_the_home_it_is_given(tmp_path):
    home = tmp_path / "home"
    root = _checkout(tmp_path / "proj")

    checkouts.record(root, home=home)

    assert checkouts.register_file(home) == home / ".specflo" / "checkouts"
    assert checkouts.recorded(home=home) == (str(root.resolve()),)
    assert checkouts.recorded() == ()


def test_a_register_that_cannot_be_written_does_not_fail_the_token_store(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    # A file where the register's directory should be.
    (home / ".specflo").write_text("")
    root = _checkout(tmp_path / "proj")

    checkouts.record(root, home=home)

    assert checkouts.recorded(home=home) == ()


def test_the_real_register_is_not_the_one_the_suite_writes():
    assert checkouts.register_file() != Path.home() / ".specflo" / "checkouts"


def test_the_isolating_fixture_is_autouse_in_the_top_level_conftest():
    tree = ast.parse((REPO / "tests" / "conftest.py").read_text(encoding="utf-8"))
    (definition,) = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == FIXTURE
    ]
    autouse = [
        keyword.value.value
        for decorator in definition.decorator_list
        if isinstance(decorator, ast.Call)
        for keyword in decorator.keywords
        if keyword.arg == "autouse"
    ]
    assert autouse == [True]

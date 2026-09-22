"""The five shipped agent definitions, and ``specflo serve pool init`` that lays them down.

A new daemon root has no pool directory. ``init`` writes one an admin can
start from: a pool file that is all comments, so nothing is declared until the
admin declares it, and the definitions that ship inside the package. What an
admin has already edited is never written over.

The definitions carry their limits in what they list, not in their prompts, so
the checks here read the loaded front matter: the rebaser can write with git
but is given nothing to push with, the model-update checker has no tool that
changes anything, and the landscape scanner alone accepts a provider that may
train on what it is sent.
"""

import re
import shutil
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from specflo.cli import app
from specflo.pool import cli_admin, config, definitions

runner = CliRunner()

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "pool"

SHIPPED = ("worker", "critic", "hermes-rebaser", "model-update-checker", "landscape-scanner")

# pi's built-in tools that change something, and the ones that only read.
STATE_CHANGING_TOOLS = {"bash", "powershell", "edit", "write"}
PI_TOOLS = STATE_CHANGING_TOOLS | {"read", "grep", "find", "ls"}


def _init(root):
    return runner.invoke(app, ["serve", "--root", str(root), "pool", "init"])


def _validate(root):
    return runner.invoke(app, ["serve", "--root", str(root), "pool", "validate"])


def _shipped(name):
    return definitions.load_definition(cli_admin.SHIPPED_DIR / f"{name}.md")


def _example(directory):
    """The pool file's commented example as data. A prose line is '# ' and
    then a word; an example line is YAML with a bare '#' put before it."""
    lines = (directory / config.POOL_FILE).read_text(encoding="utf-8").splitlines()
    example = [line[1:] for line in lines if not re.match(r"#( \S|$)", line)]
    return yaml.safe_load("\n".join(example))


# --- init ----------------------------------------------------------------


def test_init_on_an_empty_root_writes_the_pool_file_and_the_five_definitions(tmp_path):
    root = tmp_path / "daemon"

    result = _init(root)

    assert result.exit_code == 0, result.output
    directory = cli_admin.pool_dir(root)
    assert (directory / config.POOL_FILE).is_file()
    written = sorted(p.stem for p in (directory / config.DEFINITIONS_DIR).glob("*.md"))
    assert written == sorted(SHIPPED)
    for name in SHIPPED:
        assert name in result.output


def test_the_written_pool_file_declares_nothing_until_an_admin_edits_it(tmp_path):
    root = tmp_path / "daemon"
    _init(root)

    text = (cli_admin.pool_dir(root) / config.POOL_FILE).read_text(encoding="utf-8")

    assert yaml.safe_load(text) is None
    assert all(line.startswith("#") or not line.strip() for line in text.splitlines())


def test_the_written_pool_directory_is_valid_as_it_stands(tmp_path):
    root = tmp_path / "daemon"
    _init(root)

    result = _validate(root)

    assert result.exit_code == 0, result.output
    assert "5 definitions" in result.output


def test_init_is_idempotent_and_keeps_what_an_admin_edited(tmp_path):
    root = tmp_path / "daemon"
    _init(root)
    directory = cli_admin.pool_dir(root)
    pool_file = directory / config.POOL_FILE
    worker = directory / config.DEFINITIONS_DIR / "worker.md"
    critic = directory / config.DEFINITIONS_DIR / "critic.md"
    pool_file.write_text("accounts: []\n", encoding="utf-8")
    edited = worker.read_text(encoding="utf-8") + "\nOne more rule.\n"
    worker.write_text(edited, encoding="utf-8")
    shipped_critic = critic.read_text(encoding="utf-8")
    critic.unlink()

    result = _init(root)

    assert result.exit_code == 0, result.output
    assert pool_file.read_text(encoding="utf-8") == "accounts: []\n"
    assert worker.read_text(encoding="utf-8") == edited
    assert critic.read_text(encoding="utf-8") == shipped_critic


def test_a_second_init_changes_no_file(tmp_path):
    root = tmp_path / "daemon"
    _init(root)
    files = [p for p in cli_admin.pool_dir(root).rglob("*") if p.is_file()]
    before = {p: p.read_bytes() for p in files}

    result = _init(root)

    assert result.exit_code == 0, result.output
    after = [p for p in cli_admin.pool_dir(root).rglob("*") if p.is_file()]
    assert {p: p.read_bytes() for p in after} == before


def test_the_example_filled_from_the_fixture_passes_validate(tmp_path):
    # The example names the rig's llama-swap file, one of its models and the
    # operator's own models file; the fixtures stand in for the rig, so the
    # check reads none of this machine's own configuration.
    root = tmp_path / "daemon"
    _init(root)
    directory = cli_admin.pool_dir(root)
    data = _example(directory)
    shutil.copy(FIXTURES / "llama-swap.yaml", directory / "llama-swap.yaml")
    shutil.copy(FIXTURES / "models.json", directory / "models.json")
    data["llama_swap"] = "llama-swap.yaml"
    data["models_file"] = "models.json"
    for member in data["members"]:
        if member["backing"] == config.LOCAL:
            member["model"] = "model-a"
    (directory / config.POOL_FILE).write_text(yaml.safe_dump(data), encoding="utf-8")

    result = _validate(root)

    assert result.exit_code == 0, result.output
    assert "5 definitions" in result.output
    assert "5 pools" in result.output


def test_the_example_binds_every_shipped_definition_to_a_pool(tmp_path):
    root = tmp_path / "daemon"
    _init(root)

    data = _example(cli_admin.pool_dir(root))

    assert set(data) <= set(config.SECTIONS)
    assert sorted(pool["definition"] for pool in data["pools"]) == sorted(SHIPPED)


def test_init_writes_to_the_names_the_configuration_reads():
    # The admin module repeats the two names so that loading it loads nothing.
    assert cli_admin._POOL_FILE == config.POOL_FILE
    assert cli_admin._DEFINITIONS_DIR == config.DEFINITIONS_DIR


def test_init_needs_a_root():
    result = runner.invoke(app, ["serve", "pool", "init"])

    assert result.exit_code != 0
    assert "--root" in result.output


def test_init_is_listed_under_the_pool_group():
    result = runner.invoke(app, ["serve", "--root", "unused", "pool", "--help"])

    assert result.exit_code == 0
    assert "init" in result.output


# --- the definitions themselves ------------------------------------------


def test_exactly_the_five_definitions_ship():
    assert sorted(p.stem for p in cli_admin.SHIPPED_DIR.glob("*.md")) == sorted(SHIPPED)


@pytest.mark.parametrize("name", SHIPPED)
def test_a_shipped_definition_loads_and_lists_only_real_pi_tools(name):
    definition = _shipped(name)

    assert definition.name == name
    assert definition.tools, "a definition with no tools starts a member that can do nothing"
    assert set(definition.tools) <= PI_TOOLS


@pytest.mark.parametrize("name", SHIPPED)
def test_a_shipped_definition_is_plain_ascii(name):
    assert (cli_admin.SHIPPED_DIR / f"{name}.md").read_text(encoding="utf-8").isascii()


def test_the_rebaser_can_write_with_git_but_is_given_nothing_to_push_with():
    rebaser = _shipped("hermes-rebaser")

    # git runs through bash, and a conflict is resolved by editing the file.
    assert {"bash", "edit"} <= set(rebaser.tools)
    assert "git push" in rebaser.deny
    # The deny list guards against a mistake. What holds is that the member
    # is started with no credential at all, so a push has nothing to sign in with.
    assert rebaser.credentials == ()
    assert not any("TOKEN" in name or "KEY" in name or "SSH" in name for name in rebaser.env)


def test_the_model_update_checker_only_proposes():
    checker = _shipped("model-update-checker")

    assert not set(checker.tools) & STATE_CHANGING_TOOLS
    assert "strong-model" in checker.needs
    assert "model-update-check" in checker.skills


def test_the_model_update_checker_points_at_the_skill_and_the_watch_list():
    checker = _shipped("model-update-checker")

    assert "model-update-check" in checker.prompt
    assert "watch list" in checker.prompt
    # The skill holds the workflow; a copy here would drift from it.
    assert len(checker.prompt.splitlines()) <= 15


def test_the_landscape_scanner_alone_accepts_class_open():
    assert _shipped("landscape-scanner").egress == "open"
    for name in set(SHIPPED) - {"landscape-scanner"}:
        assert _shipped(name).egress != "open", name


def test_the_definition_format_has_no_key_that_starts_an_agent_on_its_own():
    for key in ("schedule", "cron", "trigger", "triggers", "every", "on"):
        assert key not in definitions.FIELDS


@pytest.mark.parametrize("key", ["schedule", "cron", "trigger"])
def test_a_definition_that_carries_a_starting_key_is_refused(tmp_path, key):
    text = (cli_admin.SHIPPED_DIR / "worker.md").read_text(encoding="utf-8")
    path = tmp_path / "worker.md"
    path.write_text(text.replace("---\n", f"---\n{key}: daily\n", 1), encoding="utf-8")

    with pytest.raises(definitions.DefinitionError) as refused:
        definitions.load_definition(path)

    assert refused.value.field == key

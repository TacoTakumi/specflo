"""Team files, and the pool directory loaded as one configuration.

A team is a markdown file in ``teams/`` beside the pool file: a set of roles,
each a named pool and how many of its leases the role takes. A team is leased
all or nothing, so a role that could never be granted is refused when the
configuration is checked, with an error naming the team and the role.
"""

import copy
import shutil
from pathlib import Path

import pytest
import yaml

from specflo.pool import config, teams

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "pool"

WORKER = """\
---
role: Does one plan task and reports what changed
tools: [read, bash, edit]
needs: [code]
egress: no-train
---

You are the worker.
"""
CRITIC = WORKER.replace("Does one plan task and reports what changed", "Reviews a result").replace(
    "You are the worker.", "You are the critic."
)

ACCOUNT = {"name": "openrouter-main", "cap": 4, "key_env": "OPENROUTER_API_KEY"}
CODER_A = {
    "name": "coder-a",
    "command": "pi --mode rpc --model model-a",
    "backing": "local",
    "model": "model-a",
    "labels": ["code"],
    "capacity": 1,
    "egress": "local",
}
CODER_B = {**CODER_A, "name": "coder-b", "command": "pi --mode rpc --model model-b",
           "model": "model-b"}
HOSTED = {
    "name": "coder-hosted",
    "command": "pi --mode rpc --model vendor/strong",
    "backing": "hosted",
    "account": "openrouter-main",
    "labels": ["code", "strong-model"],
    "capacity": 2,
    "egress": "no-train",
}
WORKERS = {
    "name": "workers",
    "definition": "worker",
    "members": ["coder-a", "coder-b"],
    "size": 2,
    "idle_default": "10m",
    "idle_max": "4h",
}
CRITICS = {**WORKERS, "name": "critics", "definition": "critic", "members": ["coder-hosted"]}

DESIGNER = {"name": "designer", "pool": "workers", "count": 1}
BUILDER = {"name": "builder", "pool": "workers", "count": 1}
REVIEWER = {"name": "reviewer", "pool": "critics", "count": 2}


def _team(roles, notes="The orchestrator leads; members do not message each other.\n"):
    front = yaml.safe_dump({"roles": copy.deepcopy(list(roles))})
    return f"---\n{front}---\n\n{notes}"


def _write(tmp_path, team_files=None, pools=(WORKERS, CRITICS), members=(CODER_A, CODER_B, HOSTED)):
    """A pool directory: pool.yaml, the llama-swap fixture, definitions/ and teams/."""
    shutil.copy(FIXTURES / "llama-swap.yaml", tmp_path / "llama-swap.yaml")
    folder = tmp_path / config.DEFINITIONS_DIR
    folder.mkdir()
    for name, text in {"worker": WORKER, "critic": CRITIC}.items():
        (folder / f"{name}.md").write_text(text, encoding="utf-8")
    data = {
        "llama_swap": "llama-swap.yaml",
        "accounts": [ACCOUNT],
        "members": list(members),
        "pools": list(pools),
    }
    (tmp_path / config.POOL_FILE).write_text(yaml.safe_dump(copy.deepcopy(data)), encoding="utf-8")
    folder = tmp_path / teams.TEAMS_DIR
    folder.mkdir()
    if team_files is None:
        team_files = {"gamedev": _team([DESIGNER, BUILDER, REVIEWER])}
    for name, text in team_files.items():
        (folder / f"{name}.md").write_text(text, encoding="utf-8")
    return tmp_path


def _one_error(directory):
    cfg, errors = config.load_pool_config(directory)
    assert len(errors) == 1, [str(e) for e in errors]
    assert cfg.teams == ()
    return errors[0]


def _changed(entry, **changes):
    """*entry* with *changes* applied; a value of None drops the key."""
    merged = {**entry, **changes}
    return {key: value for key, value in merged.items() if value is not None}


# --- a valid team --------------------------------------------------------


def test_a_team_of_three_roles_loads(tmp_path):
    cfg, errors = config.load_pool_config(_write(tmp_path))

    assert errors == []
    assert cfg.teams == (
        teams.Team(
            name="gamedev",
            roles=(
                teams.Role(name="designer", pool="workers", count=1),
                teams.Role(name="builder", pool="workers", count=1),
                teams.Role(name="reviewer", pool="critics", count=2),
            ),
            notes="The orchestrator leads; members do not message each other.",
        ),
    )


def test_a_team_file_needs_no_notes(tmp_path):
    cfg, errors = config.load_pool_config(
        _write(tmp_path, {"pair": _team([DESIGNER, REVIEWER], notes="")})
    )

    assert errors == []
    assert cfg.teams[0].notes == ""


def test_teams_load_in_name_order(tmp_path):
    files = {"zeta": _team([DESIGNER]), "alpha": _team([REVIEWER])}

    cfg, errors = config.load_pool_config(_write(tmp_path, files))

    assert errors == []
    assert [t.name for t in cfg.teams] == ["alpha", "zeta"]


def test_a_directory_without_a_teams_folder_has_no_teams(tmp_path):
    directory = _write(tmp_path, {})
    (directory / teams.TEAMS_DIR).rmdir()

    cfg, errors = config.load_pool_config(directory)

    assert errors == []
    assert cfg.teams == ()


# --- the whole directory -------------------------------------------------


def test_the_pool_directory_loads_as_one_configuration(tmp_path):
    cfg, errors = config.load_pool_config(_write(tmp_path))

    assert errors == []
    assert cfg.path == tmp_path / config.POOL_FILE
    assert [d.name for d in cfg.definitions] == ["critic", "worker"]
    assert [a.name for a in cfg.accounts] == ["openrouter-main"]
    assert [m.name for m in cfg.members] == ["coder-a", "coder-b", "coder-hosted"]
    assert [p.name for p in cfg.pools] == ["workers", "critics"]
    assert [t.name for t in cfg.teams] == ["gamedev"]


def test_the_pool_file_alone_carries_no_teams(tmp_path):
    directory = _write(tmp_path)

    assert config.load_pool_file(directory / config.POOL_FILE).teams == ()


def test_three_separate_faults_in_a_directory_are_all_reported(tmp_path):
    directory = _write(
        tmp_path,
        {"gamedev": _team([DESIGNER, _changed(REVIEWER, pool="testers")])},
        members=(CODER_A, _changed(CODER_B, capacity=0), HOSTED),
        pools=(_changed(WORKERS, members=["coder-a"], size=1), CRITICS),
    )
    broken = WORKER.replace("egress: no-train", "egress: anywhere")
    (directory / config.DEFINITIONS_DIR / "scout.md").write_text(broken, encoding="utf-8")

    cfg, errors = config.load_pool_config(directory)

    assert [(e.path.name, e.entry, e.field) for e in errors] == [
        ("pool.yaml", "member 'coder-b'", "capacity"),
        ("scout.md", "definition 'scout'", "egress"),
        ("gamedev.md", "team 'gamedev' role 'reviewer'", "pool"),
    ]
    assert [p.name for p in cfg.pools] == ["workers", "critics"]
    assert cfg.teams == ()


def test_a_pool_file_that_cannot_be_read_is_the_only_fault(tmp_path):
    directory = _write(tmp_path)
    (directory / config.POOL_FILE).unlink()

    cfg, errors = config.load_pool_config(directory)

    assert [(e.entry, e.field) for e in errors] == [(config.FILE, "file")]
    assert cfg.teams == ()


def test_files_that_are_not_utf8_are_reported_with_the_other_faults_of_the_directory(tmp_path):
    directory = _write(
        tmp_path,
        {"gamedev": _team([DESIGNER, _changed(REVIEWER, pool="testers")])},
        members=(CODER_A, _changed(CODER_B, capacity=0), HOSTED),
        pools=(_changed(WORKERS, members=["coder-a"], size=1), CRITICS),
    )
    (directory / config.DEFINITIONS_DIR / "scout.md").write_bytes(
        WORKER.replace("the worker", "the caf\xe9 scout").encode("latin-1")
    )
    (directory / teams.TEAMS_DIR / "solo.md").write_bytes(b"---\nroles: []\n---\n\xe9\n")

    cfg, errors = config.load_pool_config(directory)

    assert [(e.path.name, e.entry, e.field) for e in errors] == [
        ("pool.yaml", "member 'coder-b'", "capacity"),
        ("scout.md", "definition 'scout'", "file"),
        ("gamedev.md", "team 'gamedev' role 'reviewer'", "pool"),
        ("solo.md", "team 'solo'", "file"),
    ]
    assert [p.name for p in cfg.pools] == ["workers", "critics"]


def test_a_pool_file_that_is_not_utf8_is_the_only_fault(tmp_path):
    directory = _write(tmp_path)
    (directory / config.POOL_FILE).write_bytes(b"accounts: []  # caf\xe9\n")

    cfg, errors = config.load_pool_config(directory)

    assert [(e.entry, e.field) for e in errors] == [(config.FILE, "file")]
    assert "not UTF-8" in str(errors[0])
    assert cfg.teams == ()


# --- a role that can never be granted ------------------------------------


def test_a_role_naming_a_missing_pool_is_refused(tmp_path):
    roles = [DESIGNER, BUILDER, _changed(REVIEWER, pool="testers")]

    error = _one_error(_write(tmp_path, {"gamedev": _team(roles)}))

    assert error.path == tmp_path / teams.TEAMS_DIR / "gamedev.md"
    assert (error.entry, error.field) == ("team 'gamedev' role 'reviewer'", "pool")
    assert "'testers' is not a declared pool" in str(error)


def test_a_role_with_a_count_of_zero_is_refused(tmp_path):
    roles = [DESIGNER, _changed(BUILDER, count=0), REVIEWER]

    error = _one_error(_write(tmp_path, {"gamedev": _team(roles)}))

    assert (error.entry, error.field) == ("team 'gamedev' role 'builder'", "count")
    assert "at least 1" in str(error)


def test_a_role_with_a_count_above_the_size_of_its_pool_is_refused(tmp_path):
    roles = [DESIGNER, BUILDER, _changed(REVIEWER, count=3)]

    error = _one_error(_write(tmp_path, {"gamedev": _team(roles)}))

    assert (error.entry, error.field) == ("team 'gamedev' role 'reviewer'", "count")
    assert "3 is more than pool 'critics' grants at once, 2" in str(error)


def test_a_role_on_a_pool_refused_for_its_own_fault_is_not_reported_again(tmp_path):
    directory = _write(tmp_path, pools=(WORKERS, _changed(CRITICS, size=5)))

    cfg, errors = config.load_pool_config(directory)

    assert [(e.entry, e.field) for e in errors] == [("pool 'critics'", "size")]
    # The team cannot stand without its pool, so it is left out.
    assert cfg.teams == ()


def test_a_role_on_a_pool_that_fell_with_its_member_is_not_reported_again(tmp_path):
    directory = _write(tmp_path, members=(CODER_A, CODER_B, _changed(HOSTED, capacity=0)))

    cfg, errors = config.load_pool_config(directory)

    assert [(e.entry, e.field) for e in errors] == [("member 'coder-hosted'", "capacity")]
    assert [p.name for p in cfg.pools] == ["workers"]
    assert cfg.teams == ()


# --- a team file that cannot be used -------------------------------------


@pytest.mark.parametrize("count", [None, "2", 1.5, True])
def test_a_count_that_is_not_a_whole_number_is_refused(tmp_path, count):
    roles = [_changed(DESIGNER, count=count)]

    error = _one_error(_write(tmp_path, {"solo": _team(roles)}))

    assert (error.entry, error.field) == ("team 'solo' role 'designer'", "count")


def test_a_role_without_a_pool_is_refused(tmp_path):
    error = _one_error(_write(tmp_path, {"solo": _team([_changed(DESIGNER, pool=None)])}))

    assert (error.entry, error.field) == ("team 'solo' role 'designer'", "pool")
    assert "required" in str(error)


def test_a_role_without_a_name_is_named_by_its_position(tmp_path):
    roles = [DESIGNER, _changed(BUILDER, name=None)]

    error = _one_error(_write(tmp_path, {"gamedev": _team(roles)}))

    assert (error.entry, error.field) == ("team 'gamedev' roles[2]", "name")


def test_an_unknown_key_in_a_role_is_refused(tmp_path):
    roles = [{**DESIGNER, "priority": 1}]

    error = _one_error(_write(tmp_path, {"solo": _team(roles)}))

    assert (error.entry, error.field) == ("team 'solo' role 'designer'", "priority")
    assert "unknown key" in str(error)


def test_two_roles_under_one_name_are_refused(tmp_path):
    roles = [DESIGNER, _changed(BUILDER, name="designer")]

    error = _one_error(_write(tmp_path, {"gamedev": _team(roles)}))

    assert (error.entry, error.field) == ("team 'gamedev' role 'designer'", "name")
    assert "more than once" in str(error)


@pytest.mark.parametrize("roles", [None, [], "designer", {"designer": "workers"}])
def test_a_team_without_a_list_of_roles_is_refused(tmp_path, roles):
    front = yaml.safe_dump({"roles": roles}) if roles is not None else ""

    error = _one_error(_write(tmp_path, {"empty": f"---\n{front}---\n"}))

    assert (error.entry, error.field) == ("team 'empty'", "roles")


def test_an_unknown_key_in_the_front_matter_is_refused(tmp_path):
    text = _team([DESIGNER]).replace("---\n", "---\nlead: designer\n", 1)

    error = _one_error(_write(tmp_path, {"solo": text}))

    assert (error.entry, error.field) == ("team 'solo'", "lead")
    assert "unknown key" in str(error)


@pytest.mark.parametrize(
    "text",
    ["roles: []\n", "---\nroles: [\n---\n", "---\n- designer\n---\n"],
    ids=["no front matter", "not YAML", "not a mapping"],
)
def test_a_file_without_usable_front_matter_is_refused(tmp_path, text):
    error = _one_error(_write(tmp_path, {"solo": text}))

    assert (error.entry, error.field) == ("team 'solo'", teams.FRONT_MATTER)


def test_a_team_file_that_is_not_utf8_is_refused_naming_the_file(tmp_path):
    directory = _write(tmp_path)
    file = directory / teams.TEAMS_DIR / "solo.md"
    file.write_bytes(b"---\nroles: []\n---\n\nThe caf\xe9 team.\n")

    cfg, errors = config.load_pool_config(directory)

    assert [(e.path, e.entry, e.field) for e in errors] == [(file, "team 'solo'", "file")]
    assert "not UTF-8" in str(errors[0])
    assert [t.name for t in cfg.teams] == ["gamedev"]


def test_every_fault_in_a_team_is_reported_in_one_pass(tmp_path):
    roles = [
        _changed(DESIGNER, pool="artists", count=0),
        _changed(REVIEWER, count=3),
    ]
    files = {"gamedev": _team(roles), "pair": _team([DESIGNER, REVIEWER])}

    cfg, errors = config.load_pool_config(_write(tmp_path, files))

    assert [(e.entry, e.field) for e in errors] == [
        ("team 'gamedev' role 'designer'", "pool"),
        ("team 'gamedev' role 'designer'", "count"),
        ("team 'gamedev' role 'reviewer'", "count"),
    ]
    assert [t.name for t in cfg.teams] == ["pair"]

"""The pool configuration file: named pools.

A named pool binds one agent definition to declared members, so a consumer
asks for a pool by name and the fit is already known. A binding that cannot
hold is refused when the configuration is checked, with an error naming the
pool and the cause, and never when a request is waiting on it.
"""

import copy
import shutil
from pathlib import Path

import pytest
import yaml

from specflo.pool import config

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
POOL = {
    "name": "workers",
    "definition": "worker",
    "members": ["coder-a", "coder-b"],
    "size": 2,
    "idle_default": "10m",
    "idle_max": "4h",
    "preempt_after": "5m",
}


def _write(tmp_path, pools=(POOL,), members=(CODER_A, CODER_B, HOSTED), definitions=None):
    """A pool directory: pool.yaml, the llama-swap fixture and definitions/."""
    shutil.copy(FIXTURES / "llama-swap.yaml", tmp_path / "llama-swap.yaml")
    shutil.copy(FIXTURES / "models.json", tmp_path / "models.json")
    folder = tmp_path / config.DEFINITIONS_DIR
    folder.mkdir()
    for name, text in ({"worker": WORKER} if definitions is None else definitions).items():
        (folder / f"{name}.md").write_text(text, encoding="utf-8")
    data = {
        "llama_swap": "llama-swap.yaml",
        "models_file": "models.json",
        "accounts": [ACCOUNT],
        "members": list(members),
        "pools": list(pools),
    }
    path = tmp_path / "pool.yaml"
    path.write_text(yaml.safe_dump(copy.deepcopy(data)), encoding="utf-8")
    return path


def _refused(path):
    with pytest.raises(config.ConfigError) as excinfo:
        config.load_pool_file(path)
    return excinfo.value


def _changed(entry, **changes):
    """*entry* with *changes* applied; a value of None drops the key."""
    merged = {**entry, **changes}
    return {key: value for key, value in merged.items() if value is not None}


# --- a valid pool --------------------------------------------------------


def test_a_pool_of_two_members_bound_to_the_worker_definition_loads(tmp_path):
    cfg = config.load_pool_file(_write(tmp_path))

    assert cfg.pools == (
        config.Pool(
            name="workers",
            definition="worker",
            members=("coder-a", "coder-b"),
            size=2,
            idle_default=10 * 60,
            idle_max=4 * 60 * 60,
            preempt_after=5 * 60,
        ),
    )


def test_the_definitions_beside_the_pool_file_are_loaded_with_it(tmp_path):
    cfg = config.load_pool_file(_write(tmp_path))

    assert [d.name for d in cfg.definitions] == ["worker"]
    assert cfg.definitions[0].needs == ("code",)


def test_a_pool_without_preempt_after_is_never_preempted(tmp_path):
    path = _write(tmp_path, pools=[_changed(POOL, preempt_after=None)])

    assert config.load_pool_file(path).pools[0].preempt_after is None


def test_a_pool_may_be_smaller_than_its_members_capacity(tmp_path):
    path = _write(tmp_path, pools=[_changed(POOL, size=1)])

    assert config.load_pool_file(path).pools[0].size == 1


def test_a_member_stricter_than_the_definition_accepts_is_allowed(tmp_path):
    # The worker accepts no-train; a local member is stricter than that.
    path = _write(tmp_path, pools=[_changed(POOL, members=["coder-a", "coder-hosted"])])

    assert config.load_pool_file(path).pools[0].members == ("coder-a", "coder-hosted")


def test_a_file_without_pools_has_none(tmp_path):
    cfg = config.load_pool_file(_write(tmp_path, pools=[]))

    assert cfg.pools == ()


@pytest.mark.parametrize("text, seconds", [("45s", 45), ("10m", 600), ("4h", 14400)])
def test_a_limit_is_a_whole_number_of_seconds_minutes_or_hours(tmp_path, text, seconds):
    path = _write(tmp_path, pools=[_changed(POOL, idle_max=text, idle_default="30s",
                                            preempt_after=None)])

    assert config.load_pool_file(path).pools[0].idle_max == seconds


# --- the rejected bindings -----------------------------------------------


def test_a_pool_naming_an_undeclared_definition_is_refused(tmp_path):
    error = _refused(_write(tmp_path, pools=[_changed(POOL, definition="critic")]))

    assert error.entry == "pool 'workers'"
    assert error.field == "definition"
    assert "workers" in str(error) and "'critic' is not a declared definition" in str(error)


def test_a_pool_naming_an_undeclared_member_is_refused(tmp_path):
    error = _refused(_write(tmp_path, pools=[_changed(POOL, members=["coder-a", "coder-z"])]))

    assert (error.entry, error.field) == ("pool 'workers'", "members")
    assert "'coder-z' is not a declared member" in str(error)


def test_a_member_lacking_a_label_the_definition_needs_is_refused(tmp_path):
    members = [CODER_A, _changed(CODER_B, labels=["review"]), HOSTED]

    error = _refused(_write(tmp_path, members=members))

    assert (error.entry, error.field) == ("pool 'workers'", "members")
    assert "coder-b" in str(error) and "'code'" in str(error) and "worker" in str(error)


def test_a_member_more_open_than_the_definition_accepts_is_refused(tmp_path):
    members = [CODER_A, CODER_B, _changed(HOSTED, egress="open")]
    pool = _changed(POOL, members=["coder-a", "coder-hosted"])

    error = _refused(_write(tmp_path, pools=[pool], members=members))

    assert (error.entry, error.field) == ("pool 'workers'", "members")
    assert "coder-hosted" in str(error)
    assert "'open'" in str(error) and "'no-train'" in str(error)


def test_a_size_above_the_sum_of_member_capacities_is_refused(tmp_path):
    error = _refused(_write(tmp_path, pools=[_changed(POOL, size=3)]))

    assert (error.entry, error.field) == ("pool 'workers'", "size")
    assert "3" in str(error) and "2" in str(error)


def test_a_default_idle_limit_above_the_maximum_is_refused(tmp_path):
    error = _refused(_write(tmp_path, pools=[_changed(POOL, idle_default="5h")]))

    assert (error.entry, error.field) == ("pool 'workers'", "idle_default")
    assert "5h" in str(error) and "4h" in str(error)


@pytest.mark.parametrize("preempt_after", ["10m", "15m"])
def test_a_preempt_after_not_shorter_than_the_default_idle_limit_is_refused(
    tmp_path, preempt_after
):
    error = _refused(_write(tmp_path, pools=[_changed(POOL, preempt_after=preempt_after)]))

    assert (error.entry, error.field) == ("pool 'workers'", "preempt_after")
    assert preempt_after in str(error) and "10m" in str(error)


# --- every pool ----------------------------------------------------------


@pytest.mark.parametrize(
    "field, value",
    [
        ("definition", None),
        ("definition", "  "),
        ("members", None),
        ("members", []),
        ("members", "coder-a"),
        ("members", [1]),
        ("size", None),
        ("size", 0),
        ("size", "2"),
        ("idle_default", None),
        ("idle_default", 600),
        ("idle_default", "ten minutes"),
        ("idle_default", "0m"),
        ("idle_max", None),
        ("idle_max", "4 days"),
        ("preempt_after", 300),
        ("preempt_after", "soon"),
    ],
)
def test_a_pool_field_that_is_missing_or_mistyped_is_refused(tmp_path, field, value):
    pool = dict(POOL)
    pool.pop(field)
    if value is not None:
        pool[field] = value

    error = _refused(_write(tmp_path, pools=[pool]))

    assert (error.entry, error.field) == ("pool 'workers'", field)


def test_a_member_listed_twice_in_a_pool_is_refused(tmp_path):
    # Its capacity would count twice toward the pool's size.
    error = _refused(_write(tmp_path, pools=[_changed(POOL, members=["coder-a", "coder-a"])]))

    assert (error.entry, error.field) == ("pool 'workers'", "members")
    assert "more than once" in str(error)


def test_a_duplicate_pool_name_is_refused(tmp_path):
    error = _refused(_write(tmp_path, pools=[POOL, _changed(POOL, size=1)]))

    assert (error.entry, error.field) == ("pool 'workers'", "name")
    assert "more than once" in str(error)


def test_an_unknown_pool_key_is_refused(tmp_path):
    error = _refused(_write(tmp_path, pools=[_changed(POOL, priority=3)]))

    assert (error.entry, error.field) == ("pool 'workers'", "priority")


def test_a_pool_without_a_name_is_named_by_its_position(tmp_path):
    error = _refused(_write(tmp_path, pools=[POOL, _changed(POOL, name=None)]))

    assert (error.entry, error.field) == ("pools[2]", "name")


def test_one_pool_may_bind_a_member_that_another_pool_binds(tmp_path):
    # Capacity is shared out between pools when leases are granted, not here.
    second = _changed(POOL, name="reviewers", members=["coder-a"], size=1)

    cfg = config.load_pool_file(_write(tmp_path, pools=[POOL, second]))

    assert [p.name for p in cfg.pools] == ["workers", "reviewers"]


# --- faults reported elsewhere -------------------------------------------


def test_a_pool_on_a_member_refused_for_its_own_fault_is_not_faulted_again(tmp_path):
    members = [CODER_A, _changed(CODER_B, capacity=0)]

    cfg, errors = config.check_pool_file(_write(tmp_path, members=members))

    assert [(e.entry, e.field) for e in errors] == [("member 'coder-b'", "capacity")]
    # The pool cannot stand without its member, so it is left out.
    assert cfg.pools == ()


def test_a_definition_that_cannot_be_loaded_is_reported_once_under_its_own_name(tmp_path):
    broken = WORKER.replace("role: Does one plan task and reports what changed\n", "")
    path = _write(tmp_path, definitions={"worker": broken})

    cfg, errors = config.check_pool_file(path)

    assert [(e.entry, e.field) for e in errors] == [("definition 'worker'", "role")]
    assert errors[0].path == tmp_path / config.DEFINITIONS_DIR / "worker.md"
    assert "required" in str(errors[0])
    assert cfg.definitions == () and cfg.pools == ()


def test_every_fault_in_the_pools_is_reported_in_one_pass(tmp_path):
    pools = [
        _changed(POOL, definition="critic", size=3),
        _changed(POOL, name="reviewers", members=["coder-z"], idle_default="5h"),
    ]

    cfg, errors = config.check_pool_file(_write(tmp_path, pools=pools))

    assert [(e.entry, e.field) for e in errors] == [
        ("pool 'workers'", "definition"),
        ("pool 'workers'", "size"),
        ("pool 'reviewers'", "members"),
        ("pool 'reviewers'", "idle_default"),
    ]
    assert cfg.pools == ()

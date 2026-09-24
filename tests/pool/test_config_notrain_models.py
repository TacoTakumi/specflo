"""A no-train member on a model whose pi client sends no provider routing.

A no-train member's class holds only if OpenRouter gets the flags that deny
data collection and demand zero retention. pi reaches some vendors' models
through a client that never sends them, so such a member is refused when the
roster is checked: it would otherwise run, off this host, with no flags at all.
"""

import copy

import pytest
import yaml

from specflo.pool import config

ACCOUNT = {"name": "openrouter-main", "cap": 4, "key_env": "OPENROUTER_API_KEY"}
HOSTED = {
    "name": "strong-hosted",
    "command": "pi --mode rpc --provider openrouter",
    "backing": "hosted",
    "account": "openrouter-main",
    "labels": ["strong-model"],
    "capacity": 2,
    "egress": "no-train",
}


def _write(tmp_path, **changes):
    """A pool file whose one member is the hosted member with *changes* applied."""
    data = {"accounts": [ACCOUNT], "members": [{**HOSTED, **changes}]}
    path = tmp_path / "pool.yaml"
    path.write_text(yaml.safe_dump(copy.deepcopy(data)), encoding="utf-8")
    return path


def _refused(path):
    with pytest.raises(config.ConfigError) as excinfo:
        config.load_pool_file(path)
    return excinfo.value


def _command(*model_args):
    return " ".join(("pi --mode rpc --provider openrouter", *model_args))


def _declared(model):
    """A declared model, which a hosted member's command must also select."""
    return {"model": model, "command": _command("--model", model)}


# --- the refused prefixes ------------------------------------------------


def test_the_refused_vendor_prefixes_are_one_constant():
    assert config.NO_ROUTING_PREFIXES == ("anthropic/",)


# --- the model field -----------------------------------------------------


def test_a_no_train_member_whose_model_is_an_anthropic_model_is_refused(tmp_path):
    error = _refused(_write(tmp_path, model="anthropic/claude-sonnet-5"))

    assert error.entry == "member 'strong-hosted'"
    assert error.field == "model"
    assert "anthropic/claude-sonnet-5" in str(error)
    assert "no-train flags would not reach the provider" in str(error)


def test_a_provider_prefix_on_the_model_field_does_not_hide_the_vendor(tmp_path):
    error = _refused(_write(tmp_path, model="openrouter/anthropic/claude-sonnet-5"))

    assert (error.entry, error.field) == ("member 'strong-hosted'", "model")


def test_the_vendor_is_matched_whatever_its_case(tmp_path):
    # pi matches a model ID without regard to case.
    error = _refused(_write(tmp_path, model="Anthropic/Claude-Sonnet-5"))

    assert (error.entry, error.field) == ("member 'strong-hosted'", "model")


# --- the command ---------------------------------------------------------


@pytest.mark.parametrize(
    "model_args",
    [
        ("--model", "anthropic/claude-sonnet-5"),
        ("--model=anthropic/claude-sonnet-5",),
        ("--model", "openrouter/anthropic/claude-sonnet-5"),
        ("--model=openrouter/anthropic/claude-sonnet-5",),
        ("--model", "'anthropic/claude-sonnet-5'"),
    ],
)
def test_a_no_train_member_whose_command_names_an_anthropic_model_is_refused(
    tmp_path, model_args
):
    error = _refused(_write(tmp_path, command=_command(*model_args)))

    assert error.entry == "member 'strong-hosted'"
    assert error.field == "command"
    assert "anthropic/claude-sonnet-5" in str(error)
    assert "no-train flags would not reach the provider" in str(error)


def test_a_model_in_both_places_is_reported_for_each(tmp_path):
    path = _write(
        tmp_path,
        command=_command("--model", "anthropic/claude-sonnet-5"),
        model="anthropic/claude-sonnet-5",
    )

    cfg, errors = config.check_pool_file(path)

    assert [(e.entry, e.field) for e in errors] == [
        ("member 'strong-hosted'", "model"),
        ("member 'strong-hosted'", "command"),
    ]
    assert cfg.members == ()


def test_a_command_the_shell_rules_cannot_split_is_left_to_the_launch(tmp_path):
    # An unclosed quote is not this check's fault to report.
    path = _write(tmp_path, command="pi --mode rpc --model 'vendor/strong")

    assert config.load_pool_file(path).members[0].name == "strong-hosted"


# --- a partial model name -----------------------------------------------


@pytest.mark.parametrize(
    "model_args, named",
    [
        (("--model", "sonnet"), "sonnet"),
        (("--model=claude-sonnet-5",), "claude-sonnet-5"),
        (("--model", "openrouter/sonnet"), "openrouter/sonnet"),
        (("--model", "sonnet:high"), "sonnet:high"),
    ],
)
def test_a_no_train_member_whose_command_names_part_of_a_model_is_refused(
    tmp_path, model_args, named
):
    # pi takes a name without a vendor as a pattern, and picks the model.
    error = _refused(_write(tmp_path, command=_command(*model_args)))

    assert error.entry == "member 'strong-hosted'"
    assert error.field == "command"
    assert f"'{named}'" in str(error)
    assert "partial model name" in str(error)
    assert "does not send the no-train flags" in str(error)
    assert "full" in str(error)


@pytest.mark.parametrize("model", ["sonnet", "openrouter/claude-sonnet-5"])
def test_a_no_train_member_whose_model_is_part_of_a_model_is_refused(tmp_path, model):
    error = _refused(_write(tmp_path, model=model))

    assert (error.entry, error.field) == ("member 'strong-hosted'", "model")
    assert "partial model name" in str(error)


@pytest.mark.parametrize(
    "changes",
    [_declared("sonnet"), {"command": _command("--model", "sonnet")}],
)
def test_a_partial_model_name_on_a_member_of_class_open_passes(tmp_path, changes):
    path = _write(tmp_path, egress="open", **changes)

    assert config.load_pool_file(path).members[0].egress == "open"


# --- what still passes ---------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        _declared("anthropic/claude-sonnet-5"),
        {"command": _command("--model", "anthropic/claude-sonnet-5")},
        {"command": _command("--model=openrouter/anthropic/claude-sonnet-5")},
    ],
)
def test_the_same_member_with_class_open_passes(tmp_path, changes):
    # An open member asks OpenRouter for nothing, so nothing is lost.
    path = _write(tmp_path, egress="open", **changes)

    assert config.load_pool_file(path).members[0].egress == "open"


@pytest.mark.parametrize(
    "changes",
    [
        _declared("vendor/strong"),
        _declared("openrouter/vendor/strong"),
        {"command": _command("--model", "vendor/strong")},
        {"command": _command("--model=openrouter/vendor/strong")},
        # The vendor's name inside another vendor's model ID is not a prefix.
        _declared("vendor/anthropic/claude-sonnet-5"),
    ],
)
def test_a_no_train_member_on_another_vendors_model_passes(tmp_path, changes):
    path = _write(tmp_path, **changes)

    member = config.load_pool_file(path).members[0]

    assert member.egress == "no-train"
    assert member.model == changes.get("model")

import dataclasses
import inspect

import pytest

from specflo import auto, config
from specflo.errors import SpecfloError


def test_config_has_no_auto_advance_field():
    """Regression guard: this project adds no auto_advance config key (REQ-11)."""
    names = {f.name for f in dataclasses.fields(config.SpecfloConfig)}
    assert "auto_advance" not in names


def test_written_config_has_no_auto_advance_key(tmp_path):
    config.init_config(tmp_path)
    text = (tmp_path / ".specflo" / "config.yaml").read_text()
    assert "auto_advance" not in text
    assert not hasattr(config.load_config(tmp_path), "auto_advance")


def test_init_creates_config_file_and_projects_dir(tmp_path):
    cfg = config.init_config(tmp_path)

    assert (tmp_path / ".specflo" / "config.yaml").is_file()
    assert (tmp_path / "docs" / "projects").is_dir()
    assert cfg.projects_dir == "docs/projects"
    assert cfg.active_project is None


def test_init_respects_a_custom_projects_dir(tmp_path):
    cfg = config.init_config(tmp_path, projects_dir="specs")

    assert (tmp_path / "specs").is_dir()
    assert cfg.projects_dir == "specs"


def test_init_twice_raises_unless_forced(tmp_path):
    config.init_config(tmp_path)

    with pytest.raises(SpecfloError):
        config.init_config(tmp_path)

    # --force re-initializes without error.
    config.init_config(tmp_path, force=True)


def test_save_then_load_round_trips_the_active_project(tmp_path):
    cfg = config.init_config(tmp_path)
    cfg.active_project = "my-thing"
    config.save_config(tmp_path, cfg)

    reloaded = config.load_config(tmp_path)
    assert reloaded.active_project == "my-thing"
    assert reloaded.projects_dir == "docs/projects"


def test_load_without_init_raises(tmp_path):
    with pytest.raises(SpecfloError):
        config.load_config(tmp_path)


def test_find_root_walks_up_from_a_nested_subdir(tmp_path):
    config.init_config(tmp_path)
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)

    assert config.find_root(nested) == tmp_path


def test_find_root_returns_none_when_not_initialized(tmp_path):
    assert config.find_root(tmp_path) is None


def test_display_path_relative_to_root_os_native_by_default(tmp_path):
    target = tmp_path / "docs" / "projects" / "x"
    assert config.display_path(target, tmp_path) == str(target.relative_to(tmp_path))


def test_display_path_posix_uses_forward_slashes(tmp_path):
    target = tmp_path / "docs" / "projects" / "x"
    assert config.display_path(target, tmp_path, posix=True) == "docs/projects/x"


def test_display_path_outside_root_falls_back_to_absolute(tmp_path):
    # a path that isn't under root can't be relativized -> emitted absolute,
    # honoring the same separator policy (str vs POSIX) as the relative case.
    outside = (tmp_path / ".." / "elsewhere" / "y").resolve()
    assert config.display_path(outside, tmp_path) == str(outside)
    assert config.display_path(outside, tmp_path, posix=True) == outside.as_posix()


# --- the field registry (REQ-28, REQ-29, REQ-30) -------------------------
# One ordered registry carries every key's name, type, default, description and
# validator. Nothing outside it may define a key's default or description, and
# `load_config` builds the resolved-values object by iterating it.


def _registry_names():
    return [f.name for f in config.CONFIG_FIELDS]


def test_registry_carries_all_five_attributes_for_every_key():
    assert config.CONFIG_FIELDS, "the registry is empty"
    for field in config.CONFIG_FIELDS:
        assert field.name and isinstance(field.name, str)
        # A real type, so a CLI layer can coerce a string argument with it.
        assert isinstance(field.type, type)
        assert field.default is None or isinstance(field.default, field.type)
        assert field.description and not field.description.startswith(" ")
        assert field.description.isascii()
        assert callable(field.validate)


def test_every_registry_validator_accepts_its_own_default():
    for field in config.CONFIG_FIELDS:
        assert field.validate(field.default), field.name


def test_the_dataclass_fields_are_exactly_the_registry_in_order():
    # The resolved-values dataclass is driven by the registry, so a key can't
    # exist in one and not the other, and the file layout order is the registry's.
    names = [f.name for f in dataclasses.fields(config.SpecfloConfig)]
    assert names[: len(config.CONFIG_FIELDS)] == _registry_names()
    # Anything after the registry keys is loader metadata, not a config key.
    assert names[len(config.CONFIG_FIELDS) :] == ["present_keys"]


def test_every_registry_key_is_readable_as_an_attribute(tmp_path):
    config.init_config(tmp_path)
    cfg = config.load_config(tmp_path)
    for field in config.CONFIG_FIELDS:
        assert getattr(cfg, field.name) == field.default


def test_bare_dataclass_defaults_come_from_the_registry():
    cfg = config.SpecfloConfig()
    for field in config.CONFIG_FIELDS:
        assert getattr(cfg, field.name) == field.default


def test_the_loader_holds_no_per_key_literal():
    # REQ-28/REQ-29: load_config iterates the registry. A key named in the
    # loader's own source would be a second definition site.
    source = inspect.getsource(config.load_config)
    for name in _registry_names():
        assert name not in source, f"{name} is spelled out in load_config"


def test_the_autonomy_domain_is_defined_once(tmp_path):
    # `auto` re-exports the levels rather than restating them: a second literal
    # would be a different tuple object.
    assert auto.AUTONOMY_LEVELS is config.AUTONOMY_LEVELS
    assert auto.DEFAULT_AUTONOMY == config.DEFAULT_AUTONOMY


def test_present_keys_reports_only_the_keys_in_the_file(tmp_path):
    config.init_config(tmp_path)
    assert config.load_config(tmp_path).present_keys == {
        "projects_dir",
        "active_project",
    }


def test_present_keys_includes_keys_the_registry_does_not_know(tmp_path):
    # `config list` needs to name unrecognized keys, so presence is reported
    # unfiltered - the caller intersects it with the registry.
    config.init_config(tmp_path)
    path = config.config_path(tmp_path)
    path.write_text(path.read_text() + "autonomy: yolo\nmystery_key: 1\n")

    present = config.load_config(tmp_path).present_keys
    assert "autonomy" in present
    assert "mystery_key" in present


def test_present_keys_is_not_persisted(tmp_path):
    # It is loader metadata about the file, never a key in it.
    config.init_config(tmp_path)
    cfg = config.load_config(tmp_path)
    config.save_config(tmp_path, cfg)
    assert "present_keys" not in config.config_path(tmp_path).read_text()


# --- degrade-and-warn on an invalid value (REQ-25, REQ-26) ---------------
# A hand-edited config must not break every command that loads it, and the pi
# extension polls `status --json` every turn - so a bad value falls back to the
# shipped default and says so on stderr, once per key per process.

BAD_VALUES = {
    "autonomy": "bogus",
    "auto_max_passes": 0,
    "context_threshold_percent": 101,
}


def _write_values(tmp_path, values):
    config.init_config(tmp_path)
    path = config.config_path(tmp_path)
    path.write_text(
        path.read_text() + "".join(f"{k}: {v!r}\n" for k, v in values.items())
    )
    return path


@pytest.mark.parametrize("key,bad", sorted(BAD_VALUES.items()))
def test_an_invalid_value_degrades_to_the_default_and_warns(tmp_path, capsys, key, bad):
    _write_values(tmp_path, {key: bad})
    cfg = config.load_config(tmp_path)

    assert getattr(cfg, key) == config.FIELDS_BY_NAME[key].default
    err = capsys.readouterr().err
    assert err.strip().count("\n") == 0  # exactly one line
    assert key in err
    assert repr(bad) in err


def test_every_invalid_key_gets_its_own_line(tmp_path, capsys):
    _write_values(tmp_path, BAD_VALUES)
    cfg = config.load_config(tmp_path)

    for key, bad in BAD_VALUES.items():
        assert getattr(cfg, key) == config.FIELDS_BY_NAME[key].default
    lines = capsys.readouterr().err.strip().splitlines()
    assert len(lines) == len(BAD_VALUES)
    assert {key for key in BAD_VALUES if any(key in line for line in lines)} == set(
        BAD_VALUES
    )


def test_a_bad_key_warns_only_once_per_process(tmp_path, capsys):
    # `specflo auto` calls load_config many times in one invocation; the warning
    # must not repeat (REQ-26).
    _write_values(tmp_path, {"autonomy": "bogus"})
    config.load_config(tmp_path)
    capsys.readouterr()

    config.load_config(tmp_path)
    config.load_config(tmp_path)
    assert capsys.readouterr().err == ""


def test_a_valid_config_warns_nothing(tmp_path, capsys):
    _write_values(tmp_path, {"autonomy": "yolo", "auto_max_passes": 5})
    cfg = config.load_config(tmp_path)

    assert cfg.autonomy == "yolo"
    assert cfg.auto_max_passes == 5
    assert capsys.readouterr().err == ""


def test_loading_never_raises_on_a_bad_value(tmp_path, capsys):
    _write_values(tmp_path, {key: [1, 2] for key in BAD_VALUES})
    config.load_config(tmp_path)  # must not raise
    capsys.readouterr()


# --- the context arming threshold (pi-extension REQ-28) ------------------
# The pi extension arms its clear-and-continue trigger at a percent of the model
# context window. The percent is configured here, alongside the existing auto
# defaults, and read back out through the CLI - never by parsing this file.


def test_threshold_defaults_to_25():
    # 25, not 75 (REQ-01): the extension *arms* here and the next specflo seam
    # fires it, so arming has to happen with enough window left to finish the
    # step in flight.
    assert config.DEFAULT_CONTEXT_THRESHOLD_PERCENT == 25
    assert config.SpecfloConfig().context_threshold_percent == 25


def test_threshold_round_trips_a_custom_value(tmp_path):
    config.init_config(tmp_path)
    cfg = config.load_config(tmp_path)
    cfg.context_threshold_percent = 60
    config.save_config(tmp_path, cfg)

    assert "context_threshold_percent" in config.config_path(tmp_path).read_text()
    assert config.load_config(tmp_path).context_threshold_percent == 60


def test_threshold_key_is_omitted_at_the_default(tmp_path):
    # Same treatment as autonomy / auto_max_passes: a plain project's config
    # carries no tuning key at all.
    config.init_config(tmp_path)
    assert "context_threshold_percent" not in config.config_path(tmp_path).read_text()


@pytest.mark.parametrize(
    "raw", ["seventy", 0, 101, -5, 12.5, None, True, [25]],
)
def test_threshold_falls_back_to_the_default_when_unusable(tmp_path, raw):
    # A hand-edited config must not break every command that loads it: an
    # unusable percent degrades to the default rather than raising. `True` is
    # included deliberately - bool is an int subclass, so a naive isinstance
    # check would accept it as the percent 1.
    config.init_config(tmp_path)
    path = config.config_path(tmp_path)
    path.write_text(
        path.read_text() + f"context_threshold_percent: {raw!r}\n"
    )
    assert (
        config.load_config(tmp_path).context_threshold_percent
        == config.DEFAULT_CONTEXT_THRESHOLD_PERCENT
    )


@pytest.mark.parametrize("raw", [1, 50, 100])
def test_threshold_accepts_the_whole_percent_range(tmp_path, raw):
    config.init_config(tmp_path)
    path = config.config_path(tmp_path)
    path.write_text(path.read_text() + f"context_threshold_percent: {raw}\n")
    assert config.load_config(tmp_path).context_threshold_percent == raw

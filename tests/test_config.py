import dataclasses
import inspect
import tomllib
from pathlib import Path

import pytest
import yaml

from specflo import auto, config, projects
from specflo.errors import SpecfloError


def _live_keys(text: str) -> list[str]:
    """The keys actually set in a config file - commented-out entries excluded."""
    return [
        line.split(":", 1)[0]
        for line in text.splitlines()
        if ":" in line and not line.startswith("#")
    ]


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


def test_threshold_key_is_commented_out_at_the_default(tmp_path):
    # Same treatment as autonomy / auto_max_passes: a plain project's config
    # carries no live tuning key. REQ-03/REQ-04 supersede the earlier contract
    # that left an unset key out of the file entirely - it is now written as a
    # commented-out line, so the file documents the knob without setting it.
    config.init_config(tmp_path)
    text = config.config_path(tmp_path).read_text()

    assert "context_threshold_percent" not in _live_keys(text)
    assert "# context_threshold_percent: 25" in text


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


# --- writes round-trip the file (REQ-07, REQ-08, REQ-09) -----------------
# `save_config` mutates the document it loaded from disk instead of emitting a
# fresh one, so everything specflo does not own - the user's comments, the key
# order they chose, keys the registry has never heard of - survives a write.

HAND_WRITTEN = """\
# why this repo keeps its specs out of docs/
projects_dir: specs
mystery_key: 42
active_project: alpha
"""


def _hand_written(tmp_path):
    config.init_config(tmp_path)
    path = config.config_path(tmp_path)
    path.write_text(HAND_WRITTEN)
    return path


def test_a_save_preserves_a_hand_written_comment(tmp_path):
    path = _hand_written(tmp_path)
    cfg = config.load_config(tmp_path)
    cfg.active_project = "beta"
    config.save_config(tmp_path, cfg)

    assert "# why this repo keeps its specs out of docs/" in path.read_text()


def test_a_save_preserves_the_existing_key_order(tmp_path):
    # The file's order wins over the registry's - it is the user's file.
    path = _hand_written(tmp_path)
    cfg = config.load_config(tmp_path)
    cfg.active_project = "beta"
    config.save_config(tmp_path, cfg)

    keys = [line.split(":")[0] for line in path.read_text().splitlines() if ":" in line]
    assert [k for k in keys if not k.startswith("#")] == [
        "projects_dir",
        "mystery_key",
        "active_project",
    ]


def test_a_save_preserves_an_unrecognized_key_and_its_value(tmp_path):
    path = _hand_written(tmp_path)
    cfg = config.load_config(tmp_path)
    cfg.active_project = "beta"
    config.save_config(tmp_path, cfg)

    assert yaml.safe_load(path.read_text())["mystery_key"] == 42


def test_a_save_writes_the_values_it_owns(tmp_path):
    # Preservation is not inertia: the keys specflo owns still take the new value.
    path = _hand_written(tmp_path)
    cfg = config.load_config(tmp_path)
    cfg.active_project = "beta"
    config.save_config(tmp_path, cfg)

    assert yaml.safe_load(path.read_text())["active_project"] == "beta"
    assert config.load_config(tmp_path).active_project == "beta"


def test_the_config_is_never_emitted_from_a_fresh_mapping(tmp_path):
    # REQ-08 is structural: the module writes the file only by dumping a document
    # it first loaded. A PyYAML dump or an f-string template would be a regression.
    source = inspect.getsource(config)
    assert "safe_dump" not in source
    assert "yaml.dump" not in source


def _src_files():
    root = Path(config.__file__).parent
    return sorted(p for p in root.rglob("*.py"))


def test_ruamel_is_imported_only_by_the_config_module():
    users = [p.name for p in _src_files() if "ruamel" in p.read_text()]
    assert users == ["config.py"]


def test_front_matter_is_still_read_and_written_with_pyyaml():
    source = inspect.getsource(projects)
    assert "yaml.safe_dump" in inspect.getsource(projects._render)
    assert "yaml.safe_load" in inspect.getsource(projects._parse_frontmatter)
    assert "ruamel" not in source


def test_ruamel_is_a_declared_runtime_dependency():
    pyproject = Path(config.__file__).parents[2] / "pyproject.toml"
    deps = tomllib.loads(pyproject.read_text())["project"]["dependencies"]
    assert any(d.startswith("ruamel.yaml") for d in deps)


def test_a_save_leaves_a_long_untouched_value_on_its_own_line(tmp_path):
    # The emitter would otherwise fold a long scalar onto a continuation line,
    # rewriting a value specflo never touched.
    config.init_config(tmp_path)
    path = config.config_path(tmp_path)
    long_value = "x" * 200
    path.write_text(path.read_text() + f"mystery_key: {long_value}\n")
    cfg = config.load_config(tmp_path)
    cfg.active_project = "beta"
    config.save_config(tmp_path, cfg)

    assert f"mystery_key: {long_value}" in path.read_text()


# --- the file documents itself (REQ-03, REQ-04, REQ-05) ------------------
# Every registry key appears in the file: live once it is set, commented out at
# its shipped default while it is not, each under its one-line description.


def _items(text: str) -> list[str]:
    """The config entries in file order, live (`key: v`) and commented alike."""
    entries = []
    for line in text.splitlines():
        name = line.lstrip("# ").split(":", 1)[0]
        if ":" in line and name in config.FIELDS_BY_NAME:
            entries.append(name)
    return entries


def test_a_fresh_config_carries_an_entry_for_every_registry_key_and_no_other(tmp_path):
    config.init_config(tmp_path)
    assert _items(config.config_path(tmp_path).read_text()) == _registry_names()


def test_an_unset_key_is_commented_out_at_its_shipped_default(tmp_path):
    config.init_config(tmp_path)
    text = config.config_path(tmp_path).read_text()

    assert "# autonomy: safe" in text
    assert "autonomy" not in _live_keys(text)


def test_a_set_key_is_live_with_no_commented_duplicate(tmp_path):
    config.init_config(tmp_path)
    cfg = config.load_config(tmp_path)
    cfg.autonomy = "autonomous"
    config.save_config(tmp_path, cfg)
    text = config.config_path(tmp_path).read_text()

    assert "autonomy: autonomous" in text
    assert "# autonomy:" not in text
    assert _items(text).count("autonomy") == 1


def test_every_item_sits_under_its_description_after_one_blank_line(tmp_path):
    config.init_config(tmp_path)
    lines = config.config_path(tmp_path).read_text().splitlines()

    for spec in config.CONFIG_FIELDS:
        at = next(i for i, line in enumerate(lines) if _items(line) == [spec.name])
        assert lines[at - 1] == f"# {spec.description}"
        # exactly one blank line above the description, and none for the first item
        assert (lines[at - 2] == "") if at >= 2 else (at == 1)
        assert at < 3 or lines[at - 3] != ""


def test_repeated_saves_leave_the_file_byte_identical(tmp_path):
    # The layout is rebuilt on every write. If a rebuild were not idempotent the
    # generated lines would pile up, so a no-op save must change nothing.
    config.init_config(tmp_path)
    path = config.config_path(tmp_path)
    before = path.read_text()

    for _ in range(3):
        config.save_config(tmp_path, config.load_config(tmp_path))
    assert path.read_text() == before


def test_a_user_comment_below_a_null_valued_key_survives(tmp_path):
    # A dangling `key:` sends every line below it to a slot the loader hangs off
    # the document rather than the key; the layout pass has to collect it.
    config.init_config(tmp_path)
    path = config.config_path(tmp_path)
    path.write_text("projects_dir: docs/projects\nactive_project:\n\n# hand written\n")
    config.save_config(tmp_path, config.load_config(tmp_path))

    assert "# hand written" in path.read_text()


# --- backfilling an older config (REQ-10, REQ-11, REQ-12) ----------------
# A config written before a key existed is completed in place on the next write,
# and says so once. Reads never touch the file.

TWO_KEY_CONFIG = """\
# my own note
projects_dir: specs
active_project: alpha
"""


def _older_config(tmp_path):
    config.init_config(tmp_path)
    path = config.config_path(tmp_path)
    path.write_text(TWO_KEY_CONFIG)
    return path


def test_a_config_missing_keys_is_backfilled_on_the_next_write(tmp_path):
    path = _older_config(tmp_path)
    config.save_config(tmp_path, config.load_config(tmp_path))
    text = path.read_text()

    for spec in config.CONFIG_FIELDS:
        assert f"# {spec.description}" in text
    assert "# autonomy: safe" in text
    assert "# auto_max_passes: 50" in text
    assert "# context_threshold_percent: 25" in text
    # the live keys and the user's comment are untouched
    assert _live_keys(text) == ["projects_dir", "active_project"]
    assert yaml.safe_load(text) == {"projects_dir": "specs", "active_project": "alpha"}
    assert "# my own note" in text


def test_a_backfill_announces_the_keys_it_added_on_one_line(tmp_path, capsys):
    _older_config(tmp_path)
    config.save_config(tmp_path, config.load_config(tmp_path))

    err = capsys.readouterr().err
    assert err.strip().count("\n") == 0  # exactly one line
    for name in ("autonomy", "auto_max_passes", "context_threshold_percent"):
        assert name in err
    assert "projects_dir" not in err  # it was already there


def test_a_write_that_adds_nothing_is_silent(tmp_path, capsys):
    _older_config(tmp_path)
    config.save_config(tmp_path, config.load_config(tmp_path))
    capsys.readouterr()

    config.save_config(tmp_path, config.load_config(tmp_path))
    assert capsys.readouterr().err == ""


def test_creating_a_config_is_not_a_backfill(tmp_path, capsys):
    # `init` writes the whole file; there is nothing to announce as added.
    config.init_config(tmp_path)
    assert capsys.readouterr().err == ""


def test_loading_an_incomplete_config_leaves_the_file_bytes_unchanged(tmp_path):
    path = _older_config(tmp_path)
    before = path.read_bytes()

    config.load_config(tmp_path)
    assert path.read_bytes() == before

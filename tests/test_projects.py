import pytest

from specflo import config, projects
from specflo.errors import SpecfloError


@pytest.fixture
def root(tmp_path):
    config.init_config(tmp_path)
    return tmp_path


@pytest.fixture
def cfg(root):
    return config.load_config(root)


def test_slugify_normalizes_names():
    assert projects.slugify("My Cool Thing") == "my-cool-thing"
    assert projects.slugify("Hello, World!") == "hello-world"
    assert projects.slugify("  spaced__out  ") == "spaced-out"


def test_slugify_rejects_names_with_no_usable_characters():
    with pytest.raises(SpecfloError):
        projects.slugify("!!!")


def test_create_project_writes_artifact_and_returns_project(root, cfg):
    project = projects.create_project(root, cfg, "My Thing", created="2026-06-15")

    project_md = root / "docs" / "projects" / "my-thing" / "project.md"
    assert project_md.is_file()
    assert project.name == "My Thing"
    assert project.slug == "my-thing"
    assert project.phase == "brainstorm"
    assert project.status == "active"
    assert project.created == "2026-06-15"


def test_creating_a_duplicate_project_raises(root, cfg):
    projects.create_project(root, cfg, "My Thing")
    with pytest.raises(SpecfloError):
        projects.create_project(root, cfg, "My Thing")


def test_load_project_reads_back_the_saved_fields(root, cfg):
    projects.create_project(root, cfg, "My Thing", created="2026-06-15")

    loaded = projects.load_project(root, cfg, "my-thing")
    assert loaded.name == "My Thing"
    assert loaded.slug == "my-thing"
    assert loaded.phase == "brainstorm"
    assert loaded.status == "active"
    assert loaded.created == "2026-06-15"


def test_load_missing_project_raises(root, cfg):
    with pytest.raises(SpecfloError):
        projects.load_project(root, cfg, "does-not-exist")


def test_list_projects_returns_all_sorted_by_slug(root, cfg):
    projects.create_project(root, cfg, "Charlie")
    projects.create_project(root, cfg, "Alpha")
    projects.create_project(root, cfg, "Bravo")

    listed = projects.list_projects(root, cfg)
    assert [p.slug for p in listed] == ["alpha", "bravo", "charlie"]


def test_list_projects_is_empty_when_there_are_none(root, cfg):
    assert projects.list_projects(root, cfg) == []


def test_list_projects_ignores_dirs_without_a_project_file(root, cfg):
    projects.create_project(root, cfg, "Alpha")
    (root / cfg.projects_dir / "stray-dir").mkdir()

    listed = projects.list_projects(root, cfg)
    assert [p.slug for p in listed] == ["alpha"]


def test_switch_project_sets_active_and_persists_it(root, cfg):
    projects.create_project(root, cfg, "Alpha")
    projects.create_project(root, cfg, "Bravo")

    switched = projects.switch_project(root, cfg, "alpha")
    assert switched.slug == "alpha"
    assert cfg.active_project == "alpha"
    assert config.load_config(root).active_project == "alpha"


def test_switch_project_accepts_a_name_and_slugifies_it(root, cfg):
    projects.create_project(root, cfg, "My Thing")

    switched = projects.switch_project(root, cfg, "My Thing")
    assert switched.slug == "my-thing"
    assert config.load_config(root).active_project == "my-thing"


def test_switch_to_a_missing_project_raises(root, cfg):
    with pytest.raises(SpecfloError):
        projects.switch_project(root, cfg, "does-not-exist")


def test_advance_project_moves_to_next_phase_and_persists(root, cfg):
    projects.create_project(root, cfg, "My Thing")

    advanced = projects.advance_project(root, cfg, "my-thing")
    assert advanced.phase == "spec"
    # The change is persisted to project.md, not just held in memory.
    assert projects.load_project(root, cfg, "my-thing").phase == "spec"


def test_advance_project_walks_the_full_phase_sequence(root, cfg):
    projects.create_project(root, cfg, "My Thing")  # starts at brainstorm

    phases = [projects.advance_project(root, cfg, "my-thing").phase for _ in range(3)]
    assert phases == ["spec", "plan", "execute"]


def test_advance_project_at_the_final_phase_raises(root, cfg):
    projects.create_project(root, cfg, "My Thing")
    for _ in range(3):
        projects.advance_project(root, cfg, "my-thing")  # now at "execute"

    with pytest.raises(SpecfloError):
        projects.advance_project(root, cfg, "my-thing")


def test_complete_project_flips_status_idempotently(tmp_path):
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing")
    p = projects.complete_project(tmp_path, cfg, "thing")
    assert p.status == projects.COMPLETE_STATUS
    assert projects.load_project(tmp_path, cfg, "thing").status == "complete"
    assert projects.complete_project(tmp_path, cfg, "thing").status == "complete"


def test_shelved_status_constant_is_distinct_from_active_and_complete():
    assert projects.SHELVED_STATUS == "shelved"
    assert projects.SHELVED_STATUS not in (projects.INITIAL_STATUS, projects.COMPLETE_STATUS)


def test_project_round_trips_shelved_status_and_reason(tmp_path):
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing")
    projects.advance_project(tmp_path, cfg, "thing")  # phase -> spec

    p = projects.load_project(tmp_path, cfg, "thing")
    p.status = projects.SHELVED_STATUS
    p.shelved_reason = "not worth it"
    (p.path / projects.PROJECT_FILENAME).write_text(projects._render(p))

    loaded = projects.load_project(tmp_path, cfg, "thing")
    assert loaded.status == "shelved"
    assert loaded.shelved_reason == "not worth it"
    assert loaded.phase == "spec"  # shelving leaves phase untouched


def test_shelved_reason_is_empty_and_unwritten_when_no_reason_given(tmp_path):
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing")

    loaded = projects.load_project(tmp_path, cfg, "thing")
    assert loaded.shelved_reason == ""
    text = (loaded.path / projects.PROJECT_FILENAME).read_text()
    assert "shelved_reason" not in text

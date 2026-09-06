"""``specflo promote <project> --remote <name>``: move a local project into a daemon.

Every file of the project directory is uploaded, the daemon reports the hash
of what it wrote, the client verifies each against the bytes it sent, and
only then is the local directory removed and the project recorded as hosted.
A mismatch aborts before anything local is touched.
"""

import hashlib

import pytest
from typer.testing import CliRunner

from specflo import config, daemon
from specflo.cli import app
from specflo.errors import SpecfloError
from specflo.service import promote
from specflo.service.local import LocalProjectService

runner = CliRunner()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _local_project(root, slug_name="Thing"):
    """A local project with a brainstorm, a spec, a review round and a checkpoint."""
    cfg = config.load_config(root)
    service = LocalProjectService(root, cfg)
    project = service.create_project(slug_name, summary="One line")
    service.start_brainstorm(project.slug)
    service.add_decision(project.slug, "Use one facade", rationale="one seam")
    service.start_spec(project.slug)
    service.add_requirement(project.slug, "Prints help", "a no-arg run exits 0")
    service.start_round(project.slug)
    service.write_checkpoint(project.slug)
    (project.path / "notes.md").write_text("hand-written notes\n")
    cfg.active_project = project.slug
    config.save_config(root, cfg)
    return project


def _snapshot(directory):
    return {p.name: p.read_text() for p in directory.iterdir() if p.is_file()}


# --- the transfer operations --------------------------------------------------


def test_export_and_import_round_trip_every_file_of_a_project(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    config.init_config(source)
    config.init_config(target)
    project = _local_project(source)
    exporter = LocalProjectService(source, config.load_config(source))
    importer = LocalProjectService(target, config.load_config(target))

    files = exporter.export_project("thing")
    hashes = importer.import_project("thing", files)

    assert files == _snapshot(project.path)
    assert set(files) >= {"project.md", "brainstorm.md", "spec.md", "review-1.md", "checkpoint.md", "notes.md"}
    assert hashes == {name: _sha256(text) for name, text in files.items()}
    assert _snapshot(target / "docs" / "projects" / "thing") == files
    assert importer.load_project("thing").summary == "One line"


def test_import_refuses_an_existing_project_and_a_bad_filename(tmp_path):
    config.init_config(tmp_path)
    service = LocalProjectService(tmp_path, config.load_config(tmp_path))
    service.create_project("Taken")

    with pytest.raises(SpecfloError, match="already exists"):
        service.import_project("taken", {"project.md": "x"})
    for name in ("../escape.md", "sub/dir.md", "", ".hidden"):
        with pytest.raises(SpecfloError, match="file name"):
            service.import_project("fresh", {name: "x"})
    assert not (tmp_path / "docs" / "projects" / "fresh").exists()
    with pytest.raises(SpecfloError, match="project.md"):
        service.import_project("fresh", {"brainstorm.md": "x"})


def test_export_refuses_an_unknown_project(tmp_path):
    config.init_config(tmp_path)
    service = LocalProjectService(tmp_path, config.load_config(tmp_path))

    with pytest.raises(SpecfloError, match="No project 'nope'"):
        service.export_project("nope")


# --- the command ------------------------------------------------------------


@pytest.fixture
def checkout(tmp_path, monkeypatch, live_daemon):
    root = tmp_path / "checkout"
    root.mkdir()
    config.init_config(root)
    monkeypatch.chdir(root)
    done = runner.invoke(
        app, ["remote", "add", "home", live_daemon["url"], "--token", live_daemon["token"]]
    )
    assert done.exit_code == 0, done.output
    return root


def test_promote_moves_the_project_into_the_daemon_and_records_it_hosted(checkout, live_daemon):
    project = _local_project(checkout)
    before = _snapshot(project.path)
    brainstorm_before = runner.invoke(app, ["doc", "show", "brainstorm"]).output

    result = runner.invoke(app, ["promote", "thing", "--remote", "home"])

    assert result.exit_code == 0, result.output
    assert result.output == (
        f"Promoted 'thing' to remote 'home': {len(before)} files verified,"
        " the local copy removed.\n"
    )
    assert not project.path.exists()
    assert config.hosted_projects(checkout) == {"thing": "home"}
    hosted = live_daemon["root"] / daemon.PROJECTS_DIRNAME / "thing"
    assert _snapshot(hosted) == before
    listing = runner.invoke(app, ["list"]).output
    assert "* thing  (brainstorm)  [hosted: home]" in listing.splitlines()
    assert runner.invoke(app, ["doc", "show", "brainstorm"]).output == brainstorm_before
    status = runner.invoke(app, ["status"])
    assert status.exit_code == 0 and "Project: Thing (thing)" in status.output


def test_promote_accepts_the_project_name_as_well_as_the_slug(checkout):
    _local_project(checkout, "My Thing")

    result = runner.invoke(app, ["promote", "My Thing", "--remote", "home"])

    assert result.exit_code == 0, result.output
    assert config.hosted_projects(checkout) == {"my-thing": "home"}


def test_a_hash_mismatch_aborts_before_anything_local_changes(checkout, monkeypatch):
    project = _local_project(checkout)
    before = _snapshot(project.path)

    class Corrupting:
        def import_project(self, slug, files):
            hashes = {name: _sha256(text) for name, text in files.items()}
            hashes["spec.md"] = _sha256("something else")
            return hashes

    monkeypatch.setattr(promote, "remote_service", lambda root, name: Corrupting())

    result = runner.invoke(app, ["promote", "thing", "--remote", "home"])

    assert result.exit_code == 1
    assert "spec.md" in result.stderr and "mismatch" in result.stderr
    assert _snapshot(project.path) == before
    assert config.hosted_projects(checkout) == {}


def test_promote_refuses_what_it_cannot_move(checkout, live_daemon):
    assert runner.invoke(app, ["promote", "nope", "--remote", "home"]).exit_code == 1
    assert "No project 'nope'" in runner.invoke(app, ["promote", "nope", "--remote", "home"]).stderr

    project = _local_project(checkout)
    unknown = runner.invoke(app, ["promote", "thing", "--remote", "nowhere"])
    assert unknown.exit_code == 1 and "No remote 'nowhere'" in unknown.stderr
    assert project.path.is_dir()

    taken = runner.invoke(app, ["new", "Other", "--remote", "home"])
    assert taken.exit_code == 0, taken.output
    already = runner.invoke(app, ["promote", "other", "--remote", "home"])
    assert already.exit_code == 1 and "hosted on remote 'home'" in already.stderr

    # A project the daemon already holds under that slug is refused there,
    # and the local copy stays.
    (live_daemon["root"] / daemon.PROJECTS_DIRNAME / "thing").mkdir()
    (live_daemon["root"] / daemon.PROJECTS_DIRNAME / "thing" / "project.md").write_text("x")
    clash = runner.invoke(app, ["promote", "thing", "--remote", "home"])
    assert clash.exit_code == 1 and "already exists" in clash.stderr
    assert project.path.is_dir()
    assert config.hosted_projects(checkout) == {"other": "home"}


# --- what may travel ------------------------------------------------------------


def test_export_refuses_anything_but_plain_utf8_files(tmp_path):
    config.init_config(tmp_path)
    project = _local_project(tmp_path)
    service = LocalProjectService(tmp_path, config.load_config(tmp_path))
    before = _snapshot(project.path)

    (project.path / "attachments").mkdir()
    with pytest.raises(SpecfloError, match="'attachments', which is not a plain file"):
        service.export_project("thing")
    (project.path / "attachments").rmdir()

    (project.path / "alias.md").symlink_to(project.path / "notes.md")
    with pytest.raises(SpecfloError, match="'alias.md', which is not a plain file"):
        service.export_project("thing")
    (project.path / "alias.md").unlink()

    (project.path / "diagram.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe")
    with pytest.raises(SpecfloError, match="'diagram.png', which is not UTF-8 text"):
        service.export_project("thing")
    (project.path / "diagram.png").unlink()

    assert service.export_project("thing") == before


def test_promote_refuses_before_sending_and_leaves_the_project_in_place(tmp_path, monkeypatch, live_daemon):
    config.init_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    project = _local_project(tmp_path)
    (project.path / "attachments").mkdir()
    (project.path / "attachments" / "sketch.txt").write_text("keep me\n")
    runner.invoke(app, ["remote", "add", "home", live_daemon["url"], "--token", live_daemon["token"]])
    before = _snapshot(project.path)

    result = runner.invoke(app, ["promote", "thing", "--remote", "home"])

    assert result.exit_code == 1
    assert "'attachments', which is not a plain file" in result.stderr
    assert _snapshot(project.path) == before
    assert (project.path / "attachments" / "sketch.txt").read_text() == "keep me\n"
    assert config.hosting_remote(tmp_path, "thing") is None
    assert not (live_daemon["root"] / daemon.PROJECTS_DIRNAME / "thing").exists()


def test_a_failed_import_leaves_nothing_behind_and_the_retry_is_not_refused(tmp_path, monkeypatch):
    from pathlib import Path as _Path

    config.init_config(tmp_path)
    service = LocalProjectService(tmp_path, config.load_config(tmp_path))
    files = {"project.md": "---\nname: Thing\nslug: thing\n---\n", "brainstorm.md": "# b\n", "spec.md": "# s\n"}
    real = _Path.write_bytes
    written = []

    def failing(self, data):
        written.append(self.name)
        if len(written) == 2:
            raise OSError("disk full")
        return real(self, data)

    monkeypatch.setattr(_Path, "write_bytes", failing)
    with pytest.raises(OSError, match="disk full"):
        service.import_project("thing", files)
    monkeypatch.setattr(_Path, "write_bytes", real)

    projects_dir = tmp_path / "docs" / "projects"
    assert not (projects_dir / "thing").exists()
    assert [p.name for p in projects_dir.iterdir()] == []

    hashes = service.import_project("thing", files)

    assert set(hashes) == set(files)
    assert hashes["spec.md"] == hashlib.sha256((projects_dir / "thing" / "spec.md").read_bytes()).hexdigest()
    assert [p.name for p in projects_dir.iterdir()] == ["thing"]

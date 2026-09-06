"""``specflo serve``: the daemon process, the root it owns, and its health.

The daemon owns a root of its own: a projects directory holding the only
copy of every hosted project's artifacts, and the state store. ``serve``
prepares that root on first start and answers on a health endpoint, bound
to loopback unless told otherwise.
"""

import sqlite3
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from specflo import config, daemon
from specflo.cli import app

runner = CliRunner()
REPO_ROOT = Path(__file__).resolve().parent.parent


# --- the daemon root ------------------------------------------------------


def test_prepare_root_creates_the_projects_dir_and_the_state_store(tmp_path):
    root = tmp_path / "daemon"

    assert daemon.prepare_root(root) == root

    assert (root / daemon.PROJECTS_DIRNAME).is_dir()
    store = root / daemon.STATE_STORE_FILENAME
    assert store.is_file()
    assert sqlite3.connect(store).execute("pragma user_version").fetchone() == (0,)
    # The root is a specflo root of its own, so the local service can run on it.
    assert config.find_root(root) == root
    assert config.load_config(root).projects_dir == daemon.PROJECTS_DIRNAME


def test_prepare_root_leaves_an_existing_root_untouched(tmp_path):
    root = tmp_path / "daemon"
    daemon.prepare_root(root)
    project = root / daemon.PROJECTS_DIRNAME / "thing"
    project.mkdir()
    (project / "project.md").write_text("kept")
    config_text = config.config_path(root).read_text()

    daemon.prepare_root(root)

    assert (project / "project.md").read_text() == "kept"
    assert config.config_path(root).read_text() == config_text


# --- the application ------------------------------------------------------


def test_health_endpoint_answers_200_and_prepares_the_root(tmp_path):
    from fastapi.testclient import TestClient

    from specflo.daemon.app import create_app

    root = tmp_path / "daemon"
    client = TestClient(create_app(root))

    response = client.get(daemon.HEALTH_PATH)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert (root / daemon.PROJECTS_DIRNAME).is_dir()
    assert (root / daemon.STATE_STORE_FILENAME).is_file()


# --- the command ----------------------------------------------------------


@pytest.fixture
def served(monkeypatch):
    """Capture what ``serve`` hands to uvicorn instead of binding a socket."""
    import uvicorn

    captured = {}

    def run(application, **kwargs):
        captured["app"] = application
        captured.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", run)
    return captured


def test_serve_binds_loopback_on_the_default_port(tmp_path, served):
    root = tmp_path / "daemon"

    result = runner.invoke(app, ["serve", "--root", str(root)])

    assert result.exit_code == 0, result.output
    assert served["host"] == "127.0.0.1"
    assert served["port"] == daemon.DEFAULT_PORT
    assert served["app"].state.root == root
    assert (root / daemon.PROJECTS_DIRNAME).is_dir()
    assert f"http://127.0.0.1:{daemon.DEFAULT_PORT}" in result.output


def test_serve_honours_bind_and_port(tmp_path, served):
    root = tmp_path / "daemon"

    result = runner.invoke(
        app, ["serve", "--root", str(root), "--bind", "0.0.0.0", "--port", "9009"]
    )

    assert result.exit_code == 0, result.output
    assert served["host"] == "0.0.0.0"
    assert served["port"] == 9009


def test_serve_requires_a_root(tmp_path):
    result = runner.invoke(app, ["serve"])

    assert result.exit_code != 0
    assert "--root" in result.output


def test_serve_without_the_extra_names_how_to_install_it(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "uvicorn", None)

    result = runner.invoke(app, ["serve", "--root", str(tmp_path / "daemon")])

    assert result.exit_code == 1
    assert "specflo[serve]" in result.output


def test_serve_is_listed_in_the_top_level_help():
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "serve" in result.output


# --- the declared dependencies --------------------------------------------


def test_pyproject_declares_the_serve_extra_and_the_client_dependency():
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    core = [d.split(">")[0].split("=")[0] for d in data["project"]["dependencies"]]
    serve = [d.split(">")[0].split("=")[0] for d in data["project"]["optional-dependencies"]["serve"]]

    assert "httpx" in core
    assert "fastapi" in serve and "uvicorn" in serve
    assert "fastapi" not in core and "uvicorn" not in core


def test_ordinary_commands_import_none_of_the_web_stack():
    # A plain `pip install specflo` has no fastapi, jinja2, uvicorn or
    # starlette; every command but `serve` must still load. The daemon's
    # application, routes and web modules are the ones that need the extra,
    # so importing the CLI must not pull them in.
    code = textwrap.dedent(
        """
        import sys
        for name in ("fastapi", "jinja2", "uvicorn", "starlette"):
            sys.modules[name] = None
        import specflo.cli
        print(" ".join(sorted(n for n in sys.modules if n.startswith("specflo.daemon"))))
        """
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)

    assert done.returncode == 0, done.stderr
    loaded = set(done.stdout.split())
    assert "specflo.daemon" in loaded
    assert not {"specflo.daemon.app", "specflo.daemon.routes", "specflo.daemon.web"} & loaded

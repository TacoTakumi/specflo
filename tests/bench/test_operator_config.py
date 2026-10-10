"""Runs read the operator's configs from the bench home, not the shipped templates."""

import json
import stat
from pathlib import Path

import pytest
import yaml

from specflo.bench import HOME_ENV, arms, cc_continue, launch_cc, launch_pi

MINE = "my-own-entry"


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A bench home whose arm config and pi model specs name only an entry of its own."""
    home = tmp_path / "home"
    home.mkdir()
    config = yaml.safe_load(arms.DEFAULT_CONFIG.read_text())
    config["entries"] = {MINE: {"engine": "llama.cpp"}}
    (home / "arms.yaml").write_text(yaml.safe_dump(config))
    models = json.loads((launch_pi.FROZEN_DIR / launch_pi.MODELS_FILE).read_text())
    provider = models["providers"]["llama-swap"]
    provider["models"] = [{**provider["models"][0], "id": MINE, "contextWindow": 32768}]
    (home / launch_pi.HOME_MODELS_FILE).write_text(json.dumps(models))
    monkeypatch.setenv(HOME_ENV, str(home))
    return home


def _fake(tmp_path: Path, name: str, body: bytes) -> str:
    path = tmp_path / "bin" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def test_the_arm_config_comes_from_the_home(home):
    assert list(arms.load_config().entries) == [MINE]


def test_without_a_home_the_shipped_template_is_read(monkeypatch):
    monkeypatch.delenv(HOME_ENV, raising=False)
    assert MINE not in arms.load_config().entries
    assert arms.load_config().entries


def test_the_pi_launch_takes_the_entry_from_the_home_models(home, tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    pi = _fake(tmp_path, "pi", b"#!/bin/sh\necho 1.2.3\n")

    launched = launch_pi.build_launch(MINE, workdir=workdir, run_dir=tmp_path / "run", pi_bin=pi,
                                      base_env={"PATH": "/usr/bin:/bin"})

    copied = json.loads((launched.agent_dir / launch_pi.MODELS_FILE).read_text())
    assert [m["id"] for m in copied["providers"]["llama-swap"]["models"]] == [MINE]


def test_the_claude_launch_accepts_an_entry_of_the_home(home, tmp_path, monkeypatch):
    monkeypatch.setattr(launch_cc, "claude_version", lambda _bin: "2.1.300")
    workdir = tmp_path / "work"
    workdir.mkdir()
    claude = _fake(tmp_path, "claude", b"\x7fELF stand-in")

    with launch_cc.build_launch(MINE, workdir=workdir, run_dir=tmp_path / "run", claude_bin=claude) as launched:
        assert MINE in launched.argv


def test_the_context_window_comes_from_the_home_models(home):
    assert cc_continue.context_window(MINE) == 32768

"""The built wheel carries the bench and the data it runs on."""

import subprocess
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

SHIPPED = (
    "specflo/bench/run.py",
    "specflo/bench/cmd_graph.py",
    "specflo/bench/data/arms.yaml",
    "specflo/bench/data/matrices.yaml",
    "specflo/bench/data/heldout.tar.gz",
    "specflo/bench/data/fixture/pyproject.toml",
    "specflo/bench/data/fixture/tinytodo/__init__.py",
    "specflo/bench/data/fixture-levels/quick.md",
    "specflo/bench/data/fixture-levels/fast.md",
    "specflo/bench/data/fixture-levels/full.md",
    "specflo/bench/data/configs/pi/frozen.json",
    "specflo/bench/data/configs/pi/models.json",
    "specflo/bench/data/configs/claude/frozen.json",
)


@pytest.fixture(scope="module")
def wheel(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("wheel")
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(out), str(REPO)],
        check=True, capture_output=True, text=True,
    )
    (built,) = out.glob("specflo-*.whl")
    return built


@pytest.mark.parametrize("name", SHIPPED)
def test_the_wheel_ships_the_bench_file(wheel, name):
    with zipfile.ZipFile(wheel) as archive:
        assert name in archive.namelist()


def test_the_wheel_declares_matplotlib_under_the_bench_extra(wheel):
    with zipfile.ZipFile(wheel) as archive:
        (metadata,) = [n for n in archive.namelist() if n.endswith(".dist-info/METADATA")]
        text = archive.read(metadata).decode()
    assert "Provides-Extra: bench" in text
    assert any(
        line.startswith("Requires-Dist: matplotlib") and "extra == 'bench'" in line
        for line in text.splitlines()
    )

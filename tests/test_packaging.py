"""The built wheel and sdist carry the repo-root skills as agentsquire Root B.

The 7 workflow skills live at repo-root ``skills/`` (the single source of truth)
and ride into the wheel via one hatchling force-include line, landing at
``specflo/_repo_skills/<name>/`` -- agentsquire's Root B package-data location,
NOT ``specflo/skills`` (which would duplicate Root A and raise
``DuplicateSkillError``). The sdist carries the source ``skills/`` tree, so a
wheel built *from the sdist* reproduces the same Root B; this module builds
exactly that way, so its wheel assertions prove both artifacts at once.

Skill names are hardcoded on purpose: deriving them from disk would let a
dropped skill silently shrink the expected set. If a skill goes missing from the
build (or from the repo), its parametrized case fails loudly.
"""

import importlib.util
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# The 7 workflow skills, hardcoded so a dropped skill fails the build test.
SKILL_NAMES = ["auto", "brainstorm", "execute", "plan", "research", "shelve", "spec"]


def _load_hatch_build():
    """Load the repo-root hatch_build.py by path.

    It lives at the repo root (not on sys.path), and its hatchling import is
    guarded so it stays importable here even though the test venv has no
    hatchling -- only its completeness-check function is exercised.
    """
    spec = importlib.util.spec_from_file_location(
        "hatch_build", REPO_ROOT / "hatch_build.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hatch_build = _load_hatch_build()


def _make_skills(root: Path, names):
    """Create a skills/ tree with a minimal SKILL.md per name."""
    for name in names:
        skill_dir = root / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\n---\n")
    return root


def _run(cmd, cwd):
    """Run a build command, surfacing stdout/stderr if it fails."""
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    return proc


@pytest.fixture(scope="module")
def dist(tmp_path_factory):
    """Build the sdist, then build the wheel FROM the extracted sdist.

    Building the wheel from the sdist (rather than straight from the checkout)
    proves the sdist is self-sufficient: if it failed to carry ``skills/``, the
    force-include would find nothing and the wheel would lack Root B.
    """
    out = tmp_path_factory.mktemp("dist")
    _run(["uv", "build", "--sdist", "--out-dir", str(out)], cwd=REPO_ROOT)
    sdist = next(out.glob("*.tar.gz"))

    extracted = tmp_path_factory.mktemp("sdist-src")
    with tarfile.open(sdist) as tf:
        tf.extractall(extracted, filter="data")
    sdist_root = next(p for p in extracted.iterdir() if p.is_dir())

    wheel_out = tmp_path_factory.mktemp("wheel")
    _run(["uv", "build", "--wheel", "--out-dir", str(wheel_out)], cwd=sdist_root)
    wheel = next(wheel_out.glob("*.whl"))

    return {
        "sdist": sdist,
        "sdist_root": sdist_root.name,
        "sdist_names": set(tarfile.open(sdist).getnames()),
        "wheel": wheel,
        "wheel_names": set(zipfile.ZipFile(wheel).namelist()),
    }


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_wheel_ships_repo_skill_as_root_b(dist, name):
    # Built from the sdist, so this proves both the wheel and the sdist.
    assert f"specflo/_repo_skills/{name}/SKILL.md" in dist["wheel_names"]


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_sdist_carries_source_skill(dist, name):
    assert f"{dist['sdist_root']}/skills/{name}/SKILL.md" in dist["sdist_names"]


def test_wheel_has_no_duplicate_root_a_skills(dist):
    # Root A ships only as an empty marker (specflo/skills/.gitkeep); no skill
    # DIR may ship there -- a specflo/skills/<name>/SKILL.md would collide with
    # Root B on the same name and raise DuplicateSkillError at resolution time.
    stray = [
        n
        for n in dist["wheel_names"]
        if n.startswith("specflo/skills/") and n.endswith("/SKILL.md")
    ]
    assert not stray, f"wheel unexpectedly ships Root A skills: {stray}"


def test_wheel_ships_empty_root_a_marker(dist):
    assert "specflo/skills/.gitkeep" in dist["wheel_names"]


# The pi extension ships as ordinary package data under src/specflo/extension/
# -- no force-include needed, unlike the repo-root skills. Hardcoded like the
# skill names so a dropped file fails loudly instead of shrinking the set.
EXTENSION_FILES = ["package.json", "src/index.ts"]


@pytest.mark.parametrize("relpath", EXTENSION_FILES)
def test_wheel_ships_the_pi_extension_as_package_data(dist, relpath):
    # Built from the sdist, so this proves both artifacts carry the extension.
    assert f"specflo/extension/{relpath}" in dist["wheel_names"]


@pytest.mark.parametrize("relpath", EXTENSION_FILES)
def test_sdist_carries_the_pi_extension_source(dist, relpath):
    assert f"{dist['sdist_root']}/src/specflo/extension/{relpath}" in dist["sdist_names"]


def test_wheel_requires_agentsquire_floor(dist):
    with zipfile.ZipFile(dist["wheel"]) as zf:
        meta_name = next(n for n in zf.namelist() if n.endswith(".dist-info/METADATA"))
        requires = [
            line
            for line in zf.read(meta_name).decode().splitlines()
            if line.startswith("Requires-Dist:")
        ]
    assert "Requires-Dist: agentsquire>=0.5.0" in requires


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_repo_root_skill_is_present_on_disk(name):
    assert (REPO_ROOT / "skills" / name / "SKILL.md").is_file()


def test_hook_check_passes_for_the_real_repo_skills():
    found = hatch_build.check_skills_complete(REPO_ROOT / "skills")
    assert set(SKILL_NAMES) <= set(found)


@pytest.mark.parametrize("dropped", SKILL_NAMES)
def test_hook_check_raises_when_a_skill_is_removed(tmp_path, dropped):
    _make_skills(tmp_path, [n for n in SKILL_NAMES if n != dropped])
    with pytest.raises(ValueError) as exc:
        hatch_build.check_skills_complete(tmp_path)
    assert dropped in str(exc.value)


def test_hook_check_raises_when_a_skill_lacks_skill_md(tmp_path):
    _make_skills(tmp_path, SKILL_NAMES)
    (tmp_path / "plan" / "SKILL.md").unlink()  # dir stays, marker gone
    with pytest.raises(ValueError) as exc:
        hatch_build.check_skills_complete(tmp_path)
    assert "plan" in str(exc.value)


def test_hook_check_raises_when_skills_dir_missing(tmp_path):
    with pytest.raises(ValueError):
        hatch_build.check_skills_complete(tmp_path / "does-not-exist")


def test_build_with_hook_wired_still_ships_all_skills(dist):
    # dist builds with the completeness hook wired in pyproject; a successful
    # build that still carries every skill proves the hook guards the build
    # without blocking the normal all-6-present case.
    for name in SKILL_NAMES:
        assert f"specflo/_repo_skills/{name}/SKILL.md" in dist["wheel_names"]


def test_no_committed_src_skill_trees():
    # No skill *tree* may be committed under src/: Root B (specflo/_repo_skills)
    # is built, not stored, and Root A (specflo/skills) is a deliberately empty
    # marker directory -- the 6 skills live only at repo-root skills/. An empty
    # Root A marker file (e.g. .gitkeep) is allowed; a committed skill directory
    # under either root (any .../SKILL.md) is not.
    out = subprocess.run(
        ["git", "ls-files", "src/specflo/skills", "src/specflo/_repo_skills"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    committed = out.stdout.split()
    # _repo_skills must not be committed at all.
    stored_root_b = [p for p in committed if p.startswith("src/specflo/_repo_skills")]
    assert not stored_root_b, f"Root B must be force-included, not stored: {stored_root_b}"
    # Root A may exist only as marker files, never as skill directories.
    stored_skills = [p for p in committed if p.endswith("/SKILL.md")]
    assert not stored_skills, f"a skill tree is committed under src/: {stored_skills}"


def test_root_a_is_committed_empty():
    # The empty Root A marker is present so agentsquire's zero-arg union can
    # enumerate Root A without a FileNotFoundError, but carries no skills.
    out = subprocess.run(
        ["git", "ls-files", "src/specflo/skills"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    committed = out.stdout.split()
    assert "src/specflo/skills/.gitkeep" in committed
    assert all(not p.endswith("/SKILL.md") for p in committed)

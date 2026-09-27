"""The README's by-hand quickstart runs as written and completes its project."""

import os
import re
import subprocess
import sys
from pathlib import Path

README = Path(__file__).resolve().parents[1] / "README.md"
HEADING = "### Your first project by hand"


def _block():
    text = README.read_text()
    assert HEADING in text
    after = text.split(HEADING, 1)[1]
    match = re.search(r"```bash\n(.*?)```", after, re.DOTALL)
    assert match, "no bash block under the heading"
    return match.group(1)


def test_the_by_hand_block_completes_its_project(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith(("SPECFLO_", "GIT_"))
    }
    env |= {
        # The specflo of this checkout, not whatever is installed on the machine.
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{env.get('PATH', '')}",
        "AGENTSQUIRE_HOME": str(tmp_path / "home"),
        "AGENTSQUIRE_PROJECT": str(repo),
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    subprocess.run(["git", "init", "-q"], cwd=repo, env=env, check=True)
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", _block()],
        cwd=repo, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Completed project" in result.stdout
    [project] = (repo / "docs" / "projects").glob("*/project.md")
    assert "status: complete" in project.read_text()

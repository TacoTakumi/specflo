"""The README's first-project block runs as written and starts a full-level project."""

import os
import re
import subprocess
import sys
from pathlib import Path

README = Path(__file__).resolve().parents[1] / "README.md"
HEADING = "### Your first project"


def _block():
    text = README.read_text()
    assert HEADING in text
    after = text.split(HEADING, 1)[1]
    match = re.search(r"```bash\n(.*?)```", after, re.DOTALL)
    assert match, "no bash block under the heading"
    return match.group(1)


def test_the_first_project_block_starts_a_project(tmp_path):
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
    # The Quick start setup block runs init before this one.
    subprocess.run(["specflo", "init"], cwd=repo, env=env, check=True,
                   capture_output=True)
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", _block()],
        cwd=repo, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    [project] = (repo / "docs" / "projects").glob("*/project.md")
    text = project.read_text()
    assert "phase: brainstorm" in text
    assert "status: active" in text
    assert "level: full" in text

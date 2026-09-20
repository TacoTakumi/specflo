"""Agent definitions: a role as a harness-neutral markdown file.

The front matter names what the role may use and what it needs from a member;
the body is the system prompt. A definition that cannot be trusted as written
is refused with an error naming the file and the field at fault.
"""

import os
import subprocess
import sys

import pytest

from specflo.errors import SpecfloError
from specflo.pool import definitions

from .odd_entries import KINDS, make_odd, opened, within

FULL = """\
---
role: Rebases the work branch and reports conflicts
tools: [read, bash, edit]
skills: [model-update-check]
deny: [git push, rm -rf]
env: [GIT_AUTHOR_NAME, GIT_AUTHOR_EMAIL]
credentials: [github-read]
needs: [strong-model, long-context]
egress: open
project_context: true
---

You are the rebaser.

Never push. Report each conflict with its file.
"""


def _write(tmp_path, text, name="rebaser.md"):
    path = tmp_path / name
    path.write_text(text)
    return path


def _refused(path):
    with pytest.raises(definitions.DefinitionError) as excinfo:
        definitions.load_definition(path)
    return excinfo.value


# --- loading -------------------------------------------------------------


def test_a_definition_with_every_field_loads_its_values(tmp_path):
    path = _write(tmp_path, FULL)

    definition = definitions.load_definition(path)

    assert definition.name == "rebaser"
    assert definition.role == "Rebases the work branch and reports conflicts"
    assert definition.tools == ("read", "bash", "edit")
    assert definition.skills == ("model-update-check",)
    assert definition.deny == ("git push", "rm -rf")
    assert definition.env == ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL")
    assert definition.credentials == ("github-read",)
    assert definition.needs == ("strong-model", "long-context")
    assert definition.egress == "open"
    assert definition.project_context is True


def test_the_body_is_the_system_prompt(tmp_path):
    path = _write(tmp_path, FULL)

    definition = definitions.load_definition(path)

    assert definition.prompt == (
        "You are the rebaser.\n\nNever push. Report each conflict with its file."
    )


def test_a_rule_line_in_the_body_stays_in_the_prompt(tmp_path):
    path = _write(tmp_path, "---\nrole: Writer\n---\nFirst part.\n\n---\n\nSecond part.\n")

    definition = definitions.load_definition(path)

    assert definition.prompt == "First part.\n\n---\n\nSecond part."


def test_only_the_role_is_required_and_the_rest_default_to_the_safe_side(tmp_path):
    path = _write(tmp_path, "---\nrole: General worker\n---\nDo the task.\n", "worker.md")

    definition = definitions.load_definition(path)

    assert definition.name == "worker"
    assert definition.tools == ()
    assert definition.skills == ()
    assert definition.deny == ()
    assert definition.env == ()
    assert definition.credentials == ()
    assert definition.needs == ()
    # Prompts leave the host only to a provider that does not keep them,
    # unless the definition opts in to more.
    assert definition.egress == "no-train"
    assert definition.project_context is False


def test_every_known_egress_class_is_accepted(tmp_path):
    for egress in ("local", "no-train", "open"):
        path = _write(tmp_path, f"---\nrole: Worker\negress: {egress}\n---\nDo the task.\n")
        assert definitions.load_definition(path).egress == egress


# --- refusals ------------------------------------------------------------


def test_an_empty_body_is_refused_naming_the_file_and_the_field(tmp_path):
    path = _write(tmp_path, "---\nrole: Worker\n---\n\n   \n")

    error = _refused(path)

    assert error.path == path
    assert error.field == "body"
    assert str(path) in str(error)
    assert "body" in str(error)


def test_an_unknown_key_is_refused_naming_the_file_and_the_key(tmp_path):
    path = _write(tmp_path, "---\nrole: Worker\nschedule: hourly\n---\nDo the task.\n")

    error = _refused(path)

    assert error.field == "schedule"
    assert str(path) in str(error)
    assert "schedule" in str(error)


def test_an_unknown_egress_class_is_refused_naming_the_file_and_the_field(tmp_path):
    path = _write(tmp_path, "---\nrole: Worker\negress: public\n---\nDo the task.\n")

    error = _refused(path)

    assert error.field == "egress"
    assert str(path) in str(error)
    assert "egress" in str(error)
    assert "public" in str(error)
    for known in ("local", "no-train", "open"):
        assert known in str(error)


def test_a_missing_role_is_refused(tmp_path):
    path = _write(tmp_path, "---\ntools: [read]\n---\nDo the task.\n")

    error = _refused(path)

    assert error.field == "role"
    assert str(path) in str(error)


def test_a_list_field_given_as_a_single_string_is_refused(tmp_path):
    path = _write(tmp_path, "---\nrole: Worker\ntools: read\n---\nDo the task.\n")

    error = _refused(path)

    assert error.field == "tools"
    assert str(path) in str(error)


def test_a_project_context_flag_that_is_not_a_boolean_is_refused(tmp_path):
    path = _write(tmp_path, "---\nrole: Worker\nproject_context: maybe\n---\nDo the task.\n")

    assert _refused(path).field == "project_context"


def test_a_file_without_front_matter_is_refused_naming_the_file(tmp_path):
    path = _write(tmp_path, "Just a prompt, no front matter.\n")

    error = _refused(path)

    assert error.field == "front matter"
    assert str(path) in str(error)


def test_front_matter_that_is_not_valid_yaml_is_refused_naming_the_file(tmp_path):
    path = _write(tmp_path, "---\nrole: [unclosed\n---\nDo the task.\n")

    error = _refused(path)

    assert error.field == "front matter"
    assert str(path) in str(error)


def test_a_missing_file_is_refused_naming_the_file(tmp_path):
    path = tmp_path / "absent.md"

    error = _refused(path)

    assert str(path) in str(error)


def test_a_file_that_is_not_utf8_is_refused_naming_the_file(tmp_path):
    path = tmp_path / "rebaser.md"
    path.write_bytes(b"---\nrole: Caf\xe9 worker\n---\nDo the task.\n")

    error = _refused(path)

    assert error.field == "file"
    assert str(path) in str(error) and "not UTF-8" in str(error)


@pytest.mark.parametrize("kind", KINDS)
def test_an_entry_that_is_not_a_regular_file_is_refused_and_is_never_opened(
    tmp_path, monkeypatch, kind
):
    path = make_odd(tmp_path / "rebaser.md", kind)
    paths = opened(monkeypatch)

    # A named pipe holds its reader for ever, so the load has a limit.
    error = within(_refused, path, pipes=[path])

    assert error.field == "file"
    assert str(path) in str(error) and "not a regular file" in str(error)
    assert str(path) not in paths


def test_a_symlink_to_a_regular_file_loads_as_the_file_does(tmp_path):
    real = _write(tmp_path, FULL, name="kept-elsewhere.txt")
    path = tmp_path / "rebaser.md"
    path.symlink_to(real)

    d = within(definitions.load_definition, path)

    assert (d.name, d.role) == ("rebaser", "Rebases the work branch and reports conflicts")


def test_a_definition_is_read_as_utf8_whatever_the_locale_says(tmp_path):
    path = tmp_path / "rebaser.md"
    path.write_bytes(b"---\nrole: Worker\n---\nAnswer \xe2\x80\x9cdone\xe2\x80\x9d.\n")
    code = (
        "import sys; from pathlib import Path; from specflo.pool import definitions; "
        "print(ascii(definitions.load_definition(Path(sys.argv[1])).prompt))"
    )
    # A daemon started by a service manager often has no locale at all.
    env = {**os.environ, "LC_ALL": "C", "PYTHONCOERCECLOCALE": "0", "PYTHONUTF8": "0"}

    done = subprocess.run(
        [sys.executable, "-c", code, str(path)], capture_output=True, text=True, env=env
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == ascii("Answer \u201cdone\u201d.")


def test_a_refusal_is_a_user_facing_error(tmp_path):
    path = _write(tmp_path, "---\nrole: Worker\n---\n")

    with pytest.raises(SpecfloError):
        definitions.load_definition(path)

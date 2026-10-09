import pytest

from tinytodo.cli import main


@pytest.fixture
def run(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("TINYTODO_FILE", raising=False)
    path = tmp_path / "todo.json"

    def _run(*args):
        code = main(["--file", str(path), *args])
        out, err = capsys.readouterr()
        return code, out, err

    return _run


def test_add_then_list(run):
    assert run("add", "Buy milk") == (0, "Added task 1: Buy milk\n", "")
    run("add", "Pay rent", "--priority", "high")
    run("add", "Water plants", "--priority", "low")
    code, out, _ = run("list")
    assert code == 0
    assert out == "[ ] 1 Buy milk\n[ ] 2 Pay rent (high)\n[ ] 3 Water plants (low)\n"


def test_done_hides_task_unless_all(run):
    run("add", "Buy milk")
    run("add", "Pay rent")
    assert run("done", "1") == (0, "Completed task 1: Buy milk\n", "")
    assert run("list")[1] == "[ ] 2 Pay rent\n"
    assert run("list", "--all")[1] == "[x] 1 Buy milk\n[ ] 2 Pay rent\n"


def test_remove(run):
    run("add", "Buy milk")
    assert run("remove", "1") == (0, "Removed task 1: Buy milk\n", "")
    assert run("list", "--all")[1] == ""


def test_unknown_id_is_an_error(run):
    assert run("done", "7") == (1, "", "error: no task with id 7\n")
    assert run("remove", "7") == (1, "", "error: no task with id 7\n")


def test_empty_title_is_an_error(run):
    assert run("add", "   ") == (1, "", "error: title must not be empty\n")


def test_bad_priority_is_a_usage_error(run):
    with pytest.raises(SystemExit) as exc:
        run("add", "Buy milk", "--priority", "urgent")
    assert exc.value.code == 2


def test_env_var_picks_the_file(tmp_path, capsys, monkeypatch):
    path = tmp_path / "other.json"
    monkeypatch.setenv("TINYTODO_FILE", str(path))
    assert main(["add", "Buy milk"]) == 0
    assert path.exists()


def test_default_file_is_in_the_current_directory(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("TINYTODO_FILE", raising=False)
    monkeypatch.chdir(tmp_path)
    assert main(["add", "Buy milk"]) == 0
    assert (tmp_path / "todo.json").exists()

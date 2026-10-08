import pytest

from tinytodo.model import Task


def test_defaults():
    task = Task(id=1, title="Buy milk")
    assert task.priority == "normal"
    assert task.done is False


def test_title_is_stripped():
    assert Task(id=1, title="  Buy milk \n").title == "Buy milk"


@pytest.mark.parametrize("title", ["", "   ", "\t\n"])
def test_empty_title_is_rejected(title):
    with pytest.raises(ValueError, match="title must not be empty"):
        Task(id=1, title=title)


def test_unknown_priority_is_rejected():
    with pytest.raises(ValueError, match="unknown priority: urgent"):
        Task(id=1, title="Buy milk", priority="urgent")


def test_dict_round_trip():
    task = Task(id=3, title="Pay rent", priority="high", done=True)
    assert Task.from_dict(task.to_dict()) == task


def test_from_dict_fills_defaults():
    task = Task.from_dict({"id": 2, "title": "Call mom"})
    assert task == Task(id=2, title="Call mom", priority="normal", done=False)

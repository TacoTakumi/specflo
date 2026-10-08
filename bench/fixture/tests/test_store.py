import json

import pytest

from tinytodo.store import TaskNotFound, TaskStore


@pytest.fixture
def store(tmp_path):
    return TaskStore(tmp_path / "todo.json")


def test_missing_file_is_empty(store):
    assert store.list(include_done=True) == []
    assert not store.path.exists()


def test_add_assigns_increasing_ids(store):
    first = store.add("Buy milk")
    second = store.add("Pay rent", priority="high")
    assert (first.id, second.id) == (1, 2)
    assert second.priority == "high"


def test_get_unknown_id_raises(store):
    with pytest.raises(TaskNotFound, match="no task with id 9"):
        store.get(9)


def test_complete_and_list(store):
    store.add("Buy milk")
    store.add("Pay rent")
    store.complete(1)
    assert [t.id for t in store.list()] == [2]
    assert [t.id for t in store.list(include_done=True)] == [1, 2]


def test_remove(store):
    store.add("Buy milk")
    removed = store.remove(1)
    assert removed.title == "Buy milk"
    assert store.list(include_done=True) == []
    with pytest.raises(TaskNotFound):
        store.remove(1)


def test_ids_are_not_reused_after_remove(store):
    store.add("Buy milk")
    store.add("Pay rent")
    store.remove(2)
    store.save()
    reloaded = TaskStore(store.path)
    assert reloaded.add("Call mom").id == 3


def test_save_and_reload(store):
    store.add("Buy milk", priority="low")
    store.add("Pay rent")
    store.complete(2)
    store.save()
    reloaded = TaskStore(store.path)
    assert reloaded.list(include_done=True) == store.list(include_done=True)


def test_file_format(store):
    store.add("Buy milk")
    store.save()
    data = json.loads(store.path.read_text(encoding="utf-8"))
    assert data == {
        "version": 1,
        "next_id": 2,
        "tasks": [{"id": 1, "title": "Buy milk", "priority": "normal", "done": False}],
    }

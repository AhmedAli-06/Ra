"""Persistent task planner: add / list / complete / update / remove / clear."""
from ra import tasklist


def _reset(tmp_path, monkeypatch):
    monkeypatch.setattr(tasklist, "_PLAN_FILE", str(tmp_path / "planner.json"))


def test_add_and_list(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    tasklist.add_task("clean garage")
    tasklist.add_task("project report", ["outline", "write", "submit"])
    out = tasklist.list_tasks()
    assert "clean garage" in out
    assert "outline" in out


def test_complete_by_number(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    tasklist.add_task("first task")
    tasklist.add_task("second task")
    msg = tasklist.complete_task(2)
    assert "second task" in msg
    out = tasklist.list_tasks()          # done tasks hidden by default
    assert "second task" not in out
    out_all = tasklist.list_tasks(show_done=True)
    assert "second task" in out_all


def test_complete_bad_number(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    tasklist.add_task("only task")
    assert "No task" in tasklist.complete_task(9)


def test_update_status(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    tasklist.add_task("work item")
    assert "doing" in tasklist.update_status(1, status="doing")
    out = tasklist.list_tasks()
    assert "[~]" in out


def test_remove(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    tasklist.add_task("doomed")
    assert "Removed" in tasklist.remove_task(1)
    assert "No tasks" in tasklist.list_tasks()


def test_clear(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    tasklist.add_task("a")
    tasklist.add_task("b")
    assert "Cleared" in tasklist.clear_tasks()
    assert "No tasks" in tasklist.list_tasks()


def test_empty_list(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    assert "No tasks" in tasklist.list_tasks()


def test_persistence(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    tasklist.add_task("persistent task")
    items = tasklist._read()
    assert len(items) == 1
    assert items[0]["task"] == "persistent task"


def test_empty_add_rejected(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    assert "Nothing" in tasklist.add_task("   ")
from pathlib import Path

from orchestrator.models import TaskStatus
from orchestrator.queue import TaskQueue, make_task


def make_queue(tmp_path: Path) -> TaskQueue:
    return TaskQueue(tmp_path / "tasks.db")


def test_add_and_get(tmp_path):
    q = make_queue(tmp_path)
    task = make_task("demo", "D:/demo", "do something", "claude-code", None, 2, False)
    q.add(task)

    fetched = q.get(task.id)
    assert fetched is not None
    assert fetched.prompt == "do something"
    assert fetched.status == TaskStatus.PENDING


def test_update_changes_status(tmp_path):
    q = make_queue(tmp_path)
    task = make_task("demo", "D:/demo", "do something", "claude-code", None, 2, False)
    q.add(task)

    task.status = TaskStatus.DONE
    task.result = "hotovo"
    q.update(task)

    fetched = q.get(task.id)
    assert fetched.status == TaskStatus.DONE
    assert fetched.result == "hotovo"
    assert fetched.updated_at is not None


def test_list_and_filter(tmp_path):
    q = make_queue(tmp_path)
    t1 = make_task("demo", "D:/demo", "a", "claude-code", None, 2, False)
    t2 = make_task("demo", "D:/demo", "b", "claude-code", None, 2, False)
    q.add(t1)
    q.add(t2)
    t2.status = TaskStatus.DONE
    q.update(t2)

    all_tasks = q.list()
    assert len(all_tasks) == 2

    done_tasks = q.list(status=TaskStatus.DONE)
    assert [t.id for t in done_tasks] == [t2.id]


def test_next_pending(tmp_path):
    q = make_queue(tmp_path)
    assert q.next_pending() is None
    t1 = make_task("demo", "D:/demo", "a", "claude-code", None, 2, False)
    q.add(t1)
    assert q.next_pending().id == t1.id

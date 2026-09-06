from pathlib import Path

from orchestrator.models import Task, TaskStatus
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


def test_make_task_passes_through_requested_model_and_selection_reason(tmp_path):
    task = make_task(
        "demo", "D:/demo", "do something", "claude-code", None, 2, False,
        requested_model="claude-opus-4-1", selection_reason="explicit_agent",
    )
    assert task.requested_model == "claude-opus-4-1"
    assert task.selection_reason == "explicit_agent"
    assert task.model is None
    assert task.model_source is None


def test_next_pending(tmp_path):
    q = make_queue(tmp_path)
    assert q.next_pending() is None
    t1 = make_task("demo", "D:/demo", "a", "claude-code", None, 2, False)
    q.add(t1)
    assert q.next_pending().id == t1.id


def _waiting_autonomous_task(project: str, spec_text: str, created_at: str) -> Task:
    task = make_task(project, "D:/demo", "goal", "claude-code", None, 0, False, source="autonomous")
    task.created_at = created_at
    task.status = TaskStatus.WAITING_FOR_PROVIDER
    task.is_autonomous = True
    task.spec_text = spec_text
    task.retry_at = "2999-01-01T00:00:00+00:00"
    return task


def test_dedupe_waiting_tasks_keeps_oldest_and_supersedes_rest(tmp_path):
    q = make_queue(tmp_path)
    t1 = _waiting_autonomous_task("demo", "- [ ] a", "2026-01-01T00:00:00+00:00")
    t2 = _waiting_autonomous_task("demo", "- [ ] a", "2026-01-01T00:05:00+00:00")
    t3 = _waiting_autonomous_task("demo", "- [ ] a", "2026-01-01T00:10:00+00:00")
    for t in (t1, t2, t3):
        q.add(t)

    superseded = q.dedupe_waiting_tasks()

    assert superseded == 2
    assert q.get(t1.id).status == TaskStatus.WAITING_FOR_PROVIDER
    assert q.get(t2.id).status == TaskStatus.ERROR
    assert q.get(t3.id).status == TaskStatus.ERROR
    assert t1.id in q.get(t2.id).error


def test_dedupe_waiting_tasks_ignores_pm_checkpoint_run_id_noise(tmp_path):
    """Regression test for the real bug found in the live queue: AI Project
    Manager appends a trailing "<!-- PM-CHECKPOINT {"run_id": ...} -->"
    comment to spec_text and mints a NEW random run_id in it on every
    scheduler tick that resubmits the same card - so two spec_text values
    for "the same" card are byte-for-byte identical except for that one
    embedded id. Naive exact-string dedup never matches these and is
    exactly how ~11 duplicate WAITING_FOR_PROVIDER rows for one card piled
    up in production before this fix.
    """
    q = make_queue(tmp_path)
    spec_a = (
        "## Definition of Done\n- [ ] a\n\n"
        '<!-- PM-CHECKPOINT\n{\n  "run_id": "aaaaaaaaaaaa",\n  "checkpoint": {}\n}\n-->\n'
    )
    spec_b = (
        "## Definition of Done\n- [ ] a\n\n"
        '<!-- PM-CHECKPOINT\n{\n  "run_id": "bbbbbbbbbbbb",\n  "checkpoint": {}\n}\n-->\n'
    )
    t1 = _waiting_autonomous_task("demo", spec_a, "2026-01-01T00:00:00+00:00")
    t2 = _waiting_autonomous_task("demo", spec_b, "2026-01-01T00:05:00+00:00")
    q.add(t1)
    q.add(t2)

    superseded = q.dedupe_waiting_tasks()

    assert superseded == 1
    assert q.get(t1.id).status == TaskStatus.WAITING_FOR_PROVIDER
    assert q.get(t2.id).status == TaskStatus.ERROR

    found = q.find_active_autonomous("demo", spec_b)
    assert found is not None
    assert found.id == t1.id


def test_dedupe_waiting_tasks_leaves_different_projects_and_specs_alone(tmp_path):
    q = make_queue(tmp_path)
    t1 = _waiting_autonomous_task("demo-a", "- [ ] a", "2026-01-01T00:00:00+00:00")
    t2 = _waiting_autonomous_task("demo-b", "- [ ] a", "2026-01-01T00:00:00+00:00")
    t3 = _waiting_autonomous_task("demo-a", "- [ ] different spec", "2026-01-01T00:00:00+00:00")
    for t in (t1, t2, t3):
        q.add(t)

    superseded = q.dedupe_waiting_tasks()

    assert superseded == 0
    for t in (t1, t2, t3):
        assert q.get(t.id).status == TaskStatus.WAITING_FOR_PROVIDER


def test_dedupe_waiting_tasks_is_idempotent(tmp_path):
    q = make_queue(tmp_path)
    t1 = _waiting_autonomous_task("demo", "- [ ] a", "2026-01-01T00:00:00+00:00")
    t2 = _waiting_autonomous_task("demo", "- [ ] a", "2026-01-01T00:05:00+00:00")
    q.add(t1)
    q.add(t2)

    assert q.dedupe_waiting_tasks() == 1
    assert q.dedupe_waiting_tasks() == 0


def test_recover_orphaned_active_tasks_marks_error(tmp_path):
    q = make_queue(tmp_path)
    running = make_task("demo", "D:/demo", "a", "claude-code", None, 0, False)
    running.status = TaskStatus.RUNNING
    q.add(running)
    testing = make_task("demo", "D:/demo", "b", "claude-code", None, 0, False)
    testing.status = TaskStatus.TESTING
    q.add(testing)
    pending = make_task("demo", "D:/demo", "c", "claude-code", None, 0, False)
    q.add(pending)

    recovered = q.recover_orphaned_active_tasks("restarted mid-run")

    assert recovered == 2
    assert q.get(running.id).status == TaskStatus.ERROR
    assert q.get(running.id).error == "restarted mid-run"
    assert q.get(testing.id).status == TaskStatus.ERROR
    assert q.get(pending.id).status == TaskStatus.PENDING

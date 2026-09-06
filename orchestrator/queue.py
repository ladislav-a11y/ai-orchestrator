"""SQLite-backed task queue.

SQLite (not a JSON file) so that the CLI and the local API can safely read
task status while the background worker is writing to it. It is a single
local file (data/tasks.db) - nothing to install, nothing running as a service.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from orchestrator.models import Task, TaskStatus
from orchestrator.spec_text import normalize_spec_text as _spec_dedup_key

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_task_id() -> str:
    return uuid.uuid4().hex[:12]


class TaskQueue:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def add(self, task: Task) -> Task:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO tasks (id, data, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    task.id,
                    json.dumps(task.to_dict(), ensure_ascii=False),
                    task.status.value,
                    task.created_at,
                    task.updated_at,
                ),
            )
        return task

    def update(self, task: Task) -> Task:
        task.updated_at = now_iso()
        with self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET data = ?, status = ?, updated_at = ? WHERE id = ?",
                (
                    json.dumps(task.to_dict(), ensure_ascii=False),
                    task.status.value,
                    task.updated_at,
                    task.id,
                ),
            )
        return task

    def get(self, task_id: str) -> Optional[Task]:
        with self._connect() as conn:
            row = conn.execute("SELECT data FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            return None
        return Task.from_dict(json.loads(row["data"]))

    def list(
        self,
        status: Optional[TaskStatus] = None,
        limit: int = 50,
    ) -> list[Task]:
        with self._connect() as conn:
            if status is not None:
                rows = conn.execute(
                    "SELECT data FROM tasks WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status.value, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT data FROM tasks ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [Task.from_dict(json.loads(r["data"])) for r in rows]

    def next_pending(self) -> Optional[Task]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT data FROM tasks WHERE status = ? ORDER BY created_at ASC LIMIT 1",
                (TaskStatus.PENDING.value,),
            ).fetchone()
        if row is None:
            return None
        return Task.from_dict(json.loads(row["data"]))


    def next_due_waiting(self, now: Optional[str] = None) -> Optional[Task]:
        """Return the oldest autonomous provider-waiting task whose retry time is due."""
        now = now or now_iso()
        waiting = self.list(status=TaskStatus.WAITING_FOR_PROVIDER, limit=1000)
        due = [
            task
            for task in waiting
            if task.is_autonomous
            and task.retry_at is not None
            and task.retry_at <= now
        ]
        if not due:
            return None
        due.sort(key=lambda task: (task.retry_at or "", task.created_at))
        return due[0]

    def claim_next_due_waiting(self, now: Optional[str] = None) -> Optional[Task]:
        """Atomically claim one due autonomous task for a worker.

        Merely reading the oldest WAITING row allowed every 30-second worker
        tick (and multiple service processes) to submit the same task again
        before its first execution changed state.  ``BEGIN IMMEDIATE`` keeps
        selection and the WAITING -> RUNNING transition in one SQLite write
        transaction, so only one caller can win.
        """
        now = now or now_iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT data FROM tasks WHERE status = ? ORDER BY created_at ASC",
                (TaskStatus.WAITING_FOR_PROVIDER.value,),
            ).fetchall()
            due = []
            for row in rows:
                task = Task.from_dict(json.loads(row["data"]))
                if task.is_autonomous and task.retry_at is not None and task.retry_at <= now:
                    due.append(task)
            if not due:
                return None
            due.sort(key=lambda task: (task.retry_at or "", task.created_at))
            task = due[0]
            task.status = TaskStatus.RUNNING
            task.updated_at = now_iso()
            updated = conn.execute(
                "UPDATE tasks SET data = ?, status = ?, updated_at = ? "
                "WHERE id = ? AND status = ?",
                (
                    json.dumps(task.to_dict(), ensure_ascii=False),
                    TaskStatus.RUNNING.value,
                    task.updated_at,
                    task.id,
                    TaskStatus.WAITING_FOR_PROVIDER.value,
                ),
            )
            return task if updated.rowcount == 1 else None

    def find_active_autonomous(self, project: str, spec_text: Optional[str]) -> Optional[Task]:
        """Find an already queued/running retry for the same project+spec."""
        key = _spec_dedup_key(spec_text)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT data FROM tasks WHERE status IN (?, ?) ORDER BY created_at ASC",
                (TaskStatus.WAITING_FOR_PROVIDER.value, TaskStatus.RUNNING.value),
            ).fetchall()
        for row in rows:
            task = Task.from_dict(json.loads(row["data"]))
            if (
                task.is_autonomous
                and task.project == project
                and _spec_dedup_key(task.spec_text) == key
            ):
                return task
        return None

    def dedupe_waiting_tasks(self, now: Optional[str] = None) -> int:
        """Collapse duplicate WAITING_FOR_PROVIDER rows for the same
        (project, spec_text) pair down to one.

        Before ``find_active_autonomous`` existed, every scheduler tick that
        hit a still-limited provider created a brand new waiting row instead
        of reusing one, so the same autonomous goal could pile up dozens of
        duplicate rows in the live queue - each of which the waiting worker
        would eventually claim and RE-RUN in full, one 30s tick at a time.
        Keeps the oldest (first created) row per group - the one closest to
        actually being due - and marks the rest ERROR/superseded instead of
        leaving them to be claimed and duplicately executed. Safe to call on
        every worker tick: a no-op when there is nothing to collapse.
        Returns how many rows were superseded.
        """
        now = now or now_iso()
        superseded = 0
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT data FROM tasks WHERE status = ? ORDER BY created_at ASC",
                (TaskStatus.WAITING_FOR_PROVIDER.value,),
            ).fetchall()
            groups: dict[tuple, list[Task]] = {}
            for row in rows:
                task = Task.from_dict(json.loads(row["data"]))
                if not task.is_autonomous:
                    continue
                key = (task.project, _spec_dedup_key(task.spec_text))
                groups.setdefault(key, []).append(task)
            for tasks in groups.values():
                if len(tasks) <= 1:
                    continue
                tasks.sort(key=lambda t: t.created_at)
                keep, extras = tasks[0], tasks[1:]
                for extra in extras:
                    extra.status = TaskStatus.ERROR
                    extra.error = (
                        f"Nahrazeno duplicitním čekajícím během {keep.id} pro "
                        "stejný projekt a Definition of Done (dedup fronty)."
                    )
                    extra.updated_at = now
                    updated = conn.execute(
                        "UPDATE tasks SET data = ?, status = ?, updated_at = ? "
                        "WHERE id = ? AND status = ?",
                        (
                            json.dumps(extra.to_dict(), ensure_ascii=False),
                            TaskStatus.ERROR.value,
                            now,
                            extra.id,
                            TaskStatus.WAITING_FOR_PROVIDER.value,
                        ),
                    )
                    superseded += updated.rowcount
        return superseded

    def recover_orphaned_active_tasks(self, message: str) -> int:
        """Mark tasks stuck in an in-flight status (RUNNING/TESTING/FIXING/
        COMMITTING) as ERROR. Call once, early in service startup: a fresh
        process starts with an empty worker pool, so any row already in one
        of these statuses at that point cannot belong to this process - it
        can only be left over from a previous process that crashed or was
        killed mid-run. Left alone, such a row never becomes WAITING/DONE/
        error on its own, and ``find_active_autonomous`` would keep treating
        it as a live run forever, blocking a real retry from ever starting.
        """
        active = (
            TaskStatus.RUNNING,
            TaskStatus.TESTING,
            TaskStatus.FIXING,
            TaskStatus.COMMITTING,
        )
        now = now_iso()
        recovered = 0
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            placeholders = ", ".join("?" for _ in active)
            rows = conn.execute(
                f"SELECT data FROM tasks WHERE status IN ({placeholders})",
                tuple(status.value for status in active),
            ).fetchall()
            for row in rows:
                task = Task.from_dict(json.loads(row["data"]))
                task.status = TaskStatus.ERROR
                task.error = message
                task.updated_at = now
                conn.execute(
                    "UPDATE tasks SET data = ?, status = ?, updated_at = ? WHERE id = ?",
                    (
                        json.dumps(task.to_dict(), ensure_ascii=False),
                        TaskStatus.ERROR.value,
                        now,
                        task.id,
                    ),
                )
                recovered += 1
        return recovered


def make_task(
    project: str,
    project_path: str,
    prompt: str,
    agent: str,
    test_command: Optional[str],
    max_fix_attempts: int,
    auto_commit_requested: bool,
    source: str = "cli",
    requested_model: Optional[str] = None,
    selection_reason: Optional[str] = None,
) -> Task:
    return Task(
        id=new_task_id(),
        created_at=now_iso(),
        project=project,
        project_path=project_path,
        prompt=prompt,
        agent=agent,
        status=TaskStatus.PENDING,
        test_command=test_command,
        max_fix_attempts=max_fix_attempts,
        auto_commit_requested=auto_commit_requested,
        source=source,
        requested_model=requested_model,
        selection_reason=selection_reason,
    )

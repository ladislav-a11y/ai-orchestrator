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


def make_task(
    project: str,
    project_path: str,
    prompt: str,
    agent: str,
    test_command: Optional[str],
    max_fix_attempts: int,
    auto_commit_requested: bool,
    source: str = "cli",
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
    )

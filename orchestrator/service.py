"""Wires config + queue + agent + runner together.

One OrchestratorService instance is shared by the CLI and the local API.
Tasks are processed one at a time by a single background worker thread, so
two tasks never run against the same project concurrently.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from orchestrator.agents.registry import build_agent
from orchestrator.config import Config, load_config
from orchestrator.logging_config import setup_logging, write_task_log
from orchestrator.models import Task, TaskStatus
from orchestrator.queue import TaskQueue, make_task
from orchestrator.runner import run_task


class OrchestratorService:
    def __init__(self, config: Optional[Config] = None):
        self.config = config or load_config()
        self.logger = setup_logging(self.config.logs_dir)
        self.queue = TaskQueue(self.config.data_dir / "tasks.db")
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="orchestrator-worker")

    # -- task submission -------------------------------------------------

    def submit(
        self,
        project_ref: str,
        prompt: str,
        agent_name: Optional[str] = None,
        test_command_override: Optional[str] = None,
        auto_commit: Optional[bool] = None,
        source: str = "cli",
    ) -> Task:
        entry = self.config.resolve_project(project_ref)
        test_command = test_command_override
        if test_command is None:
            test_command = entry.test_command or self.config.testing.test_command or None

        task = make_task(
            project=entry.name,
            project_path=entry.path,
            prompt=prompt,
            agent=agent_name or self.config.default_agent,
            test_command=test_command,
            max_fix_attempts=self.config.testing.max_fix_attempts,
            auto_commit_requested=self.config.git.auto_commit if auto_commit is None else auto_commit,
            source=source,
        )
        self.queue.add(task)
        self.logger.info("Task %s zařazen do fronty (projekt=%s, zdroj=%s)", task.id, entry.name, source)
        return task

    def run_sync(self, task: Task) -> Task:
        """Run a task in the current thread and block until done. Used by the CLI."""
        return self._execute(task)

    def submit_async(self, *args, **kwargs) -> Task:
        """Submit and schedule for background execution. Used by the API."""
        task = self.submit(*args, **kwargs)
        self._executor.submit(self._execute, task)
        return task

    # -- execution ---------------------------------------------------------

    def _execute(self, task: Task) -> Task:
        agent = build_agent(task.agent, self.config)
        result_task = run_task(task, self.config, agent, self.queue, self.logger)
        log_path = write_task_log(self.config.logs_dir, result_task)
        result_task.log_file = str(log_path.relative_to(self.config.logs_dir.parent))
        self.queue.update(result_task)
        self._write_outbox(result_task)
        return result_task

    def _write_outbox(self, task: Task) -> None:
        self.config.outbox_dir.mkdir(parents=True, exist_ok=True)
        out_path = self.config.outbox_dir / f"{task.id}.json"
        out_path.write_text(
            json.dumps(task.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # -- inbox (future external bridge, e.g. from ChatGPT) -----------------

    def import_inbox(self) -> list[Task]:
        """Read *.json task files from inbox/, enqueue them, move to inbox/processed/.

        Expected file shape: {"project": "...", "prompt": "...", "agent": "...",
        "test_command": "...", "auto_commit": true}. Only "project" and "prompt"
        are required. Files are moved, never deleted.
        """
        inbox_dir = self.config.inbox_dir
        processed_dir = inbox_dir / "processed"
        processed_dir.mkdir(parents=True, exist_ok=True)
        created: list[Task] = []

        for f in sorted(inbox_dir.glob("*.json")):
            try:
                spec = json.loads(f.read_text(encoding="utf-8"))
                task = self.submit(
                    project_ref=spec["project"],
                    prompt=spec["prompt"],
                    agent_name=spec.get("agent"),
                    test_command_override=spec.get("test_command"),
                    auto_commit=spec.get("auto_commit"),
                    source="inbox",
                )
                created.append(task)
                f.rename(processed_dir / f.name)
            except Exception as e:  # noqa: BLE001 - keep processing remaining files
                self.logger.error("Nepodařilo se zpracovat inbox soubor %s: %s", f, e)
        return created

    # -- read-only accessors -------------------------------------------------

    def get_task(self, task_id: str) -> Optional[Task]:
        return self.queue.get(task_id)

    def list_tasks(self, status: Optional[TaskStatus] = None, limit: int = 50) -> list[Task]:
        return self.queue.list(status=status, limit=limit)

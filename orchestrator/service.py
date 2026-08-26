"""Wires config + queue + agent + runner together.

One OrchestratorService instance is shared by the CLI and the local API.
Tasks are processed one at a time by a single background worker thread, so
two tasks never run against the same project concurrently.
"""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from orchestrator.agents.registry import build_agent, build_failover_agent
from orchestrator.autonomous import (
    AutonomousResult,
    AutonomousStatus,
    DEFAULT_MAX_ITERATIONS,
    DOD_BATCH_SIZE,
    parse_definition_of_done,
    run_autonomous_loop,
)
from orchestrator.autonomous_checkpoint import (
    apply_checkpoint,
    load_checkpoint,
    save_checkpoint,
)
from orchestrator.claude_settings import ensure_project_claude_settings
from orchestrator.config import Config, load_config
from orchestrator.git_utils import has_uncommitted_changes, is_git_repo
from orchestrator.logging_config import setup_logging, write_autonomous_log, write_task_log
from orchestrator.models import Task, TaskStatus
from orchestrator.queue import TaskQueue, make_task, new_task_id
from orchestrator.runner import run_task


class OrchestratorService:
    def __init__(self, config: Optional[Config] = None):
        self.config = config or load_config()
        self.logger = setup_logging(self.config.logs_dir)
        self.queue = TaskQueue(self.config.data_dir / "tasks.db")
        self._instance_lock_file = None
        self._acquire_instance_lock()
        self._recover_on_startup()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="orchestrator-worker")
        self._waiting_worker_stop = False
        self._waiting_worker_event = threading.Event()
        self._waiting_worker_interval = 30
        self._waiting_worker = threading.Thread(target=self._waiting_worker_loop, daemon=True)
        self._waiting_worker.start()
        self._resume_waiting_tasks()

    def _acquire_instance_lock(self) -> None:
        """Ensure only one service process uses this queue database at a time."""
        lock_path = self.config.data_dir / "orchestrator.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError(
                f"Jiná instance ai-orchestrator již používá frontu {self.queue.db_path}."
            ) from exc
        self._instance_lock_file = handle

    def _release_instance_lock(self) -> None:
        handle = self._instance_lock_file
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._instance_lock_file = None

    def _recover_on_startup(self) -> None:
        """Reconcile queue state left over from a previous process before
        anything in this process starts claiming work - see
        TaskQueue.recover_orphaned_active_tasks/dedupe_waiting_tasks."""
        recovered = self.queue.recover_orphaned_active_tasks(
            "Přerušeno restartem orchestrátoru: předchozí proces skončil "
            "uprostřed běhu (RUNNING/TESTING/FIXING/COMMITTING) a nikdy "
            "nedoběhl do koncového stavu."
        )
        if recovered:
            self.logger.warning(
                "Obnova po startu: %d osiřelých úloh z předchozího běhu označeno jako error.",
                recovered,
            )
        superseded = self.queue.dedupe_waiting_tasks()
        if superseded:
            self.logger.warning(
                "Obnova po startu: %d duplicitních čekajících běhů ve frontě sloučeno do jednoho.",
                superseded,
            )

    def _resume_waiting_tasks(self) -> None:
        """Resume autonomous tasks that were waiting for provider quota."""
        task = self.queue.claim_next_due_waiting()
        if task is None:
            return

        self.logger.info(
            "Obnovuji čekající task %s po čekání na providera",
            task.id,
        )
        self._executor.submit(self._resume_autonomous_task, task)

    def _waiting_worker_loop(self) -> None:
        """Periodically resume autonomous tasks after provider wait."""
        first_pass = True
        while not self._waiting_worker_stop or first_pass:
            first_pass = False
            try:
                self.queue.dedupe_waiting_tasks()
                task = self.queue.claim_next_due_waiting()
                if task is not None:
                    self.logger.info(
                        "Worker obnovuje čekající task %s po vypršení čekání",
                        task.id,
                    )
                    self._executor.submit(self._resume_autonomous_task, task)
            except Exception as exc:
                self.logger.error(
                    "Chyba waiting workeru: %s",
                    exc,
                )

            self._waiting_worker_event.wait(self._waiting_worker_interval)

    def shutdown(self, wait: bool = True) -> None:
        """Stop background workers and release resources."""
        self._waiting_worker_stop = True
        self._waiting_worker_event.set()

        if self._waiting_worker.is_alive():
            self._waiting_worker.join(timeout=5)

        try:
            self._executor.shutdown(wait=wait)
        finally:
            self._release_instance_lock()
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
        project_dir = Path(entry.path)
        if not project_dir.exists():
            # Only registered projects (config.yaml) can reach this point
            # with a non-existent path - resolve_project() already requires
            # a raw filesystem path to exist, and already checked entry.path
            # lies inside workspace_root. This lets a project like
            # "station-agent" be registered before it exists on disk, so the
            # agent can scaffold it from scratch on its first task.
            project_dir.mkdir(parents=True, exist_ok=True)
            self.logger.info("Vytvořen nový adresář projektu '%s': %s", entry.name, project_dir)

        # Same safe allow/deny permissions for every project the orchestrator
        # touches - no-op if the project already has its own settings file.
        ensure_project_claude_settings(project_dir)

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

    def _resume_autonomous_task(self, task: Task) -> None:
        """Resume a claimed autonomous retry through the autonomous loop."""
        try:
            self.run_autonomous(
                project_ref=task.project,
                goal=task.goal or task.prompt,
                spec_text=task.spec_text,
                agent_name=task.agent,
                test_command_override=task.test_command,
                max_iterations=task.max_iterations,
                auto_commit=False,
                run_id=task.run_id,
                _waiting_task=task,
            )
        except Exception as exc:  # noqa: BLE001 - persist worker failure
            task.status = TaskStatus.ERROR
            task.error = str(exc)
            self.queue.update(task)
            self.logger.error("Obnova čekající autonomní úlohy %s selhala: %s", task.id, exc)

    def _write_outbox(self, task: Task) -> None:
        self.config.outbox_dir.mkdir(parents=True, exist_ok=True)
        out_path = self.config.outbox_dir / f"{task.id}.json"
        out_path.write_text(
            json.dumps(task.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # -- autonomous development mode ----------------------------------------

    def run_autonomous(
        self,
        project_ref: str,
        goal: Optional[str] = None,
        spec_text: Optional[str] = None,
        agent_name: Optional[str] = None,
        test_command_override: Optional[str] = None,
        max_iterations: Optional[int] = None,
        auto_commit: Optional[bool] = None,
        run_id: Optional[str] = None,
        _waiting_task: Optional[Task] = None,
    ) -> tuple[str, AutonomousResult]:
        """Run the autonomous implement -> test -> evaluate -> fix loop until
        the Definition of Done is met or a safe iteration/no-progress limit is
        hit. See orchestrator/autonomous.py for the loop itself; this method
        only wires it up the same way submit()/run_sync() wire up a normal
        task (project resolution, workspace_root check, Claude settings,
        agent construction, logging).
        """
        if not (goal or spec_text):
            raise ValueError("Je potřeba zadat --goal nebo --spec (Definition of Done).")

        entry = self.config.resolve_project(project_ref)
        project_dir = Path(entry.path)
        if not project_dir.exists():
            project_dir.mkdir(parents=True, exist_ok=True)
            self.logger.info("Vytvořen nový adresář projektu '%s': %s", entry.name, project_dir)

        preexisting_dirty = is_git_repo(project_dir) and has_uncommitted_changes(project_dir)
        ensure_project_claude_settings(project_dir)

        dod_source = spec_text if spec_text else goal
        dod_items = parse_definition_of_done(dod_source)
        goal_text = goal or dod_source.strip().splitlines()[0]

        test_command = test_command_override
        if test_command is None:
            test_command = entry.test_command or self.config.testing.test_command or None

        if agent_name:
            agent = build_agent(agent_name, self.config)
        else:
            agent = build_failover_agent(
            self.config,
            logger=self.logger,
            agent_builder=build_agent,
        )
        run_id = run_id or new_task_id()

        # Restore already-verified DoD progress from a previous, separate
        # run of this exact project+spec (see autonomous_checkpoint.py) -
        # never trusts an agent claim, only what an earlier run's own
        # orchestrator-verified merge already persisted.
        checkpoint = load_checkpoint(self.config.data_dir, project_dir, dod_source)
        restored = apply_checkpoint(dod_items, checkpoint) if checkpoint else 0
        if restored:
            resume_batch = [idx for idx, item in enumerate(dod_items) if not item.done][:DOD_BATCH_SIZE]
            self.logger.info(
                "Autonomní běh %s: obnoveno %s/%s bodů Definition of Done z checkpointu projektu "
                "'%s' (z běhu %s) - pokračuji dávkou bodů %s, ne od bodu 0",
                run_id, restored, len(dod_items), entry.name, checkpoint.run_id, resume_batch,
            )
        elif checkpoint is not None:
            # A checkpoint file exists but apply_checkpoint() refused it
            # (item count/text mismatch despite a matching spec hash) -
            # extremely unlikely, but must never silently misapply state.
            self.logger.warning(
                "Autonomní běh %s: checkpoint pro projekt '%s' nalezen, ale neodpovídá aktuálně "
                "naparsované Definition of Done - ignoruji ho a začínám od bodu 0",
                run_id, entry.name,
            )

        def on_iteration(partial_result: AutonomousResult) -> None:
            write_autonomous_log(self.config.logs_dir, run_id, entry.name, goal_text, partial_result)
            # Persisted after every iteration (not just at the end of the
            # run) so this progress survives Ctrl+C, a session limit, or a
            # crash - see autonomous_checkpoint.py module docstring.
            save_checkpoint(
                self.config.data_dir, project_dir, dod_source, goal_text,
                partial_result.dod_items, run_id,
            )

        result = run_autonomous_loop(
            run_id=run_id,
            project_path=project_dir,
            goal=goal_text,
            dod_items=dod_items,
            config=self.config,
            agent=agent,
            logger=self.logger,
            test_command=test_command,
            max_iterations=max_iterations or DEFAULT_MAX_ITERATIONS,
            auto_commit_requested=(self.config.git.auto_commit if auto_commit is None else auto_commit),
            on_iteration=on_iteration,
            preexisting_dirty=preexisting_dirty,
        )
        if result.status == AutonomousStatus.WAITING_FOR_PROVIDER:
            waiting_task = _waiting_task or self.queue.find_active_autonomous(
                entry.name, dod_source
            )
            is_new_waiting_task = waiting_task is None
            if waiting_task is None:
                waiting_task = make_task(
                    project=entry.name,
                    project_path=str(project_dir),
                    prompt=goal_text,
                    # The autonomous run used failover when no explicit agent was
                    # supplied.  Persist that strategy, not default_agent
                    # (claude-code), so a later resume cannot silently lose the
                    # antigravity/codex fallback.
                    agent=agent_name or "auto",
                    test_command=test_command,
                    max_fix_attempts=0,
                    auto_commit_requested=False,
                    source="autonomous",
                )

            waiting_task.status = TaskStatus.WAITING_FOR_PROVIDER
            waiting_task.error = result.error
            waiting_task.retry_after_seconds = result.retry_after_seconds
            if result.retry_after_seconds is not None:
                from datetime import datetime, timedelta, timezone
                # timespec="seconds" matches queue.now_iso() exactly - the two
                # strings are compared lexicographically in
                # claim_next_due_waiting()/next_due_waiting(). Without it,
                # this value carries microseconds ("...57.123456+00:00")
                # while now_iso() does not ("...57+00:00"); '.' (0x2E) sorts
                # after '+' (0x2B), so retry_at would compare as LATER than
                # an equal or later now_iso() whenever both fall in the same
                # second - a task due "now" (e.g. retry_after_seconds=0)
                # would never be claimable.
                waiting_task.retry_at = (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=result.retry_after_seconds)
                ).isoformat(timespec="seconds")
            waiting_task.goal = goal_text
            waiting_task.spec_text = dod_source
            waiting_task.is_autonomous = True
            waiting_task.max_iterations = max_iterations or DEFAULT_MAX_ITERATIONS
            waiting_task.run_id = run_id

            if is_new_waiting_task:
                self.queue.add(waiting_task)
            else:
                self.queue.update(waiting_task)

            self.logger.info(
                "Autonomní běh %s čeká na provider limit, uložen do fronty jako %s",
                run_id,
                waiting_task.id,
            )
        elif _waiting_task is not None:
            _waiting_task.status = (
                TaskStatus.DONE
                if result.status == AutonomousStatus.COMPLETED
                else TaskStatus.FAILED
                if result.status in (AutonomousStatus.BLOCKED, AutonomousStatus.MAX_ITERATIONS)
                else TaskStatus.ERROR
            )
            _waiting_task.result = (
                result.iterations[-1].agent_output if result.iterations else ""
            )
            _waiting_task.error = result.error
            _waiting_task.retry_at = None
            _waiting_task.retry_after_seconds = None
            self.queue.update(_waiting_task)

        result.restored_from_checkpoint = restored

        write_autonomous_log(self.config.logs_dir, run_id, entry.name, goal_text, result)
        save_checkpoint(self.config.data_dir, project_dir, dod_source, goal_text, result.dod_items, run_id)
        self._write_autonomous_outbox(run_id, entry.name, goal_text, result)
        return run_id, result

    def _write_autonomous_outbox(self, run_id: str, project: str, goal: str, result: AutonomousResult) -> None:
        self.config.outbox_dir.mkdir(parents=True, exist_ok=True)
        out_path = self.config.outbox_dir / f"autonomous-{run_id}.json"
        completed_indices = [index for index, item in enumerate(result.dod_items) if item.done]
        next_item = next((item.text for item in result.dod_items if not item.done), "")
        last_output = result.iterations[-1].agent_output if result.iterations else ""
        provider_sequence = []
        for iteration in result.iterations:
            if iteration.agent_name and iteration.agent_name not in provider_sequence:
                provider_sequence.append(iteration.agent_name)
        stop_reason = result.error
        if not stop_reason and result.status != AutonomousStatus.COMPLETED:
            stop_reason = result.status.value
        payload = {
            "run_id": run_id,
            "project": project,
            "goal": goal,
            "status": result.status.value,
            "dod_items": [{"text": i.text, "done": i.done} for i in result.dod_items],
            "iterations": [it.to_dict() for it in result.iterations],
            "committed": result.committed,
            "commit_hash": result.commit_hash,
            "error": result.error,
            "retry_after_seconds": result.retry_after_seconds,
            "restored_from_checkpoint": result.restored_from_checkpoint,
            "breaker_saved_attempts": result.breaker_saved_attempts,
            # Stable handoff contract consumed by AI Project Manager.
            "done": result.status == AutonomousStatus.COMPLETED,
            "checkpoint": {
                "run_id": run_id,
                "completed_dod_indices": completed_indices,
            },
            "last_output": last_output,
            "next_step": next_item,
            "stop_reason": stop_reason,
            "limit_hit": (
                result.error or "all configured providers are limited"
                if result.status == AutonomousStatus.WAITING_FOR_PROVIDER
                else None
            ),
            "provider_sequence": provider_sequence,
            "active_provider": provider_sequence[-1] if provider_sequence else None,
        }
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

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







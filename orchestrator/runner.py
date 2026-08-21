"""The pipeline: agent -> tests -> (fix loop) -> (review, future) -> git commit.

See ARCHITECTURE.md for the full picture. This module only orchestrates;
it has no knowledge of the CLI, the API, or how the task was submitted.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from orchestrator.agents.base import Agent, AgentRunRequest
from orchestrator.config import Config
from orchestrator.git_utils import GitError, commit as git_commit, has_uncommitted_changes, is_git_repo
from orchestrator.models import Task, TaskStatus
from orchestrator.queue import TaskQueue

TEST_TIMEOUT_SECONDS = 900
MAX_LOG_TAIL_CHARS = 6000


def tail_text(text: str, limit: int = MAX_LOG_TAIL_CHARS) -> str:
    return text if len(text) <= limit else text[-limit:]


def run_test_command(project_path: Path, test_command: str, logger: logging.Logger) -> tuple[bool, str]:
    logger.info("Spouštím testy: %s", test_command)
    try:
        proc = subprocess.run(
            test_command,
            shell=True,
            cwd=str(project_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TEST_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return False, f"Testy nedoběhly do {TEST_TIMEOUT_SECONDS}s (timeout)."
    output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    return proc.returncode == 0, output.strip()


def _fix_prompt(original_prompt: str, test_command: str, test_output: str) -> str:
    return (
        "Předchozí pokus o splnění tohoto úkolu prošel, ale automatické testy selhaly.\n"
        f"Testovací příkaz: {test_command}\n\n"
        f"Výstup testů (může být zkrácený):\n{tail_text(test_output)}\n\n"
        "Oprav kód tak, aby testy prošly. Neměň nic, co s chybou nesouvisí. "
        f"Původní zadání pro kontext: {original_prompt}"
    )


def run_task(task: Task, config: Config, agent: Agent, queue: TaskQueue, logger: logging.Logger) -> Task:
    project_path = Path(task.project_path)
    task.status = TaskStatus.RUNNING
    queue.update(task)
    logger.info("Task %s: spouštím agenta '%s' na projektu %s", task.id, task.agent, project_path)

    result = agent.run(AgentRunRequest(project_path=project_path, prompt=task.prompt))
    task.claude_session_id = result.session_id
    task.cost_usd = result.cost_usd

    if not result.success:
        task.status = TaskStatus.ERROR
        task.result = result.output_text
        task.error = result.error
        queue.update(task)
        logger.error("Task %s: agent selhal: %s", task.id, result.error)
        return task

    task.result = result.output_text
    task.attempts = 1
    logger.info("Task %s: agent dokončil běh, session=%s", task.id, task.claude_session_id)

    if task.test_command:
        while True:
            task.status = TaskStatus.TESTING
            queue.update(task)
            passed, output = run_test_command(project_path, task.test_command, logger)
            task.test_output = output
            task.tests_passed = passed
            if passed:
                logger.info("Task %s: testy prošly", task.id)
                break

            logger.warning("Task %s: testy selhaly (pokus %s)", task.id, task.attempts)
            if task.attempts > task.max_fix_attempts:
                task.status = TaskStatus.FAILED
                task.error = "Testy opakovaně selhávaly, dosažen limit max_fix_attempts."
                queue.update(task)
                return task

            task.status = TaskStatus.FIXING
            queue.update(task)
            fix_result = agent.run(
                AgentRunRequest(
                    project_path=project_path,
                    prompt=_fix_prompt(task.prompt, task.test_command, output),
                    session_id=task.claude_session_id,
                )
            )
            task.attempts += 1
            task.claude_session_id = fix_result.session_id or task.claude_session_id
            if fix_result.cost_usd:
                task.cost_usd = (task.cost_usd or 0) + fix_result.cost_usd
            if not fix_result.success:
                task.status = TaskStatus.ERROR
                task.result = fix_result.output_text
                task.error = fix_result.error
                queue.update(task)
                logger.error("Task %s: oprava selhala: %s", task.id, fix_result.error)
                return task
            task.result = fix_result.output_text
    else:
        task.tests_passed = None  # no test command configured -> tests skipped, not failed

    _maybe_commit(task, project_path, config, logger)

    task.status = TaskStatus.DONE
    queue.update(task)
    logger.info("Task %s: hotovo (committed=%s)", task.id, task.committed)
    return task


def _maybe_commit(task: Task, project_path: Path, config: Config, logger: logging.Logger) -> None:
    if not (task.auto_commit_requested and config.git.auto_commit):
        return
    if task.tests_passed is False:
        return  # never commit on failing tests - enforced regardless of caller intent
    if not is_git_repo(project_path):
        logger.info("Task %s: %s není Git repozitář, commit se přeskakuje", task.id, project_path)
        return
    if not has_uncommitted_changes(project_path):
        logger.info("Task %s: žádné změny v pracovním stromu, není co commitnout", task.id)
        return

    task.status = TaskStatus.COMMITTING
    summary = task.prompt.strip().splitlines()[0][:72]
    message = f"{config.git.commit_message_prefix}{summary}\n\nTask {task.id}"
    try:
        commit_hash = git_commit(project_path, message)
        task.committed = True
        task.commit_hash = commit_hash
        logger.info("Task %s: vytvořen commit %s", task.id, commit_hash)
    except GitError as e:
        logger.warning("Task %s: commit se nepodařil: %s", task.id, e)
        task.error = ((task.error + " | ") if task.error else "") + f"Commit selhal: {e}"

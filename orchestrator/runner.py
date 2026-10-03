"""The pipeline: agent -> tests -> (fix loop) -> (review, future) -> git commit.

See ARCHITECTURE.md for the full picture. This module only orchestrates;
it has no knowledge of the CLI, the API, or how the task was submitted.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import sys
from pathlib import Path

from orchestrator.agents.base import Agent, AgentRunRequest, AgentRunResult
from orchestrator.config import Config
from orchestrator.git_utils import GitError, commit as git_commit, has_uncommitted_changes, is_git_repo
from orchestrator.models import Task, TaskStatus
from orchestrator.queue import TaskQueue

TEST_TIMEOUT_SECONDS = 900
TEST_ENV_SETUP_TIMEOUT_SECONDS = 900
MAX_LOG_TAIL_CHARS = 6000


def tail_text(text: str, limit: int = MAX_LOG_TAIL_CHARS) -> str:
    return text if len(text) <= limit else text[-limit:]


def _project_venv_paths(project_path: Path) -> tuple[Path, Path]:
    """Return the local venv if present, otherwise the managed external one.

    Generated projects often include a requirements.txt but no pre-created
    virtual environment. Keeping managed environments outside the checkout
    prevents the controller finalizer from ever staging interpreter files.
    """
    local_venv = project_path / ".venv"
    if local_venv.exists():
        venv_path = local_venv
    else:
        configured_root = os.environ.get("AI_ORCHESTRATOR_TEST_VENV_ROOT", "").strip()
        if configured_root:
            root = Path(configured_root).expanduser()
        elif os.environ.get("LOCALAPPDATA", "").strip():
            root = (
                Path(os.environ["LOCALAPPDATA"])
                / "AIProjectManager"
                / "test-venvs"
            )
        else:
            cache_root = Path(
                os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))
            )
            root = cache_root / "ai-orchestrator" / "test-venvs"
        identity = str(project_path.resolve()).casefold().encode("utf-8")
        venv_path = root / hashlib.sha256(identity).hexdigest()[:24]

    if os.name == "nt":
        python_path = venv_path / "Scripts" / "python.exe"
    else:
        python_path = venv_path / "bin" / "python"
    return venv_path, python_path


def project_test_python(project_path: Path) -> Path | None:
    """Return the prepared interpreter for a project's declared dependencies."""
    _venv_path, python_path = _project_venv_paths(project_path)
    return python_path if python_path.is_file() else None


def _prepare_project_test_environment(
    project_path: Path, logger: logging.Logger
) -> tuple[Path | None, str | None]:
    """Provision requirements.txt into an isolated project test environment.

    Projects without requirements.txt retain the legacy runner environment.
    Python projects that declare dependencies never silently borrow the
    orchestrator's packages: a managed venv is created when needed and the
    declared requirements are synchronized before each test run.
    """
    requirements = project_path / "requirements.txt"
    if not requirements.is_file():
        return None, None

    venv_path, python_path = _project_venv_paths(project_path)
    if not python_path.is_file():
        logger.info("Připravuji izolované testovací prostředí: %s", venv_path)
        try:
            created = subprocess.run(
                [sys.executable, "-m", "venv", str(venv_path)],
                cwd=str(project_path),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=TEST_ENV_SETUP_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return None, f"Virtuální prostředí projektu se nepodařilo vytvořit: {exc}"
        if created.returncode != 0 or not python_path.is_file():
            output = (created.stdout or "") + (
                "\n" + created.stderr if created.stderr else ""
            )
            return None, (
                "Virtuální prostředí projektu se nepodařilo vytvořit.\n"
                + output.strip()
            )

    install_env = os.environ.copy()
    scripts_path = str(python_path.parent)
    install_env["PATH"] = os.pathsep.join(
        [scripts_path, install_env.get("PATH", "")]
    )
    logger.info("Synchronizuji projektové závislosti z %s", requirements)
    try:
        installed = subprocess.run(
            [
                str(python_path),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "-r",
                str(requirements),
            ],
            cwd=str(project_path),
            env=install_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TEST_ENV_SETUP_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"Závislosti projektu se nepodařilo připravit: {exc}"
    if installed.returncode != 0:
        output = (installed.stdout or "") + (
            "\n" + installed.stderr if installed.stderr else ""
        )
        return None, "Instalace requirements.txt selhala.\n" + output.strip()
    return python_path.parent, None


def run_test_command(project_path: Path, test_command: str, logger: logging.Logger) -> tuple[bool, str]:
    """Run the project's test command and report a definite pass/fail.

    Always returns an actual bool for "passed" - never None - so callers
    (notably the autonomous loop) can rely on `tests_passed is True/False`
    being a real, verified result whenever a test command was configured,
    instead of having to treat "no result" as a silently-skipped state.
    """
    logger.info("Spouštím testy: %s", test_command)
    env = os.environ.copy()
    project_venv_scripts, setup_error = _prepare_project_test_environment(
        project_path, logger
    )
    if setup_error:
        return False, setup_error
    project_scripts = project_venv_scripts or (project_path / ".venv" / "Scripts")
    interpreter_scripts = Path(sys.executable).resolve().parent
    # Test commands are part of the target project's contract.  On Windows
    # the project interpreter must win over the system Python or WindowsApps
    # alias, otherwise a valid command can look like a test failure before the
    # project test suite even starts.  Temporary/test projects without their
    # own venv use the interpreter that launched this orchestrator.
    preferred_scripts = (
        [str(project_scripts)] if project_scripts.is_dir() else []
    ) + [str(interpreter_scripts)]
    env["PATH"] = os.pathsep.join(
        [*preferred_scripts, env.get("PATH", "")]
    )
    try:
        proc = subprocess.run(
            test_command,
            shell=True,
            cwd=str(project_path),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TEST_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return False, f"Testy nedoběhly do {TEST_TIMEOUT_SECONDS}s (timeout)."
    except OSError as e:
        return False, f"Testovací příkaz se nepodařilo spustit: {e}"
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


def _record_permission_denials(task: Task, result: AgentRunResult, logger: logging.Logger) -> None:
    """Accumulate one agent.run() call's denied actions onto the task, and
    log the concrete denied commands (not just the count) so it's visible
    without having to open the outbox JSON. Also surfaces how many repeated
    test-invocation attempts the PreToolUse circuit breaker short-circuited
    (see orchestrator/hooks/test_command_guard.py) - both are logged the
    same way so neither is silently buried in a JSON file."""
    if result.permission_denials:
        task.permission_denials += result.permission_denials
        task.permission_denial_details.extend(result.permission_denial_details)
        logger.warning(
            "Task %s: agent narazil na %s zamítnutou akci(í) kvůli oprávněním: %s",
            task.id, result.permission_denials, result.permission_denial_details,
        )
    if result.breaker_saved_attempts:
        task.breaker_saved_attempts += result.breaker_saved_attempts
        logger.info(
            "Task %s: circuit breaker ušetřil %s opakovaných pokusů o spuštění testů (agent je "
            "po prvním zamítnutí nezkoušel opakovat jinou variantou příkazu).",
            task.id, result.breaker_saved_attempts,
        )


def run_task(task: Task, config: Config, agent: Agent, queue: TaskQueue, logger: logging.Logger) -> Task:
    project_path = Path(task.project_path)
    # task.preexisting_dirty is set by OrchestratorService.submit() from a
    # snapshot taken BEFORE it calls ensure_project_claude_settings() - that
    # call writes .claude/settings.local.json into a project that doesn't
    # have one yet, which would itself make the tree look "dirty" here and
    # make every first-ever task against a project silently lose its commit.
    # Falls back to computing it directly for callers that build a Task
    # without going through submit() (e.g. tests).
    preexisting_dirty = (
        task.preexisting_dirty
        if task.preexisting_dirty is not None
        else is_git_repo(project_path) and has_uncommitted_changes(project_path)
    )
    if preexisting_dirty:
        logger.warning(
            "Task %s: projekt byl dirty u? p?ed startem; auto-commit je pro tento b?h zak?z?n, "
            "aby orchestr?tor nep?ibral ciz? rozpracovan? zm?ny.",
            task.id,
        )
    task.status = TaskStatus.RUNNING
    queue.update(task)
    logger.info("Task %s: spouštím agenta '%s' na projektu %s", task.id, task.agent, project_path)

    result = agent.run(
        AgentRunRequest(
            project_path=project_path,
            prompt=task.prompt,
            caller="orchestrator.runner.run_task",
            source=task.source,
            requested_model=task.requested_model,
            selection_reason=task.selection_reason,
        )
    )
    task.claude_session_id = result.session_id
    task.cost_usd = result.cost_usd
    task.model = result.model
    task.model_source = result.model_source
    task.selection_reason = result.selection_reason
    _record_permission_denials(task, result, logger)

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
                    caller="orchestrator.runner.run_task.fix",
                    source=task.source,
                    session_id=task.claude_session_id,
                    requested_model=task.requested_model,
                    selection_reason=task.selection_reason,
                )
            )
            task.attempts += 1
            task.claude_session_id = fix_result.session_id or task.claude_session_id
            task.model = fix_result.model or task.model
            task.model_source = fix_result.model_source or task.model_source
            task.selection_reason = fix_result.selection_reason or task.selection_reason
            _record_permission_denials(task, fix_result, logger)
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

    _maybe_commit(task, project_path, config, logger, preexisting_dirty=preexisting_dirty)

    task.status = TaskStatus.DONE
    queue.update(task)
    logger.info("Task %s: hotovo (committed=%s)", task.id, task.committed)
    return task


def _maybe_commit(
    task: Task,
    project_path: Path,
    config: Config,
    logger: logging.Logger,
    preexisting_dirty: bool = False,
) -> None:
    # `task.auto_commit_requested` is already the fully-resolved per-task
    # decision (OrchestratorService.submit() falls back to
    # `config.git.auto_commit` only when the caller did not explicitly pass
    # `auto_commit=...` - see submit()). ANDing with `config.git.auto_commit`
    # again here used to make an explicit per-task approval (auto_commit=True
    # passed by a caller, e.g. via the API or the CLI --commit flag) silently
    # no-op whenever the global config default was False - the ambient
    # default and the deliberate override must never fight each other.
    if not task.auto_commit_requested:
        return
    if preexisting_dirty:
        logger.info(
            "Task %s: commit se p?eskakuje, proto?e pracovn? strom obsahoval zm?ny u? p?ed startem b?hu.",
            task.id,
        )
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

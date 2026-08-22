import logging
from pathlib import Path

from orchestrator.agents.base import Agent, AgentRunResult
from orchestrator.config import Config, GitConfig
from orchestrator.models import TaskStatus
from orchestrator.queue import TaskQueue, make_task
from orchestrator.runner import run_task

LOGGER = logging.getLogger("test")


class FakeAgent(Agent):
    name = "fake"

    def __init__(self, results):
        self._results = list(results)

    def is_available(self):
        return True, "fake agent always available"

    def run(self, request):
        return self._results.pop(0)


def make_cfg(tmp_path: Path, auto_commit: bool = False) -> Config:
    cfg = Config()
    cfg.git = GitConfig(auto_commit=auto_commit)
    return cfg


def test_run_task_no_tests_marks_done(tmp_path):
    queue = TaskQueue(tmp_path / "tasks.db")
    task = make_task("demo", str(tmp_path), "dej mi ahoj", "fake", None, 2, False)
    queue.add(task)

    agent = FakeAgent([AgentRunResult(success=True, output_text="hotovo")])
    result = run_task(task, make_cfg(tmp_path), agent, queue, LOGGER)

    assert result.status == TaskStatus.DONE
    assert result.tests_passed is None
    assert result.committed is False


def test_run_task_agent_error_marks_error(tmp_path):
    queue = TaskQueue(tmp_path / "tasks.db")
    task = make_task("demo", str(tmp_path), "dej mi ahoj", "fake", None, 2, False)
    queue.add(task)

    agent = FakeAgent([AgentRunResult(success=False, output_text="", error="boom")])
    result = run_task(task, make_cfg(tmp_path), agent, queue, LOGGER)

    assert result.status == TaskStatus.ERROR
    assert result.error == "boom"


def test_run_task_tests_fail_then_fixed(tmp_path):
    queue = TaskQueue(tmp_path / "tasks.db")
    # "test command" that fails once then succeeds, driven by a marker file
    marker = tmp_path / "fixed.marker"
    test_cmd = (
        f'python -c "import sys, pathlib; '
        f"sys.exit(0 if pathlib.Path(r'{marker}').exists() else 1)\""
    )
    task = make_task("demo", str(tmp_path), "oprav to", "fake", test_cmd, 2, False)
    queue.add(task)

    agent = FakeAgent([])
    calls = {"n": 0}

    def run_side_effect(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return AgentRunResult(success=True, output_text="prvni pokus")
        marker.write_text("fixed", encoding="utf-8")
        return AgentRunResult(success=True, output_text="opraveno")

    agent.run = run_side_effect

    result = run_task(task, make_cfg(tmp_path), agent, queue, LOGGER)

    assert result.status == TaskStatus.DONE
    assert result.tests_passed is True
    assert result.attempts == 2


def test_run_task_tests_exhaust_attempts_marks_failed(tmp_path):
    queue = TaskQueue(tmp_path / "tasks.db")
    test_cmd = 'python -c "import sys; sys.exit(1)"'
    task = make_task("demo", str(tmp_path), "oprav to", "fake", test_cmd, 1, False)
    queue.add(task)

    agent = FakeAgent(
        [
            AgentRunResult(success=True, output_text="pokus 1"),
            AgentRunResult(success=True, output_text="pokus 2 (fix)"),
        ]
    )
    result = run_task(task, make_cfg(tmp_path), agent, queue, LOGGER)

    assert result.status == TaskStatus.FAILED
    assert result.committed is False


def test_run_task_records_permission_denial_details(tmp_path):
    queue = TaskQueue(tmp_path / "tasks.db")
    task = make_task("demo", str(tmp_path), "dej mi ahoj", "fake", None, 2, False)
    queue.add(task)

    denials = [{"tool_name": "Bash", "tool_input": {"command": "git push --force"}}]
    agent = FakeAgent(
        [AgentRunResult(success=True, output_text="hotovo", permission_denials=1, permission_denial_details=denials)]
    )
    result = run_task(task, make_cfg(tmp_path), agent, queue, LOGGER)

    assert result.permission_denials == 1
    assert result.permission_denial_details == denials


def test_run_task_records_breaker_saved_attempts(tmp_path):
    # The PreToolUse circuit breaker (orchestrator/hooks/test_command_guard.py)
    # short-circuits repeated test-invocation attempts after the first
    # denial and reports how many it saved via AgentRunResult - run_task
    # must accumulate that onto the task, same as permission_denials.
    queue = TaskQueue(tmp_path / "tasks.db")
    task = make_task("demo", str(tmp_path), "dej mi ahoj", "fake", None, 2, False)
    queue.add(task)

    agent = FakeAgent([AgentRunResult(success=True, output_text="hotovo", breaker_saved_attempts=14)])
    result = run_task(task, make_cfg(tmp_path), agent, queue, LOGGER)

    assert result.breaker_saved_attempts == 14


def test_run_task_commits_when_enabled_and_tests_pass(tmp_path, git_repo):
    queue = TaskQueue(tmp_path / "tasks.db")
    (git_repo / "changed.txt").write_text("nova zmena\n", encoding="utf-8")

    task = make_task("demo", str(git_repo), "uprav soubor", "fake", None, 2, True)
    queue.add(task)

    agent = FakeAgent([AgentRunResult(success=True, output_text="hotovo")])
    result = run_task(task, make_cfg(tmp_path, auto_commit=True), agent, queue, LOGGER)

    assert result.status == TaskStatus.DONE
    assert result.committed is True
    assert result.commit_hash


def test_run_task_never_commits_when_tests_fail(tmp_path, git_repo):
    queue = TaskQueue(tmp_path / "tasks.db")
    (git_repo / "changed.txt").write_text("nova zmena\n", encoding="utf-8")

    test_cmd = 'python -c "import sys; sys.exit(1)"'
    task = make_task("demo", str(git_repo), "uprav soubor", "fake", test_cmd, 0, True)
    queue.add(task)

    agent = FakeAgent([AgentRunResult(success=True, output_text="hotovo")])
    result = run_task(task, make_cfg(tmp_path, auto_commit=True), agent, queue, LOGGER)

    assert result.status == TaskStatus.FAILED
    assert result.committed is False

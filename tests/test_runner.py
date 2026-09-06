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


def test_run_task_forwards_requested_model_and_records_receipt(tmp_path):
    queue = TaskQueue(tmp_path / "tasks.db")
    task = make_task(
        "demo", str(tmp_path), "dej mi ahoj", "fake", None, 2, False,
        requested_model="claude-opus-4-1", selection_reason="explicit_agent",
    )
    queue.add(task)

    captured = {}

    class RecordingAgent(Agent):
        name = "fake"

        def is_available(self):
            return True, "ok"

        def run(self, request):
            captured["requested_model"] = request.requested_model
            captured["selection_reason"] = request.selection_reason
            return AgentRunResult(
                success=True,
                output_text="hotovo",
                model="claude-opus-4-1",
                model_source="reported",
                selection_reason="explicit_agent",
            )

    result = run_task(task, make_cfg(tmp_path), RecordingAgent(), queue, LOGGER)

    assert captured["requested_model"] == "claude-opus-4-1"
    assert captured["selection_reason"] == "explicit_agent"
    assert result.model == "claude-opus-4-1"
    assert result.model_source == "reported"
    assert result.selection_reason == "explicit_agent"


def test_run_task_forwards_requested_model_through_fix_attempts(tmp_path):
    queue = TaskQueue(tmp_path / "tasks.db")
    test_cmd = 'python -c "import sys; sys.exit(1)"'
    task = make_task(
        "demo", str(tmp_path), "oprav to", "fake", test_cmd, 1, False,
        requested_model="claude-opus-4-1",
    )
    queue.add(task)

    requested_models_seen = []

    class RecordingAgent(Agent):
        name = "fake"

        def is_available(self):
            return True, "ok"

        def run(self, request):
            requested_models_seen.append(request.requested_model)
            return AgentRunResult(success=True, output_text="pokus", model="claude-opus-4-1", model_source="reported")

    result = run_task(task, make_cfg(tmp_path), RecordingAgent(), queue, LOGGER)

    assert result.status == TaskStatus.FAILED
    # First attempt + at least one fix attempt, all carrying the same
    # per-task requested_model override.
    assert len(requested_models_seen) >= 2
    assert all(model == "claude-opus-4-1" for model in requested_models_seen)


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

    task = make_task("demo", str(git_repo), "uprav soubor", "fake", None, 2, True)
    queue.add(task)

    class WritingAgent:
        def run(self, request):
            (git_repo / "changed.txt").write_text("nova zmena\n", encoding="utf-8")
            return AgentRunResult(success=True, output_text="hotovo")

    result = run_task(
        task,
        make_cfg(tmp_path, auto_commit=True),
        WritingAgent(),
        queue,
        LOGGER,
    )

    assert result.status == TaskStatus.DONE
    assert result.committed is True
    assert result.commit_hash


def test_run_task_does_not_commit_preexisting_dirty_tree(tmp_path, git_repo):
    queue = TaskQueue(tmp_path / "tasks.db")

    # Tato zm?na existuje u? P?ED startem ?kolu a orchestr?tor ji nesm?
    # p?ivlastnit sv?mu commitu.
    (git_repo / "preexisting.txt").write_text("rozpracovana prace\n", encoding="utf-8")

    task = make_task("demo", str(git_repo), "nic nemen", "fake", None, 2, True)
    queue.add(task)

    agent = FakeAgent([AgentRunResult(success=True, output_text="hotovo")])
    result = run_task(
        task,
        make_cfg(tmp_path, auto_commit=True),
        agent,
        queue,
        LOGGER,
    )

    assert result.status == TaskStatus.DONE
    assert result.committed is False
    assert result.commit_hash is None
    assert (git_repo / "preexisting.txt").exists()


def test_run_task_commits_when_task_explicitly_requests_it_even_if_config_default_is_off(tmp_path, git_repo):
    # Regression: task.auto_commit_requested (an explicit per-task approval,
    # e.g. via the CLI --commit flag or the API auto_commit param) used to be
    # ANDed with config.git.auto_commit in _maybe_commit, so an explicit
    # approval silently did nothing whenever the global config default was
    # False - the only way to ever get a commit was to flip the global
    # switch for every future task. auto_commit_requested=True must be
    # sufficient on its own once tests pass.
    queue = TaskQueue(tmp_path / "tasks.db")
    task = make_task("demo", str(git_repo), "uprav soubor", "fake", None, 2, True)
    queue.add(task)

    class WritingAgent:
        def run(self, request):
            (git_repo / "changed.txt").write_text("nova zmena\n", encoding="utf-8")
            return AgentRunResult(success=True, output_text="hotovo")

    result = run_task(
        task,
        make_cfg(tmp_path, auto_commit=False),
        WritingAgent(),
        queue,
        LOGGER,
    )

    assert result.status == TaskStatus.DONE
    assert result.committed is True
    assert result.commit_hash


def test_run_task_does_not_commit_when_task_did_not_request_it_even_if_config_default_is_on(tmp_path, git_repo):
    # Mirror case: an explicit per-task opt-out (auto_commit_requested=False,
    # e.g. CLI --no-commit) must still be honored even when the global
    # config default is True - the ban on unsolicited commits is per-task,
    # not overridable by the ambient default in the other direction either.
    queue = TaskQueue(tmp_path / "tasks.db")
    task = make_task("demo", str(git_repo), "uprav soubor", "fake", None, 2, False)
    queue.add(task)

    class WritingAgent:
        def run(self, request):
            (git_repo / "changed.txt").write_text("nova zmena\n", encoding="utf-8")
            return AgentRunResult(success=True, output_text="hotovo")

    result = run_task(
        task,
        make_cfg(tmp_path, auto_commit=True),
        WritingAgent(),
        queue,
        LOGGER,
    )

    assert result.status == TaskStatus.DONE
    assert result.committed is False
    assert result.commit_hash is None


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

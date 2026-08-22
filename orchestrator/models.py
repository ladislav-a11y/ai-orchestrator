"""Core data model shared by the queue, runner, CLI and API."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    TESTING = "testing"
    FIXING = "fixing"
    COMMITTING = "committing"
    DONE = "done"
    FAILED = "failed"
    ERROR = "error"

    @classmethod
    def is_terminal(cls, status: "TaskStatus") -> bool:
        return status in (cls.DONE, cls.FAILED, cls.ERROR)


@dataclass
class Task:
    """One unit of work submitted to the orchestrator.

    Field names intentionally match the minimum set requested by the user:
    id, created_at, project, prompt (zadani), status, result, agent, error.
    Extra fields exist to make the pipeline (agent -> tests -> commit) auditable.
    """

    id: str
    created_at: str
    project: str
    project_path: str
    prompt: str
    agent: str = "claude-code"
    status: TaskStatus = TaskStatus.PENDING
    result: Optional[str] = None
    error: Optional[str] = None

    updated_at: Optional[str] = None
    attempts: int = 0
    max_fix_attempts: int = 0
    test_command: Optional[str] = None
    test_output: Optional[str] = None
    tests_passed: Optional[bool] = None
    auto_commit_requested: bool = True
    committed: bool = False
    commit_hash: Optional[str] = None
    log_file: Optional[str] = None
    claude_session_id: Optional[str] = None
    cost_usd: Optional[float] = None
    source: str = "cli"  # cli | api | inbox

    # Count of tool calls the agent wanted to make but were denied by the
    # permission system, and the denied actions themselves (see
    # AgentRunResult in orchestrator/agents/base.py) - accumulated across
    # every agent.run() call for this task (initial run + any fix attempts).
    permission_denials: int = 0
    permission_denial_details: list[dict[str, Any]] = field(default_factory=list)

    # How many repeated test-invocation attempts (pytest/python/unittest/cmd
    # variants after the first denial) the orchestrator's own PreToolUse
    # hook (orchestrator/hooks/test_command_guard.py) short-circuited for
    # this task - accumulated across every agent.run() call, same as
    # permission_denials above.
    breaker_saved_attempts: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["status"] = self.status.value if isinstance(self.status, TaskStatus) else self.status
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Task":
        d = dict(d)
        status = d.get("status", TaskStatus.PENDING.value)
        d["status"] = TaskStatus(status) if not isinstance(status, TaskStatus) else status
        return Task(**d)

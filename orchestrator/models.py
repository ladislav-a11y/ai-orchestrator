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
    WAITING_FOR_PROVIDER = "waiting_for_provider"
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

    # Explicit per-task model/provider selection contract (see
    # orchestrator/agents/base.py's AgentRunRequest.requested_model/
    # selection_reason and PROVIDER_MODEL_ROUTING_RESEARCH.md ch.6). Caller
    # (API/CLI/Inbox) may set requested_model/selection_reason before the
    # task runs; run_task() fills model/model_source/selection_reason from
    # the agent's actual AgentRunResult afterwards, so the outbox receipt for
    # a plain run/import-inbox task carries the same active_provider(agent)/
    # active_model(model)/selection_reason triple the autonomous outbox
    # already exposes, closing the asymmetry described there.
    requested_model: Optional[str] = None
    selection_reason: Optional[str] = None
    model: Optional[str] = None
    model_source: Optional[str] = None

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

    # Fields for persistent waiting and autonomous retry/resume
    retry_at: Optional[str] = None
    retry_after_seconds: Optional[float] = None
    goal: Optional[str] = None
    spec_text: Optional[str] = None
    is_autonomous: bool = False
    max_iterations: Optional[int] = None
    # External run identifier (e.g. the Trello card id AI Project Manager
    # passed via `--run-id`). Preserved across a WAITING_FOR_PROVIDER ->
    # resume cycle so the resumed run's outbox/autonomous-<run_id>.json
    # overwrites the SAME file the external caller is watching, instead of
    # a fresh random id it would never see.
    run_id: Optional[str] = None

    # Snapshot of "did this project's Git tree already have uncommitted
    # changes before the orchestrator touched it at all", taken by
    # OrchestratorService.submit()/run_autonomous() BEFORE they call
    # ensure_project_claude_settings() - see runner.py's run_task() and
    # autonomous.py's run_autonomous_loop(), which use this instead of
    # recomputing it later (recomputing after that call would always see the
    # settings file the orchestrator itself just wrote and misreport "dirty"
    # on every first-ever run against a project). None means "not captured
    # by the caller" - run_task()/run_autonomous_loop() fall back to
    # computing it themselves.
    preexisting_dirty: Optional[bool] = None

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

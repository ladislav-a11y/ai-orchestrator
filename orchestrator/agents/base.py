"""Abstract interface every AI agent/provider must implement.

Keep this surface small and provider-agnostic so a future OpenAI Codex
agent (or a review agent) can be added by writing one new class, without
touching the queue, runner, CLI, or API.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class AgentRunRequest:
    project_path: Path
    prompt: str
    # Extra instructions appended for a fix-attempt (e.g. failing test output).
    # None on the first attempt.
    context: Optional[str] = None
    session_id: Optional[str] = None  # to resume a previous run of the same task


@dataclass
class AgentRunResult:
    success: bool
    output_text: str
    raw_response: Optional[dict] = None
    session_id: Optional[str] = None
    cost_usd: Optional[float] = None
    error: Optional[str] = None
    # Count of tool calls the agent wanted to make but were denied by the
    # permission system (e.g. sandboxed Bash). Kept out of `output_text` -
    # any caller parsing a strict contract out of the agent's own message
    # (see autonomous.py) must only ever see what the agent itself said.
    permission_denials: int = 0
    # The denied actions themselves (tool name/input etc.), exactly as
    # reported by the agent's own raw response - so a human/log can see
    # *which* commands were denied, not just how many. Same rationale as
    # `permission_denials` for staying out of `output_text`.
    permission_denial_details: list[dict[str, Any]] = field(default_factory=list)
    # How many repeated test-invocation attempts (pytest/python/unittest/cmd
    # variants tried again after the first permission denial for that
    # command class) the orchestrator's own PreToolUse hook
    # (orchestrator/hooks/test_command_guard.py) short-circuited during this
    # run, instead of letting the agent keep retrying - see
    # ClaudeCodeAgent.run() and claude_settings.py's `hooks.PreToolUse`.
    breaker_saved_attempts: int = 0


class Agent(ABC):
    """Base class for any implementation agent (Claude Code, Codex, ...)."""

    name: str = "base-agent"

    @abstractmethod
    def is_available(self) -> tuple[bool, str]:
        """Return (available, message). Must never raise."""
        raise NotImplementedError

    @abstractmethod
    def run(self, request: AgentRunRequest) -> AgentRunResult:
        """Execute the agent on the given project. Must never raise for
        expected failure modes (agent error, timeout, non-zero exit) -
        those should come back as AgentRunResult(success=False, error=...).
        Only truly unexpected programming errors should raise.
        """
        raise NotImplementedError

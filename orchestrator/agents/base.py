"""Abstract interface every AI agent/provider must implement.

Keep this surface small and provider-agnostic so a future OpenAI Codex
agent (or a review agent) can be added by writing one new class, without
touching the queue, runner, CLI, or API.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


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

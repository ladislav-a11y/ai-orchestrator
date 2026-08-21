"""Registry mapping agent names to concrete Agent implementations.

To add a new provider (e.g. OpenAI Codex) later:
  1. write orchestrator/agents/codex.py implementing Agent (see base.py)
  2. add one line here: "codex": lambda cfg: CodexAgent(cfg.codex)
Nothing else in the orchestrator needs to change.
"""

from __future__ import annotations

from orchestrator.agents.base import Agent
from orchestrator.agents.claude_code import ClaudeCodeAgent
from orchestrator.config import Config


def build_agent(name: str, config: Config) -> Agent:
    if name == "claude-code":
        return ClaudeCodeAgent(config.claude_code)
    raise ValueError(
        f"Neznámý agent '{name}'. Zatím je implementován pouze 'claude-code'."
    )


AVAILABLE_AGENTS = ["claude-code"]  # extend when a new Agent subclass is added

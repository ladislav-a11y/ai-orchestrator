"""Registry mapping agent names to concrete Agent implementations.

To add a new provider (e.g. OpenAI Codex) later:
  1. write orchestrator/agents/codex.py implementing Agent (see base.py)
  2. add one line here: "codex": lambda cfg: CodexAgent(cfg.codex)
Nothing else in the orchestrator needs to change.
"""

from __future__ import annotations

from orchestrator.agents.antigravity import AntigravityAgent
from orchestrator.agents.base import Agent
from orchestrator.agents.claude_code import ClaudeCodeAgent
from orchestrator.agents.codex import CodexAgent
from orchestrator.agents.hermes import HermesAgent
from orchestrator.agents.gemini import GeminiAgent
from orchestrator.config import AVAILABLE_AGENTS, Config


def build_agent(name: str, config: Config) -> Agent:
    if name == "claude-code":
        return ClaudeCodeAgent(config.claude_code)
    if name == "antigravity":
        return AntigravityAgent(config.antigravity)
    if name == "codex":
        return CodexAgent(config.codex)
    if name == "hermes":
        return HermesAgent(config.hermes)
    if name == "gemini":
        return GeminiAgent(config.gemini)
    if name in ("auto", "failover"):
        from orchestrator.agents.failover import build_failover_agent
        return build_failover_agent(config)
    raise ValueError(
        f"Neznámý agent '{name}'. Podporované jsou: {', '.join(AVAILABLE_AGENTS)}."
    )


def build_failover_agent(
    config: Config,
    provider_order: list[str] | None = None,
    logger=None,
    agent_builder=None,
) -> Agent:
    from orchestrator.agents.failover import build_failover_agent as _build
    return _build(
        config,
        provider_order=provider_order,
        logger=logger,
        agent_builder=agent_builder,
    )

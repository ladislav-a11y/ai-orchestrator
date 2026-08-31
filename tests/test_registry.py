import sys

import pytest

from orchestrator.agents.antigravity import AntigravityAgent
from orchestrator.agents.claude_code import ClaudeCodeAgent
from orchestrator.agents.codex import CodexAgent
from orchestrator.agents.hermes import HermesAgent
from orchestrator.agents.registry import AVAILABLE_AGENTS, build_agent
from orchestrator.config import load_config

EXAMPLE = __import__("pathlib").Path(__file__).resolve().parent.parent / "config" / "config.example.yaml"


def make_config():
    cfg = load_config(EXAMPLE, create_if_missing=False)
    # Point every agent's cli_path at a real, always-present executable so
    # construction never fails on "CLI not found" - this test is only about
    # provider *selection* (registry.py -> the right class), not detection.
    cfg.claude_code.cli_path = sys.executable
    cfg.antigravity.cli_path = sys.executable
    cfg.codex.cli_path = sys.executable
    cfg.hermes.cli_path = sys.executable
    return cfg


def test_available_agents_lists_codex():
    assert "codex" in AVAILABLE_AGENTS


def test_build_agent_selects_codex_by_name():
    agent = build_agent("codex", make_config())
    assert isinstance(agent, CodexAgent)
    assert agent.name == "codex"


def test_build_agent_selects_claude_code_by_name():
    agent = build_agent("claude-code", make_config())
    assert isinstance(agent, ClaudeCodeAgent)


def test_build_agent_selects_antigravity_by_name():
    agent = build_agent("antigravity", make_config())
    assert isinstance(agent, AntigravityAgent)


def test_build_agent_selects_hermes_by_name():
    agent = build_agent("hermes", make_config())
    assert isinstance(agent, HermesAgent)
    assert agent.name == "hermes"


def test_build_agent_rejects_unknown_name():
    with pytest.raises(ValueError, match="Neznámý agent"):
        build_agent("does-not-exist", make_config())

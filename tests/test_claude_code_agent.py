import json
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator.agents.base import AgentRunRequest
from orchestrator.agents.claude_code import ClaudeCodeAgent, find_claude_cli
from orchestrator.config import ClaudeCodeAgentConfig


def make_config(**overrides) -> ClaudeCodeAgentConfig:
    base = dict(cli_path=sys.executable, permission_mode="acceptEdits")
    base.update(overrides)
    return ClaudeCodeAgentConfig(**base)


def test_rejects_bypass_permissions_at_construction():
    with pytest.raises(ValueError, match="bypassPermissions"):
        ClaudeCodeAgent(make_config(permission_mode="bypassPermissions"))


def test_find_claude_cli_missing_explicit_path():
    path, note = find_claude_cli("D:/definitely/not/a/real/path/claude.exe")
    assert path is None
    assert "neexistuje" in note


def test_command_never_contains_forbidden_flags():
    agent = ClaudeCodeAgent(make_config())
    cmd = agent._build_command(AgentRunRequest(project_path=Path("."), prompt="hello"))
    joined = " ".join(cmd)
    assert "--dangerously-skip-permissions" not in joined
    assert "bypassPermissions" not in joined
    assert "--permission-mode" in cmd
    assert "acceptEdits" in cmd


def test_run_success(monkeypatch):
    agent = ClaudeCodeAgent(make_config())

    fake_stdout = json.dumps(
        {
            "is_error": False,
            "result": "hotovo",
            "session_id": "abc123",
            "total_cost_usd": 0.01,
        }
    )

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=fake_stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = agent.run(AgentRunRequest(project_path=Path("."), prompt="udelej neco"))
    assert result.success is True
    assert result.output_text == "hotovo"
    assert result.session_id == "abc123"
    assert result.cost_usd == 0.01


def test_run_reports_agent_error(monkeypatch):
    agent = ClaudeCodeAgent(make_config())
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))

    fake_stdout = json.dumps({"is_error": True, "result": "Not logged in \u00b7 Please run /login"})

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=1, stdout=fake_stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = agent.run(AgentRunRequest(project_path=Path("."), prompt="udelej neco"))
    assert result.success is False
    assert "Not logged in" in result.error


def test_run_captures_permission_denial_details(monkeypatch):
    agent = ClaudeCodeAgent(make_config())
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))

    denials = [
        {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}},
        {"tool_name": "Bash", "tool_input": {"command": "git push --force"}},
    ]
    fake_stdout = json.dumps(
        {
            "is_error": False,
            "result": "hotovo",
            "session_id": "abc123",
            "permission_denials": denials,
        }
    )

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=fake_stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = agent.run(AgentRunRequest(project_path=Path("."), prompt="udelej neco"))
    assert result.permission_denials == 2
    assert result.permission_denial_details == denials
    # Denial detail must never leak into output_text - that would re-break
    # the autonomous loop's strict JSON contract (see ARCHITECTURE.md).
    assert "rm -rf" in str(result.permission_denial_details)
    assert "rm -rf" not in result.output_text


def test_run_prompt_forbids_agent_git_commit(monkeypatch):
    # The prompt actually sent to the CLI (built inside run(), not just
    # _build_command()) must tell the agent never to commit itself -
    # committing is the orchestrator's own job (AGENTS.md rule 11).
    agent = ClaudeCodeAgent(make_config())
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))

    captured_cmd = {}

    def fake_run(cmd, **kwargs):
        captured_cmd["cmd"] = cmd
        fake_stdout = json.dumps({"is_error": False, "result": "hotovo"})
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=fake_stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    agent.run(AgentRunRequest(project_path=Path("."), prompt="udelej neco"))

    sent_prompt = captured_cmd["cmd"][captured_cmd["cmd"].index("-p") + 1]
    assert "git commit" in sent_prompt
    assert "git status" in sent_prompt
    assert "git diff" in sent_prompt
    assert "udelej neco" in sent_prompt


def test_run_timeout(monkeypatch):
    agent = ClaudeCodeAgent(make_config(timeout_seconds=1))
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = agent.run(AgentRunRequest(project_path=Path("."), prompt="udelej neco"))
    assert result.success is False
    assert "timeout" in result.error.lower()

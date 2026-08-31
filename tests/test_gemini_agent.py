import json
import subprocess
from pathlib import Path

import pytest

from orchestrator.agents.base import AgentRunRequest
from orchestrator.agents.gemini import GeminiAgent
from orchestrator.config import GeminiAgentConfig


def make_agent(**overrides):
    values = {"cli_path": "gemini", "model": "gemini-2.5-flash"}
    values.update(overrides)
    agent = GeminiAgent(GeminiAgentConfig(**values))
    agent._cli_path = "gemini"
    return agent


def test_rejects_yolo_approval_mode():
    with pytest.raises(ValueError, match="nikdy yolo"):
        GeminiAgent(GeminiAgentConfig(approval_mode="yolo"))


def test_build_command_uses_explicit_model_and_auto_edit_without_yolo():
    agent = make_agent()
    command = agent._build_command(AgentRunRequest(Path("."), "hello"))

    assert command == [
        "gemini",
        "-p",
        "hello",
        "--output-format",
        "json",
        "--approval-mode",
        "auto_edit",
        "--model",
        "gemini-2.5-flash",
        "--skip-trust",
    ]
    assert "--yolo" not in command
    assert "-y" not in command


def test_run_success_parses_json_response(monkeypatch):
    agent = make_agent()
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["cwd"] = kwargs["cwd"]
        return subprocess.CompletedProcess(
            command,
            returncode=0,
            stdout=json.dumps({
                "response": "implemented",
                "session_id": "gemini-session-1",
                "stats": {"input_tokens": 10, "output_tokens": 4},
            }),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = agent.run(AgentRunRequest(Path("."), "do the work"))

    assert result.success is True
    assert result.output_text == "implemented"
    assert result.session_id == "gemini-session-1"
    assert result.input_tokens == 10
    assert result.output_tokens == 4
    assert result.total_tokens == 14
    assert captured["command"][1] == "-p"
    assert "do the work" in captured["command"][2]


def test_run_quota_error_is_limited(monkeypatch):
    agent = make_agent()
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            returncode=1,
            stdout=json.dumps({"error": "RESOURCE_EXHAUSTED: quota exceeded"}),
            stderr="",
        ),
    )

    result = agent.run(AgentRunRequest(Path("."), "do the work"))

    assert result.success is False
    assert result.limited is True
    assert "LIMITED" in result.error


def test_run_unsupported_cli_account_is_unavailable_not_limited(monkeypatch):
    agent = make_agent()
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            returncode=1,
            stdout="",
            stderr="Error authenticating: IneligibleTierError UNSUPPORTED_CLIENT",
        ),
    )

    result = agent.run(AgentRunRequest(Path("."), "do the work"))

    assert result.success is False
    assert result.unavailable is True
    assert result.limited is False


def test_run_timeout_is_distinct_from_quota(monkeypatch):
    agent = make_agent(timeout_seconds=1)
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))

    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 1)

    monkeypatch.setattr(subprocess, "run", timeout)
    result = agent.run(AgentRunRequest(Path("."), "do the work"))

    assert result.success is False
    assert result.timed_out is True
    assert result.limited is False

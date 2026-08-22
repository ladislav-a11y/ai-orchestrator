import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator.agents.antigravity import AntigravityAgent, find_antigravity_cli
from orchestrator.agents.base import AgentRunRequest
from orchestrator.config import AntigravityAgentConfig

LIVE_ENV_VAR = "AI_ORCHESTRATOR_RUN_LIVE_ANTIGRAVITY_TEST"


def make_config(**overrides) -> AntigravityAgentConfig:
    base = dict(cli_path=sys.executable, mode="accept-edits")
    base.update(overrides)
    return AntigravityAgentConfig(**base)


def test_rejects_unsafe_mode_at_construction():
    with pytest.raises(ValueError, match="antigravity.mode|povolené hodnoty"):
        AntigravityAgent(make_config(mode="dangerously-skip-permissions"))


def test_find_antigravity_cli_missing_explicit_path():
    path, note = find_antigravity_cli("D:/definitely/not/a/real/path/agy.exe")
    assert path is None
    assert "neexistuje" in note


def test_command_never_contains_forbidden_flags():
    agent = AntigravityAgent(make_config())
    cmd = agent._build_command(AgentRunRequest(project_path=Path("."), prompt="hello"))
    joined = " ".join(cmd)
    assert "--dangerously-skip-permissions" not in joined
    assert "--allow-dangerously-skip-permissions" not in joined


def test_command_uses_print_and_json_output_format():
    agent = AntigravityAgent(make_config())
    cmd = agent._build_command(AgentRunRequest(project_path=Path("."), prompt="hello"))
    assert "--print" in cmd
    assert cmd[cmd.index("--print") + 1] == "hello"
    assert "--output-format" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert "--mode" in cmd
    assert cmd[cmd.index("--mode") + 1] == "accept-edits"


def test_run_success(monkeypatch):
    agent = AntigravityAgent(make_config())
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))

    fake_stdout = json.dumps(
        {
            "conversation_id": "conv-123",
            "status": "SUCCESS",
            "response": "hotovo",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "thinking_tokens": 5,
                "total_tokens": 125,
                "cache_read_tokens": 0,
            },
        }
    )

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=fake_stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = agent.run(AgentRunRequest(project_path=Path("."), prompt="udelej neco"))
    assert result.success is True
    assert result.output_text == "hotovo"
    assert result.session_id == "conv-123"
    assert result.error is None
    assert result.limited is False
    assert result.input_tokens == 100
    assert result.output_tokens == 20
    assert result.thinking_tokens == 5
    assert result.total_tokens == 125


def test_run_non_success_status_is_a_clear_error(monkeypatch):
    agent = AntigravityAgent(make_config())
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))

    fake_stdout = json.dumps(
        {
            "conversation_id": "conv-err",
            "status": "ERROR",
            "response": "",
            "error": "permission check failed for command \"rm -rf /\"",
        }
    )

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=1, stdout=fake_stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = agent.run(AgentRunRequest(project_path=Path("."), prompt="udelej neco"))
    assert result.success is False
    assert result.limited is False
    assert "permission check failed" in result.error
    assert "ERROR" in result.error


def test_run_invalid_json_reports_returncode_and_stderr(monkeypatch):
    agent = AntigravityAgent(make_config())
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, returncode=2, stdout="not json at all", stderr="boom: something broke"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = agent.run(AgentRunRequest(project_path=Path("."), prompt="udelej neco"))
    assert result.success is False
    assert "2" in result.error
    assert "boom: something broke" in result.error


def test_run_quota_error_maps_to_limited(monkeypatch):
    agent = AntigravityAgent(make_config())
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))

    fake_stdout = json.dumps(
        {
            "conversation_id": "conv-quota",
            "status": "ERROR",
            "response": "",
            "error": "RESOURCE_EXHAUSTED: generation quota has been exceeded. Please try again later.",
            "usage": {"input_tokens": 50, "output_tokens": 0, "thinking_tokens": 0, "total_tokens": 50},
        }
    )

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=1, stdout=fake_stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = agent.run(AgentRunRequest(project_path=Path("."), prompt="udelej neco"))
    assert result.success is False
    assert result.limited is True
    assert "LIMITED" in result.error
    assert result.total_tokens == 50


def test_run_quota_error_extracts_retry_after_seconds(monkeypatch):
    agent = AntigravityAgent(make_config())
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))

    fake_stdout = json.dumps(
        {
            "conversation_id": "conv-quota-retry",
            "status": "ERROR",
            "response": "",
            "error": "rate limit exceeded, please retry after 30 seconds",
        }
    )

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=1, stdout=fake_stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = agent.run(AgentRunRequest(project_path=Path("."), prompt="udelej neco"))
    assert result.limited is True
    assert result.retry_after_seconds == 30.0


def test_run_timeout(monkeypatch):
    agent = AntigravityAgent(make_config(timeout_seconds=1))
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = agent.run(AgentRunRequest(project_path=Path("."), prompt="udelej neco"))
    assert result.success is False
    assert "timeout" in result.error.lower()


def test_run_prompt_forbids_agent_git_commit(monkeypatch):
    agent = AntigravityAgent(make_config())
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))

    captured_cmd = {}

    def fake_run(cmd, **kwargs):
        captured_cmd["cmd"] = cmd
        fake_stdout = json.dumps({"status": "SUCCESS", "response": "hotovo"})
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=fake_stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    agent.run(AgentRunRequest(project_path=Path("."), prompt="udelej neco"))

    sent_prompt = captured_cmd["cmd"][captured_cmd["cmd"].index("--print") + 1]
    assert "git commit" in sent_prompt
    assert "git status" in sent_prompt
    assert "udelej neco" in sent_prompt


@pytest.mark.skipif(
    os.environ.get(LIVE_ENV_VAR) != "1",
    reason=f"pouze explicitně přes {LIVE_ENV_VAR}=1 - spouští skutečné Antigravity CLI a spotřebovává kvótu",
)
def test_live_smoke_reads_project_state_without_changes(tmp_path):
    """Optional live smoke test: real `agy` CLI, read-only prompt, no mutation.

    Never runs in normal test runs (see skipif above) - only when explicitly
    requested, and only against a throwaway tmp_path, never a real project.
    """
    from orchestrator.config import load_config

    cfg = load_config()
    agent = AntigravityAgent(cfg.antigravity)
    available, note = agent.is_available()
    assert available, note

    (tmp_path / "marker.txt").write_text("hello", encoding="utf-8")
    before = sorted(p.name for p in tmp_path.iterdir())

    result = agent.run(
        AgentRunRequest(
            project_path=tmp_path,
            prompt=(
                "Do not create, modify, or delete any files. Just reply with the "
                "single word OK to confirm you received this."
            ),
        )
    )

    after = sorted(p.name for p in tmp_path.iterdir())
    assert before == after, "live smoke test must never mutate the project directory"
    assert result.raw_response is not None

import json
import subprocess

from orchestrator.agents.base import AgentRunRequest
from orchestrator.agents.hermes import (
    HERMES_FREE_MODEL,
    HERMES_MAX_TURNS,
    HERMES_TIMEOUT_CAP_SECONDS,
    HermesAgent,
)
from orchestrator.config import HermesAgentConfig


def _agent():
    return HermesAgent(HermesAgentConfig(cli_path="hermes-test.exe"))


def _stub_git_status(monkeypatch):
    monkeypatch.setattr("orchestrator.agents.hermes._git_status", lambda path: "(clean)")


def test_hermes_command_is_nous_only_and_passes_terminal_cwd(monkeypatch, tmp_path):
    agent = _agent()
    _stub_git_status(monkeypatch)
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))
    seen = {}

    def fake_run(cmd, **kwargs):
        if cmd[-1] == "--version":
            return subprocess.CompletedProcess(cmd, 0, "0.20.6", "")
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        usage_path = cmd[cmd.index("--usage-file") + 1]
        with open(usage_path, "w", encoding="utf-8") as handle:
            json.dump({"provider": "nous", "model": HERMES_FREE_MODEL, "total_tokens": 3}, handle)
        return subprocess.CompletedProcess(cmd, 0, '{"items":[],"notes":"verified"}', "")

    monkeypatch.setattr("orchestrator.agents.hermes.subprocess.run", fake_run)
    result = agent.run(AgentRunRequest(project_path=tmp_path, prompt="do the work", output_schema={"type": "object", "required": ["items", "notes"]}))

    assert result.success is True
    assert seen["kwargs"]["cwd"] == str(tmp_path.resolve())
    assert seen["kwargs"]["env"]["TERMINAL_CWD"] == str(tmp_path.resolve())
    assert seen["cmd"][seen["cmd"].index("--provider") + 1] == "nous"
    assert seen["cmd"][seen["cmd"].index("--model") + 1] == HERMES_FREE_MODEL
    assert seen["cmd"][seen["cmd"].index("--reasoning") + 1] == "none"
    assert seen["cmd"][seen["cmd"].index("--toolsets") + 1] == "file,terminal"
    assert "--safe-mode" in seen["cmd"]
    assert seen["cmd"][seen["cmd"].index("--in") + 1] == str(tmp_path.resolve())
    assert seen["kwargs"]["env"]["HERMES_MAX_ITERATIONS"] == str(HERMES_MAX_TURNS)
    assert "opencode-free" not in seen["cmd"]
    assert seen["kwargs"]["timeout"] == HERMES_TIMEOUT_CAP_SECONDS


def test_hermes_timeout_is_capped_for_headless_pm(monkeypatch, tmp_path):
    agent = HermesAgent(HermesAgentConfig(cli_path="hermes-test.exe", timeout_seconds=600))
    _stub_git_status(monkeypatch)
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        usage_path = cmd[cmd.index("--usage-file") + 1]
        with open(usage_path, "w", encoding="utf-8") as handle:
            json.dump({"provider": "nous", "model": HERMES_FREE_MODEL}, handle)
        return subprocess.CompletedProcess(cmd, 0, '{"items":[]}', "")

    monkeypatch.setattr("orchestrator.agents.hermes.subprocess.run", fake_run)
    result = agent.run(AgentRunRequest(project_path=tmp_path, prompt="do the work"))

    assert result.success is True
    assert seen["timeout"] == HERMES_TIMEOUT_CAP_SECONDS


def test_hermes_passes_prose_to_autonomous_protocol_repair(monkeypatch, tmp_path):
    agent = _agent()
    _stub_git_status(monkeypatch)
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))

    def fake_run(cmd, **kwargs):
        usage_path = cmd[cmd.index("--usage-file") + 1]
        with open(usage_path, "w", encoding="utf-8") as handle:
            json.dump({"provider": "nous", "model": HERMES_FREE_MODEL}, handle)
        return subprocess.CompletedProcess(cmd, 0, "I completed the work.", "")

    monkeypatch.setattr("orchestrator.agents.hermes.subprocess.run", fake_run)
    result = agent.run(AgentRunRequest(project_path=tmp_path, prompt="do the work", output_schema={"type": "object", "required": ["items", "notes"]}))

    # The autonomous loop owns JSON-shape handling so it can issue one cheap
    # repair reprompt and then fail over after repeated protocol errors.
    assert result.success is True
    assert "JSON" in result.error


def test_hermes_rejects_usage_from_any_other_provider(monkeypatch, tmp_path):
    agent = _agent()
    _stub_git_status(monkeypatch)
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))

    def fake_run(cmd, **kwargs):
        usage_path = cmd[cmd.index("--usage-file") + 1]
        with open(usage_path, "w", encoding="utf-8") as handle:
            json.dump({"provider": "gemini", "model": "gemini-3.7-flash"}, handle)
        return subprocess.CompletedProcess(cmd, 0, "{}", "")

    monkeypatch.setattr("orchestrator.agents.hermes.subprocess.run", fake_run)
    result = agent.run(AgentRunRequest(project_path=tmp_path, prompt="do the work"))

    assert result.success is False
    assert "Nous-only" in result.error


def test_hermes_truncated_stream_is_failover_worthy(monkeypatch, tmp_path):
    agent = _agent()
    _stub_git_status(monkeypatch)
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))

    def fake_run(cmd, **kwargs):
        usage_path = cmd[cmd.index("--usage-file") + 1]
        with open(usage_path, "w", encoding="utf-8") as handle:
            json.dump({}, handle)
        return subprocess.CompletedProcess(
            cmd, 0, "Response truncated due to output length limit", ""
        )

    monkeypatch.setattr("orchestrator.agents.hermes.subprocess.run", fake_run)
    result = agent.run(AgentRunRequest(project_path=tmp_path, prompt="do the work"))

    assert result.success is False
    assert result.timed_out is True
    assert "stream selhal" in result.error

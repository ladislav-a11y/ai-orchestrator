import json
import subprocess

from orchestrator.agents.base import AgentRunRequest
from orchestrator.agents.hermes import HERMES_FREE_MODEL, HermesAgent
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
    assert "opencode-free" not in seen["cmd"]


def test_hermes_rejects_prose_when_json_schema_is_required(monkeypatch, tmp_path):
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

    assert result.success is False
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

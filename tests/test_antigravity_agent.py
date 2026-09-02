import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator.agents.antigravity import (
    NO_COMMIT_INSTRUCTION,
    TEST_EXECUTION_INSTRUCTION,
    AntigravityAgent,
    _detect_quota_limit,
    _extract_retry_after_seconds,
    find_antigravity_cli,
)
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


def test_find_antigravity_cli_existing_explicit_path():
    path, note = find_antigravity_cli(sys.executable)
    assert path == str(Path(sys.executable))
    assert "použita ručně nastavená cesta" in note


def test_find_antigravity_cli_from_path(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/agy" if cmd == "agy" else None)
    path, note = find_antigravity_cli("")
    assert path == "/usr/bin/agy"
    assert "nalezeno v PATH" in note


def test_find_antigravity_cli_not_found(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cmd: None)
    path, note = find_antigravity_cli("")
    assert path is None
    assert "nenalezeno v PATH" in note


def test_is_available(monkeypatch):
    agent = AntigravityAgent(make_config())

    def fake_run_ok(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="agy 1.1.19", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run_ok)
    ok, msg = agent.is_available()
    assert ok is True
    assert "agy 1.1.19" in msg

    def fake_run_fail(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="command failed")

    monkeypatch.setattr(subprocess, "run", fake_run_fail)
    ok, msg = agent.is_available()
    assert ok is False
    assert "selhalo" in msg

    agent._cli_path = None
    ok, msg = agent.is_available()
    assert ok is False


def test_command_never_contains_forbidden_flags_or_sandbox():
    agent = AntigravityAgent(make_config())
    project_path = Path("D:/test/project/path")
    cmd = agent._build_command(AgentRunRequest(project_path=project_path, prompt="hello"))
    joined = " ".join(cmd)
    assert "--dangerously-skip-permissions" not in joined
    assert "--allow-dangerously-skip-permissions" not in joined
    assert "--sandbox" not in joined


def test_command_explicitly_adds_project_dir_and_flags():
    agent = AntigravityAgent(make_config())
    project_path = Path("D:/projects/custom-repo")
    cmd = agent._build_command(AgentRunRequest(project_path=project_path, prompt="hello world"))

    # Explicitly verifies --add-dir and project_path
    assert "--add-dir" in cmd
    assert cmd[cmd.index("--add-dir") + 1] == str(project_path)

    # Verifies --print and prompt
    assert "--print" in cmd
    assert cmd[cmd.index("--print") + 1] == "hello world"

    # Verifies --output-format json
    assert "--output-format" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"

    # Verifies --mode accept-edits
    assert "--mode" in cmd
    assert cmd[cmd.index("--mode") + 1] == "accept-edits"

    # No sandbox flag
    assert "--sandbox" not in cmd


def test_command_optional_session_and_model():
    agent = AntigravityAgent(make_config(model="gemini-2.5-pro"))
    project_path = Path("D:/projects/test")
    cmd = agent._build_command(
        AgentRunRequest(
            project_path=project_path,
            prompt="do something",
            session_id="conv-xyz-789",
        )
    )

    assert "--conversation" in cmd
    assert cmd[cmd.index("--conversation") + 1] == "conv-xyz-789"
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "gemini-2.5-pro"


def test_run_success(monkeypatch):
    agent = AntigravityAgent(make_config())
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))

    fake_stdout = json.dumps(
        {
            "conversation_id": "conv-123",
            "status": "SUCCESS",
            "response": "hotovo",
            "model": "agy-task-model",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "thinking_tokens": 5,
                "total_tokens": 125,
                "cache_read_tokens": 0,
            },
        }
    )

    captured_call = {}

    def fake_run(cmd, **kwargs):
        captured_call["cmd"] = cmd
        captured_call["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=fake_stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    project_dir = Path("D:/orchestrator/target-app")
    result = agent.run(AgentRunRequest(project_path=project_dir, prompt="udelej neco"))

    assert result.success is True
    assert result.output_text == "hotovo"
    assert result.session_id == "conv-123"
    assert result.error is None
    assert result.limited is False
    assert result.input_tokens == 100
    assert result.output_tokens == 20
    assert result.thinking_tokens == 5
    assert result.total_tokens == 125
    assert result.model == "agy-task-model"

    # Explicitly verifies --add-dir in cmd and cwd passed to subprocess.run
    assert "--add-dir" in captured_call["cmd"]
    assert captured_call["cmd"][captured_call["cmd"].index("--add-dir") + 1] == str(project_dir)
    assert captured_call["kwargs"]["cwd"] == str(project_dir)


def test_run_non_success_status_is_a_clear_error(monkeypatch):
    agent = AntigravityAgent(make_config())
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))

    fake_stdout = json.dumps(
        {
            "conversation_id": "conv-err",
            "status": "ERROR",
            "response": "",
            "error": 'permission check failed for command "rm -rf /"',
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


def test_extract_retry_after_from_dict_and_regex():
    assert _extract_retry_after_seconds({"retry_after_seconds": 45}, "") == 45.0
    assert _extract_retry_after_seconds({"retryAfter": 12.5}, "") == 12.5
    assert _extract_retry_after_seconds({}, "retry in 2 minutes") == 120.0
    assert _extract_retry_after_seconds({}, "retrying again after 15 secs") == 15.0
    assert _extract_retry_after_seconds({}, "no retry info here") is None


def test_detect_quota_limit_patterns():
    for marker in ("rate limit", "RESOURCE_EXHAUSTED", "Too Many Requests", "session limit"):
        is_lim, _ = _detect_quota_limit({}, f"Error: {marker}")
        assert is_lim is True
    is_lim, _ = _detect_quota_limit({}, "Ordinary compilation error")
    assert is_lim is False


def test_run_timeout(monkeypatch):
    agent = AntigravityAgent(make_config(timeout_seconds=1))
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = agent.run(AgentRunRequest(project_path=Path("."), prompt="udelej neco"))
    assert result.success is False
    assert "timeout" in result.error.lower()
    assert result.timed_out is True


def test_run_file_not_found(monkeypatch):
    agent = AntigravityAgent(make_config())
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))

    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("agy not found")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = agent.run(AgentRunRequest(project_path=Path("."), prompt="udelej neco"))
    assert result.success is False
    assert "Nelze spustit CLI" in result.error


def test_run_cli_unavailable(monkeypatch):
    agent = AntigravityAgent(make_config())
    monkeypatch.setattr(agent, "is_available", lambda: (False, "CLI nenalezeno"))

    result = agent.run(AgentRunRequest(project_path=Path("."), prompt="udelej neco"))
    assert result.success is False
    assert result.error == "CLI nenalezeno"


def test_run_prompt_forbids_agent_git_commit_and_includes_context(monkeypatch):
    agent = AntigravityAgent(make_config())
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))

    captured_cmd = {}

    def fake_run(cmd, **kwargs):
        captured_cmd["cmd"] = cmd
        fake_stdout = json.dumps({"status": "SUCCESS", "response": "hotovo"})
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=fake_stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    agent.run(
        AgentRunRequest(
            project_path=Path("."),
            prompt="udelej neco",
            context="dodatecny kontext ukolu",
        )
    )

    sent_prompt = captured_cmd["cmd"][captured_cmd["cmd"].index("--print") + 1]
    assert "git commit" in sent_prompt
    assert "git status" in sent_prompt
    assert "udelej neco" in sent_prompt
    assert "dodatecny kontext ukolu" in sent_prompt
    assert NO_COMMIT_INSTRUCTION in sent_prompt
    assert TEST_EXECUTION_INSTRUCTION in sent_prompt


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

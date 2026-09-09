import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from orchestrator.agents import codex as codex_module
from orchestrator.agents.codex import CodexAgent, contract_fingerprint, find_codex_cli
from orchestrator.agents.base import AgentRunRequest
from orchestrator.config import CodexAgentConfig

LIVE_ENV_VAR = "AI_ORCHESTRATOR_RUN_LIVE_CODEX_TEST"


def make_config(**overrides) -> CodexAgentConfig:
    base = dict(cli_path=sys.executable, sandbox_mode="workspace-write")
    base.update(overrides)
    return CodexAgentConfig(**base)


def test_extract_retry_after_from_usage_limit_time(monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 8, 26, 0, 0, tzinfo=timezone(timedelta(hours=2)))
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(codex_module, "datetime", FixedDateTime)

    seconds = codex_module._extract_retry_after_seconds(
        {}, "You've hit your usage limit. Please try again at 4:14 AM."
    )

    assert seconds == (4 * 60 + 14) * 60


def jsonl(*events) -> str:
    return "\n".join(json.dumps(e) for e in events)


def test_rejects_unsafe_sandbox_mode_at_construction():
    with pytest.raises(ValueError, match="codex.sandbox_mode|povolené"):
        CodexAgent(make_config(sandbox_mode="danger-full-access"))


def test_find_codex_cli_missing_explicit_path():
    path, note = find_codex_cli("D:/definitely/not/a/real/path/codex.exe")
    assert path is None
    assert "neexistuje" in note


def test_contract_fingerprint_is_stable_and_looks_like_sha256():
    first = contract_fingerprint()
    second = contract_fingerprint()
    assert first == second
    assert len(first) == 64
    int(first, 16)  # raises ValueError if not valid hex


def test_contract_fingerprint_changes_with_build_command_source(monkeypatch):
    original = contract_fingerprint()

    def other_build_command(self, request, output_schema_path=None):
        return ["codex", "exec", "different-contract-shape", "-"]

    monkeypatch.setattr(CodexAgent, "_build_command", other_build_command)
    assert contract_fingerprint() != original


def test_command_never_contains_forbidden_flags():
    agent = CodexAgent(make_config())
    cmd = agent._build_command(AgentRunRequest(project_path=Path("."), prompt="hello"))
    joined = " ".join(cmd)
    assert "--dangerously-bypass-approvals-and-sandbox" not in joined
    assert "--yolo" not in joined
    assert "--dangerously-bypass-hook-trust" not in joined


def test_command_uses_exec_json_and_safe_sandbox():
    agent = CodexAgent(make_config())
    cmd = agent._build_command(AgentRunRequest(project_path=Path("."), prompt="hello"))
    assert cmd[1] == "exec"
    assert "--json" in cmd
    assert "--sandbox" not in cmd
    assert "--skip-git-repo-check" in cmd
    assert "--ask-for-approval" not in cmd
    assert "--ephemeral" not in cmd
    assert "--ignore-user-config" in cmd
    assert "--approve-for-me" in cmd
    assert cmd[-1] == "-"
    assert "hello" not in cmd
    assert "--cd" in cmd
    assert cmd[cmd.index("--cd") + 1] == "."
    assert "--output-schema" not in cmd


def test_list_models_reads_debug_catalog_from_lang_contract(monkeypatch):
    agent = CodexAgent(make_config())
    captured = {}
    payload = {
        "models": [
            {
                "slug": "gpt-5.6-sol",
                "display_name": "GPT-5.6-Sol",
                "visibility": "list",
                "supported_reasoning_levels": [{"effort": "low"}],
            },
            {
                "slug": "codex-auto-review",
                "display_name": "Codex Auto Review",
                "visibility": "hide",
            },
        ]
    }

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            cmd,
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = agent.list_models()

    assert result["state"] == "REPORTED"
    assert result["source"] == "codex debug models"
    assert [model["id"] for model in result["models"]] == [
        "gpt-5.6-sol",
        "codex-auto-review",
    ]
    assert result["models"][0]["slug"] == "gpt-5.6-sol"
    assert result["models"][0]["supported_reasoning_levels"] == [{"effort": "low"}]
    assert captured["cmd"][1:] == ["debug", "models"]
    assert captured["kwargs"]["timeout"] == 30


def test_identity_probe_reports_exact_model_match_without_fallback(monkeypatch):
    agent = CodexAgent(make_config(model="gpt-5.6-luna"))
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))
    fake_stdout = jsonl(
        {"type": "thread.started", "thread_id": "thread-match"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "gpt-5.6-luna"},
        },
        {"type": "turn.completed"},
    )

    def fake_run(cmd, **kwargs):
        assert cmd[cmd.index("--model") + 1] == "gpt-5.6-luna"
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=fake_stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = agent.probe_identity()

    assert result["available"] is True
    assert result["model"] == "gpt-5.6-luna"
    assert result["model_source"] == "reported"
    assert result["model_verification"]["status"] == "MATCH"
    assert result["model_verification"]["exact_match"] is True


def test_identity_probe_does_not_claim_requested_model_on_generic_response(monkeypatch):
    agent = CodexAgent(make_config(model="gpt-5.6-luna"))
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))
    fake_stdout = jsonl(
        {"type": "thread.started", "thread_id": "thread-mismatch"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "gpt-5"},
        },
        {"type": "turn.completed"},
    )

    def fake_run(cmd, **kwargs):
        assert cmd[cmd.index("--model") + 1] == "gpt-5.6-luna"
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=fake_stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = agent.probe_identity()

    assert result["available"] is True
    assert result["model"] == "gpt-5"
    assert result["model_source"] == "reported"
    assert result["model_verification"]["status"] == "MISMATCH"
    assert result["model_verification"]["exact_match"] is False
    assert "přesná shoda nebyla potvrzena" in result["response"]


def test_run_enforces_and_cleans_up_output_schema(monkeypatch):
    agent = CodexAgent(make_config())
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    captured_schema_path = None

    def fake_run(cmd, **kwargs):
        nonlocal captured_schema_path
        assert "--output-schema" in cmd
        captured_schema_path = Path(cmd[cmd.index("--output-schema") + 1])
        assert json.loads(captured_schema_path.read_text(encoding="utf-8")) == schema
        stdout = jsonl(
            {"type": "thread.started", "thread_id": "thread-schema"},
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": '{"ok":true}'},
            },
            {"type": "turn.completed", "usage": {}},
        )
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = agent.run(
        AgentRunRequest(project_path=Path("."), prompt="hello", output_schema=schema)
    )

    assert result.success is True
    assert result.output_text == '{"ok":true}'
    assert captured_schema_path is not None
    assert not captured_schema_path.exists()


def test_command_uses_read_only_sandbox_without_approve_for_me():
    agent = CodexAgent(make_config(sandbox_mode="read-only"))
    cmd = agent._build_command(
        AgentRunRequest(project_path=Path("."), prompt="hello")
    )
    assert "--sandbox" in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert "--approve-for-me" not in cmd
    assert "--ask-for-approval" not in cmd
    assert "--skip-git-repo-check" in cmd


def test_command_uses_skip_git_repo_check_for_workspace_write():
    agent = CodexAgent(make_config(sandbox_mode="workspace-write"))
    cmd = agent._build_command(
        AgentRunRequest(project_path=Path("."), prompt="hello")
    )
    assert "--sandbox" not in cmd
    assert "--skip-git-repo-check" in cmd
    assert "--approve-for-me" in cmd


def test_command_starts_fresh_session_when_session_id_present():
    agent = CodexAgent(make_config())
    cmd = agent._build_command(
        AgentRunRequest(project_path=Path("."), prompt="hello", session_id="sess-1")
    )
    assert cmd[1] == "exec"
    assert "resume" not in cmd
    assert "sess-1" not in cmd
    assert "--json" in cmd
    assert "--cd" in cmd
    assert cmd[cmd.index("--cd") + 1] == "."
    assert "--ignore-user-config" in cmd
    assert "--sandbox" not in cmd
    assert "--skip-git-repo-check" in cmd
    assert "--approve-for-me" in cmd
    assert "--ephemeral" not in cmd


def test_build_command_uses_requested_model_override_not_config():
    agent = CodexAgent(make_config(model="gpt-5.6"))
    cmd = agent._build_command(
        AgentRunRequest(project_path=Path("."), prompt="hello", requested_model="gpt-6-preview")
    )
    assert cmd[cmd.index("--model") + 1] == "gpt-6-preview"


def test_build_command_falls_back_to_configured_model_without_override():
    agent = CodexAgent(make_config(model="gpt-5.6"))
    cmd = agent._build_command(AgentRunRequest(project_path=Path("."), prompt="hello"))
    assert cmd[cmd.index("--model") + 1] == "gpt-5.6"


def test_run_does_not_claim_requested_model_when_provider_does_not_confirm(monkeypatch):
    agent = CodexAgent(make_config(model="gpt-5.6"))
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))

    fake_stdout = jsonl(
        {"id": "sess-123", "msg": {"type": "task_started"}},
        {"id": "sess-123", "msg": {"type": "task_complete", "last_agent_message": "hotovo"}},
    )

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=fake_stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = agent.run(
        AgentRunRequest(project_path=Path("."), prompt="udelej neco", requested_model="gpt-6-preview")
    )
    assert result.model is None
    assert result.model_source is None
    assert result.requested_model == "gpt-6-preview"
    assert result.model_verification["status"] == "UNVERIFIED"


def test_run_does_not_claim_configured_model_when_provider_does_not_confirm(monkeypatch):
    agent = CodexAgent(make_config(model="gpt-5.6"))
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))

    fake_stdout = jsonl(
        {"id": "sess-123", "msg": {"type": "task_started"}},
        {"id": "sess-123", "msg": {"type": "task_complete", "last_agent_message": "hotovo"}},
    )

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=fake_stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = agent.run(AgentRunRequest(project_path=Path("."), prompt="udelej neco"))
    assert result.model is None
    assert result.model_source is None
    assert result.requested_model == "gpt-5.6"
    assert result.model_verification["status"] == "UNVERIFIED"


def test_run_keeps_receipt_identity_separate_and_records_mismatch(monkeypatch):
    agent = CodexAgent(make_config(model="gpt-5.6-luna"))
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))
    fake_stdout = jsonl(
        {"type": "thread.started", "thread_id": "thread-receipt"},
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": '{"answer":"hotovo","model":"gpt-5-codex"}',
            },
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 10, "output_tokens": 4, "reasoning_output_tokens": 1},
        },
    )

    def fake_run(cmd, **kwargs):
        assert cmd[cmd.index("--model") + 1] == "gpt-5.6-luna"
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=fake_stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = agent.run(
        AgentRunRequest(
            project_path=Path("."),
            prompt="udelej neco",
            requested_model="gpt-5.6-luna",
            receipt_prompt="Return exactly one JSON object with answer and model.",
        )
    )

    assert result.success is True
    assert result.model == "gpt-5-codex"
    assert result.model_source == "reported_receipt"
    assert result.requested_model == "gpt-5.6-luna"
    assert result.receipt_model == "gpt-5-codex"
    assert result.model_verification["status"] == "MISMATCH"
    assert result.model_verification["metadata_model"] is None


def test_run_accepts_exact_receipt_model_as_match_without_metadata(monkeypatch):
    agent = CodexAgent(make_config(model="gpt-5.6-sol"))
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))
    fake_stdout = jsonl(
        {"type": "thread.started", "thread_id": "thread-receipt-match"},
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": '{"answer":"hotovo","model":"gpt-5.6-sol"}',
            },
        },
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 4}},
    )

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(
            cmd, returncode=0, stdout=fake_stdout, stderr=""
        ),
    )
    result = agent.run(
        AgentRunRequest(
            project_path=Path("."),
            prompt="udelej neco",
            requested_model="gpt-5.6-sol",
            receipt_prompt="Return exactly one JSON object with answer and model.",
        )
    )
    assert result.model == "gpt-5.6-sol"
    assert result.model_source == "reported_receipt"
    assert result.model_verification["status"] == "MATCH"
    assert result.model_verification["authoritative_model"] == "gpt-5.6-sol"


def test_run_metadata_model_wins_over_receipt_identity(monkeypatch):
    agent = CodexAgent(make_config(model="gpt-5.6-luna"))
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))
    fake_stdout = jsonl(
        {"type": "thread.started", "thread_id": "thread-metadata"},
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": '{"answer":"hotovo","model":"gpt-5-codex"}',
            },
        },
        {
            "type": "turn.completed",
            "model": "gpt-5.6-sol",
            "usage": {"input_tokens": 10, "output_tokens": 4},
        },
    )

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=fake_stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = agent.run(
        AgentRunRequest(
            project_path=Path("."),
            prompt="udelej neco",
            requested_model="gpt-5.6-luna",
            receipt_prompt="Return exactly one JSON object with answer and model.",
        )
    )

    assert result.model == "gpt-5.6-sol"
    assert result.model_source == "reported"
    assert result.receipt_model == "gpt-5-codex"
    assert result.model_verification["authoritative_model"] == "gpt-5.6-sol"
    assert result.model_verification["status"] == "MISMATCH"


def test_run_echoes_selection_reason_into_result(monkeypatch):
    agent = CodexAgent(make_config())
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))

    fake_stdout = jsonl(
        {"id": "sess-123", "msg": {"type": "task_started"}},
        {"id": "sess-123", "msg": {"type": "task_complete", "last_agent_message": "hotovo"}},
    )

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=fake_stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = agent.run(
        AgentRunRequest(project_path=Path("."), prompt="udelej neco", selection_reason="explicit_agent")
    )
    assert result.selection_reason == "explicit_agent"


def test_run_rejects_invalid_requested_model_safely(monkeypatch):
    agent = CodexAgent(make_config())
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))

    fake_stdout = jsonl(
        {"id": "sess-err", "msg": {"type": "task_started"}},
        {"id": "sess-err", "msg": {"type": "error", "message": "unknown model 'not-a-real-model'"}},
    )

    def fake_run(cmd, **kwargs):
        assert cmd[cmd.index("--model") + 1] == "not-a-real-model"
        return subprocess.CompletedProcess(cmd, returncode=1, stdout=fake_stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = agent.run(
        AgentRunRequest(project_path=Path("."), prompt="udelej neco", requested_model="not-a-real-model")
    )
    assert result.success is False
    assert "not-a-real-model" in result.error


def test_run_success(monkeypatch):
    agent = CodexAgent(make_config())
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))

    fake_stdout = jsonl(
        {"id": "sess-123", "model": "gpt-5.6", "msg": {"type": "task_started"}},
        {"id": "sess-123", "msg": {"type": "agent_message", "message": "pracuji"}},
        {
            "id": "sess-123",
            "msg": {
                "type": "token_count",
                "input_tokens": 100,
                "output_tokens": 20,
                "reasoning_output_tokens": 5,
                "total_tokens": 125,
            },
        },
        {"id": "sess-123", "msg": {"type": "task_complete", "last_agent_message": "hotovo"}},
    )

    def fake_run(cmd, **kwargs):
        assert cmd[-1] == "-"
        assert "udelej neco" in kwargs["input"]
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=fake_stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = agent.run(AgentRunRequest(project_path=Path("."), prompt="udelej neco"))
    assert result.success is True
    assert result.output_text == "hotovo"
    assert result.session_id == "sess-123"
    assert result.error is None
    assert result.limited is False
    assert result.input_tokens == 100
    assert result.output_tokens == 20
    assert result.thinking_tokens == 5
    assert result.total_tokens == 125
    assert result.model == "gpt-5.6"


def test_run_success_parses_current_codex_jsonl_schema(monkeypatch):
    agent = CodexAgent(make_config())
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))

    fake_stdout = jsonl(
        {"type": "thread.started", "thread_id": "thread-123"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {"id": "item-0", "type": "agent_message", "text": "OK"},
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "reasoning_output_tokens": 5,
            },
        },
    )

    def fake_run(cmd, **kwargs):
        assert cmd[-1] == "-"
        assert "udelej neco" in kwargs["input"]
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=fake_stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = agent.run(AgentRunRequest(project_path=Path("."), prompt="udelej neco"))
    assert result.success is True
    assert result.output_text == "OK"
    assert result.session_id == "thread-123"
    assert result.error is None
    assert result.input_tokens == 100
    assert result.output_tokens == 20
    assert result.thinking_tokens == 5
    # Current Codex omits total_tokens even though both exact components are
    # present; the adapter must preserve a useful exact total.
    assert result.total_tokens == 120


def test_run_incomplete_current_schema_output_is_an_error(monkeypatch):
    """Same failure mode as test_run_incomplete_output_without_task_complete_is_an_error
    (see PROJECT_HANDOVER_2026-08-25.md #3 'Codex ... při interní chybě
    patchování vrací neúplný výstup'), but for the current (0.149.x)
    thread/item/turn event schema instead of the older nested-msg schema -
    a run that streams progress but never reaches 'turn.completed' or an
    error event must still be reported as a clear failure, not silently
    treated as success with whatever partial text arrived.
    """
    agent = CodexAgent(make_config())
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))

    fake_stdout = jsonl(
        {"type": "thread.started", "thread_id": "thread-inc"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {"id": "item-0", "type": "agent_message", "text": "pracuji"},
        },
    )

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=fake_stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = agent.run(AgentRunRequest(project_path=Path("."), prompt="udelej neco"))
    assert result.success is False
    assert "neúplný výstup" in result.error
    assert result.session_id == "thread-inc"


def test_run_error_event_is_a_clear_error(monkeypatch):
    agent = CodexAgent(make_config())
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))

    fake_stdout = jsonl(
        {"id": "sess-err", "msg": {"type": "task_started"}},
        {"id": "sess-err", "msg": {"type": "error", "message": "permission denied for command \"rm -rf /\""}},
    )

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=1, stdout=fake_stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = agent.run(AgentRunRequest(project_path=Path("."), prompt="udelej neco"))
    assert result.success is False
    assert result.limited is False
    assert "permission denied" in result.error


def test_run_invalid_output_reports_returncode_and_stderr(monkeypatch):
    agent = CodexAgent(make_config())
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


def test_run_incomplete_output_without_task_complete_is_an_error(monkeypatch):
    agent = CodexAgent(make_config())
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))

    fake_stdout = jsonl(
        {"id": "sess-inc", "msg": {"type": "task_started"}},
        {"id": "sess-inc", "msg": {"type": "agent_message", "message": "pracuji"}},
    )

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=fake_stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = agent.run(AgentRunRequest(project_path=Path("."), prompt="udelej neco"))
    assert result.success is False
    assert "task_complete" in result.error


def test_run_quota_error_maps_to_limited(monkeypatch):
    agent = CodexAgent(make_config())
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))

    fake_stdout = jsonl(
        {"id": "sess-quota", "msg": {"type": "task_started"}},
        {
            "id": "sess-quota",
            "msg": {
                "type": "token_count",
                "input_tokens": 50,
                "output_tokens": 0,
                "reasoning_output_tokens": 0,
                "total_tokens": 50,
            },
        },
        {
            "id": "sess-quota",
            "msg": {
                "type": "error",
                "message": "RESOURCE_EXHAUSTED: usage limit reached. Please try again later.",
            },
        },
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
    agent = CodexAgent(make_config())
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))

    fake_stdout = jsonl(
        {"id": "sess-quota-retry", "msg": {"type": "task_started"}},
        {
            "id": "sess-quota-retry",
            "msg": {"type": "error", "message": "rate limit exceeded, please retry after 30 seconds"},
        },
    )

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=1, stdout=fake_stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = agent.run(AgentRunRequest(project_path=Path("."), prompt="udelej neco"))
    assert result.limited is True
    assert result.retry_after_seconds == 30.0


def test_run_timeout(monkeypatch):
    agent = CodexAgent(make_config(timeout_seconds=1))
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = agent.run(AgentRunRequest(project_path=Path("."), prompt="udelej neco"))
    assert result.success is False
    assert "timeout" in result.error.lower()
    assert result.timed_out is True


def test_run_prompt_forbids_agent_git_commit(monkeypatch):
    agent = CodexAgent(make_config())
    monkeypatch.setattr(agent, "is_available", lambda: (True, "ok"))

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = kwargs["input"]
        fake_stdout = jsonl({"id": "s", "msg": {"type": "task_complete", "last_agent_message": "hotovo"}})
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=fake_stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    agent.run(AgentRunRequest(project_path=Path("."), prompt="udelej neco"))

    assert captured["cmd"][-1] == "-"
    sent_prompt = captured["input"]
    assert "git commit" in sent_prompt
    assert "git status" in sent_prompt
    assert "udelej neco" in sent_prompt


@pytest.mark.skipif(
    os.environ.get(LIVE_ENV_VAR) != "1",
    reason=f"pouze explicitně přes {LIVE_ENV_VAR}=1 - spouští skutečné Codex CLI a spotřebovává kvótu",
)
def test_live_smoke_reads_project_state_without_changes(tmp_path):
    """Real CLI contract smoke: read-only, structured final JSON, no mutation.

    Never runs in normal test runs (see skipif above) - only when explicitly
    requested, and only against a throwaway tmp_path, never a real project.
    """
    from orchestrator.config import load_config

    cfg = load_config()
    cfg.codex.sandbox_mode = "read-only"
    agent = CodexAgent(cfg.codex)
    available, note = agent.is_available()
    assert available, note

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)
    (tmp_path / "marker.txt").write_text("hello", encoding="utf-8")
    before = sorted(p.name for p in tmp_path.iterdir())

    result = agent.run(
        AgentRunRequest(
            project_path=tmp_path,
            prompt=(
                "Do not create, modify, or delete any files. Inspect marker.txt, then report "
                "whether its content is hello using the required final JSON response."
            ),
            output_schema={
                "type": "object",
                "properties": {"marker_is_hello": {"type": "boolean"}},
                "required": ["marker_is_hello"],
                "additionalProperties": False,
            },
        )
    )

    after = sorted(p.name for p in tmp_path.iterdir())
    assert before == after, "live smoke test must never mutate the project directory"
    assert result.success, result.error
    assert json.loads(result.output_text) == {"marker_is_hello": True}
    assert result.raw_response is not None

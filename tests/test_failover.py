import json
import logging
from pathlib import Path

import pytest

from orchestrator.agents.base import Agent, AgentRunRequest, AgentRunResult
from orchestrator.agents.failover import FailoverAgent, ProviderStatus, build_failover_agent
from orchestrator.autonomous import AUDIT_MARKER, AutonomousStatus, parse_definition_of_done, run_autonomous_loop
from orchestrator.config import (
    ApiConfig,
    Config,
    GitConfig,
    PathsConfig,
    ProjectEntry,
    TestingConfig,
)
from orchestrator.service import OrchestratorService


class MockAgent(Agent):
    def __init__(
        self,
        name: str,
        available: bool = True,
        avail_msg: str = "ok",
        run_fn=None,
    ):
        self.name = name
        self._available = available
        self._avail_msg = avail_msg
        self._run_fn = run_fn
        self.run_calls: list[AgentRunRequest] = []

    def is_available(self):
        return self._available, self._avail_msg

    def run(self, request: AgentRunRequest):
        self.run_calls.append(request)
        if self._run_fn:
            return self._run_fn(request)
        return AgentRunResult(success=True, output_text=f"result from {self.name}", session_id=f"sess-{self.name}")


def _audit_confirming_run_fn(executor_fn):
    def run_fn(request):
        if AUDIT_MARKER in request.prompt:
            return AgentRunResult(
                success=True,
                output_text='{"rejected_indices": [], "notes": "audit passed"}',
                session_id="audit-sess",
            )
        return executor_fn(request)

    return run_fn


# -- 1. First provider succeeds ----------------------------------------------


def test_failover_first_provider_succeeds(caplog):
    caplog.set_level(logging.INFO)
    p1 = MockAgent("claude-code", available=True)
    p2 = MockAgent("antigravity", available=True)
    p3 = MockAgent("codex", available=True)

    agent = FailoverAgent([p1, p2, p3])
    result = agent.run(AgentRunRequest(project_path=Path("."), prompt="vytvor feature"))

    assert result.success is True
    assert result.output_text == "result from claude-code"
    assert len(p1.run_calls) == 1
    assert len(p2.run_calls) == 0
    assert len(p3.run_calls) == 0
    assert "Vybrán provider 'claude-code'" in caplog.text


# -- 2. First provider is LIMITED, second succeeds ---------------------------


def test_failover_first_limited_second_succeeds(caplog):
    caplog.set_level(logging.INFO)

    def p1_run(request):
        return AgentRunResult(
            success=False,
            output_text="",
            error="429 Too Many Requests - quota exceeded",
            limited=True,
            retry_after_seconds=60.0,
        )

    p1 = MockAgent("claude-code", available=True, run_fn=p1_run)
    p2 = MockAgent("antigravity", available=True)
    p3 = MockAgent("codex", available=True)

    agent = FailoverAgent([p1, p2, p3])
    result = agent.run(AgentRunRequest(project_path=Path("."), prompt="vytvor feature"))

    assert result.success is True
    assert result.output_text == "result from antigravity"
    assert len(p1.run_calls) == 1
    assert len(p2.run_calls) == 1
    assert len(p3.run_calls) == 0
    assert "Provider 'claude-code' vrátil LIMITED" in caplog.text
    assert "retry po 60.0s" in caplog.text
    assert "Přepínám na providera 'antigravity'" in caplog.text


# -- 3. First provider is unavailable, second succeeds -----------------------


def test_failover_first_unavailable_second_succeeds(caplog):
    caplog.set_level(logging.INFO)
    p1 = MockAgent("claude-code", available=False, avail_msg="claude CLI nenalezeno v PATH")
    p2 = MockAgent("antigravity", available=True)
    p3 = MockAgent("codex", available=True)

    agent = FailoverAgent([p1, p2, p3])
    result = agent.run(AgentRunRequest(project_path=Path("."), prompt="vytvor feature"))

    assert result.success is True
    assert result.output_text == "result from antigravity"
    assert len(p1.run_calls) == 0  # not run because unavailable
    assert len(p2.run_calls) == 1
    assert len(p3.run_calls) == 0
    assert "Provider 'claude-code' není lokálně dostupný" in caplog.text
    assert "přeskakuji na dalšího providera" in caplog.text
    assert "Vybrán provider 'antigravity'" in caplog.text


# -- 4. All providers are LIMITED or unavailable -----------------------------


def test_failover_all_limited_or_unavailable(caplog):
    caplog.set_level(logging.INFO)

    def p1_run(request):
        return AgentRunResult(
            success=False,
            output_text="",
            error="Rate limit reached",
            limited=True,
        )

    def p3_run(request):
        return AgentRunResult(
            success=False,
            output_text="",
            error="RESOURCE_EXHAUSTED",
            limited=True,
        )

    p1 = MockAgent("claude-code", available=True, run_fn=p1_run)
    p2 = MockAgent("antigravity", available=False, avail_msg="agy not found")
    p3 = MockAgent("codex", available=True, run_fn=p3_run)

    agent = FailoverAgent([p1, p2, p3])
    result = agent.run(AgentRunRequest(project_path=Path("."), prompt="vytvor feature"))

    assert result.success is False
    assert result.limited is True
    assert "nedostupní nebo LIMITED" in result.error
    assert len(p1.run_calls) == 1
    assert len(p2.run_calls) == 0
    assert len(p3.run_calls) == 1
    assert "Všichni konfigurovaní provideři" in caplog.text


# -- 5. Normal error does NOT trigger failover -------------------------------


def test_failover_normal_error_does_not_trigger_failover(caplog):
    caplog.set_level(logging.INFO)

    def p1_run(request):
        return AgentRunResult(
            success=False,
            output_text="syntax error in line 42",
            error="Python process failed with exit code 1",
            limited=False,
        )

    p1 = MockAgent("claude-code", available=True, run_fn=p1_run)
    p2 = MockAgent("antigravity", available=True)
    p3 = MockAgent("codex", available=True)

    agent = FailoverAgent([p1, p2, p3])
    result = agent.run(AgentRunRequest(project_path=Path("."), prompt="vytvor feature"))

    assert result.success is False
    assert result.limited is False
    assert result.error == "Python process failed with exit code 1"
    assert len(p1.run_calls) == 1
    assert len(p2.run_calls) == 0
    assert len(p3.run_calls) == 0
    assert "Přepínám na providera" not in caplog.text


# -- 6. Explicit --agent does NOT perform failover ---------------------------


def test_explicit_agent_does_not_perform_failover(tmp_path, monkeypatch):
    """When user explicitly passes --agent, service.run_autonomous must not
    wrap it in FailoverAgent, so an explicit choice never silently switches."""
    from orchestrator import service as service_module

    p1_calls = []

    class ExplicitAgent(Agent):
        name = "claude-code"

        def is_available(self):
            return True, "ok"

        def run(self, request):
            p1_calls.append(request)
            return AgentRunResult(
                success=False,
                output_text="",
                error="Quota exceeded",
                limited=True,
            )

    monkeypatch.setattr(service_module, "build_agent", lambda name, config: ExplicitAgent())

    cfg = Config(
        projects={"myproj": ProjectEntry("myproj", str(tmp_path / "workspace" / "myproj"))},
        git=GitConfig(auto_commit=False),
        testing=TestingConfig(),
        api=ApiConfig(),
        paths=PathsConfig(
            inbox=str(tmp_path / "inbox"),
            outbox=str(tmp_path / "outbox"),
            logs=str(tmp_path / "logs"),
            data=str(tmp_path / "data"),
        ),
        workspace_root=str(tmp_path / "workspace"),
    )

    service = OrchestratorService(cfg)
    run_id, result = service.run_autonomous(
        project_ref="myproj",
        goal="vytvor aplikaci",
        spec_text="- [ ] bod 1",
        agent_name="claude-code",  # explicitly chosen agent
        max_iterations=2,
    )

    # Ended with ERROR on the first iteration without failing over
    assert result.status == AutonomousStatus.ERROR
    assert len(p1_calls) == 1
    assert result.error == "Quota exceeded"


# -- 7. Autonomous loop preserves DoD and checkpoint across failover ---------


def test_failover_preserves_autonomous_checkpoint_and_dod_across_iterations(tmp_path):
    dod = parse_definition_of_done("- [ ] bod 0\n- [ ] bod 1")
    p1_calls = {"n": 0}

    def p1_run(request):
        p1_calls["n"] += 1
        if p1_calls["n"] == 1:
            # Iteration 1: p1 marks bod 0 done
            return AgentRunResult(
                success=True,
                output_text='{"items": [{"index": 0, "done": true}], "notes": "bod 0 hotov"}',
                session_id="p1-sess-1",
            )
        # Iteration 2: p1 hits rate limit
        return AgentRunResult(
            success=False,
            output_text="",
            error="429 Rate limit exceeded",
            limited=True,
        )

    def p2_run(request):
        if AUDIT_MARKER in request.prompt:
            return AgentRunResult(
                success=True,
                output_text='{"rejected_indices": [], "notes": "audit potvrzen"}',
                session_id="p2-audit-sess",
            )
        # p2 receives request with prompt containing bod 1 and marks it done
        assert "bod 1" in request.prompt
        return AgentRunResult(
            success=True,
            output_text='{"items": [{"index": 1, "done": true}], "notes": "bod 1 hotov pres p2"}',
            session_id="p2-sess-1",
        )

    p1 = MockAgent("claude-code", available=True, run_fn=p1_run)
    p2 = MockAgent("antigravity", available=True, run_fn=p2_run)

    failover_agent = FailoverAgent([p1, p2])
    cfg = Config()

    result = run_autonomous_loop(
        run_id="failover-run-123",
        project_path=tmp_path,
        goal="udelej 2 body",
        dod_items=dod,
        config=cfg,
        agent=failover_agent,
        logger=logging.getLogger("test"),
        test_command=None,
        max_iterations=3,
        auto_commit_requested=False,
    )

    assert result.status == AutonomousStatus.COMPLETED
    assert all(item.done for item in result.dod_items)
    # Provider failover m??e prob?hnout uvnit? stejn? autonomous iterace.
    # Podstatn? je zachov?n? DoD a skute?n? p?evzet? dal??m providerem.
    assert len(result.iterations) >= 1
    assert any(it.agent_name == "antigravity" for it in result.iterations)


# -- 8. Session isolation per provider ---------------------------------------


def test_failover_maintains_session_isolation_per_provider():
    p1_requests = []
    p2_requests = []

    def p1_run(request):
        p1_requests.append(request)
        if len(p1_requests) == 1:
            return AgentRunResult(success=True, output_text="p1 ok", session_id="sess-claude-99")
        return AgentRunResult(success=False, output_text="", error="Quota 429", limited=True)

    def p2_run(request):
        p2_requests.append(request)
        return AgentRunResult(success=True, output_text="p2 ok", session_id="sess-agy-77")

    p1 = MockAgent("claude-code", available=True, run_fn=p1_run)
    p2 = MockAgent("antigravity", available=True, run_fn=p2_run)

    agent = FailoverAgent([p1, p2])

    # Call 1: p1 runs, returns session "sess-claude-99"
    res1 = agent.run(AgentRunRequest(project_path=Path("."), prompt="step 1"))
    assert res1.success is True
    assert p1_requests[0].session_id is None

    # Call 2: p1 fails with limited, p2 runs with fresh session (None, not sess-claude-99)
    res2 = agent.run(AgentRunRequest(project_path=Path("."), prompt="step 2"))
    assert res2.success is True
    assert p2_requests[0].session_id is None

    # Call 3: p2 continues, using its own session "sess-agy-77"
    res3 = agent.run(AgentRunRequest(project_path=Path("."), prompt="step 3"))
    assert res3.success is True
    assert p2_requests[1].session_id == "sess-agy-77"


# -- 9. is_available reporting -----------------------------------------------


def test_failover_is_available_logic():
    p1 = MockAgent("claude-code", available=False, avail_msg="no claude")
    p2 = MockAgent("antigravity", available=True, avail_msg="agy ok")
    agent = FailoverAgent([p1, p2])

    ok, msg = agent.is_available()
    assert ok is True
    assert "antigravity" in msg

    p2._available = False
    p2._avail_msg = "no agy"
    ok, msg = agent.is_available()
    assert ok is False
    assert "Žádný provider není k dispozici" in msg


def test_failover_all_limited_preserves_earliest_retry():
    def p1_run(request):
        return AgentRunResult(
            success=False,
            output_text="",
            error="Rate limit",
            limited=True,
            retry_after_seconds=3600,
        )

    def p2_run(request):
        return AgentRunResult(
            success=False,
            output_text="",
            error="Quota limit",
            limited=True,
            retry_after_seconds=900,
        )

    p1 = MockAgent("claude-code", available=True, run_fn=p1_run)
    p2 = MockAgent("antigravity", available=True, run_fn=p2_run)

    result = FailoverAgent([p1, p2]).run(
        AgentRunRequest(project_path=Path("."), prompt="test")
    )

    assert result.success is False
    assert result.limited is True
    assert result.retry_after_seconds == 900

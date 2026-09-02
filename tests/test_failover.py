import json
import logging
import re
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


def _audit_response(request):
    indices = [int(value) for value in re.findall(r"(?m)^(\d+)\. ", request.prompt)]
    return json.dumps({
        "items": [{"index": value, "accepted": True, "evidence": "audit evidence"} for value in indices],
        "notes": "audit passed",
    })


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
                output_text=_audit_response(request),
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


def test_failover_preserves_output_contract_for_selected_provider():
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
    }
    p1 = MockAgent("claude-code", available=False)
    p2 = MockAgent("codex", available=True)
    agent = FailoverAgent([p1, p2])

    result = agent.run(
        AgentRunRequest(project_path=Path("."), prompt="audit", output_schema=schema)
    )

    assert result.success is True
    assert p2.run_calls[0].output_schema == schema


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


def test_failover_provider_run_auth_unavailable_second_succeeds(caplog):
    caplog.set_level(logging.INFO)

    p1 = MockAgent(
        "gemini",
        available=True,
        run_fn=lambda request: AgentRunResult(
            success=False,
            output_text="",
            error="UNSUPPORTED_CLIENT",
            unavailable=True,
        ),
    )
    p2 = MockAgent("antigravity", available=True)

    agent = FailoverAgent([p1, p2])
    result = agent.run(AgentRunRequest(project_path=Path("."), prompt="vytvor feature"))

    assert result.success is True
    assert len(p1.run_calls) == 1
    assert len(p2.run_calls) == 1
    assert "je nedostupný" in caplog.text


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


def test_failover_all_exhausted_sends_detailed_slack_notification(monkeypatch):
    """DoD (produkční incident cb501524e47e, 26.8.2026 - Claude LIMITED
    reset 22:10, Antigravity LIMITED reset +70h36m, Codex LIMITED reset
    22:05): the Slack message sent when every provider is exhausted must
    say the status of EACH provider individually (not just their names, as
    the old message did) and the nearest known reset/retry."""
    sent = []
    monkeypatch.setattr("orchestrator.agents.failover.notify", lambda msg: sent.append(msg))

    def p1_run(request):
        return AgentRunResult(
            success=False, output_text="", error="Rate limit reached",
            limited=True, retry_after_seconds=3600,
        )

    def p3_run(request):
        return AgentRunResult(
            success=False, output_text="", error="RESOURCE_EXHAUSTED",
            limited=True, retry_after_seconds=254160,
        )

    p1 = MockAgent("claude-code", available=True, run_fn=p1_run)
    p2 = MockAgent("antigravity", available=False, avail_msg="agy not found")
    p3 = MockAgent("codex", available=True, run_fn=p3_run)

    agent = FailoverAgent([p1, p2, p3])
    agent.run(AgentRunRequest(project_path=Path("."), prompt="vytvor feature"))

    # Other notify() calls fire along the way (active-provider announcement,
    # per-switch "provider X vyčerpal limit" - see run()'s existing
    # per-switch notifications) - the detailed exhaustion summary this test
    # cares about is always the LAST one sent, once every provider has been
    # tried and none is left.
    assert len(sent) >= 1
    message = sent[-1]
    assert "WAITING_FOR_PROVIDER" in message
    assert "claude-code: LIMITED (Rate limit reached, reset za 1h0m)" in message
    assert "antigravity: nedostupný (agy not found)" in message
    assert "codex: LIMITED (RESOURCE_EXHAUSTED, reset za 70h36m)" in message
    assert "Nejbližší známý reset/retry: za 1h0m" in message


def test_failover_all_exhausted_notification_handles_unknown_retry(monkeypatch):
    """When no provider reports a retry_after_seconds at all, the message
    must say so plainly instead of omitting the nearest-reset line."""
    sent = []
    monkeypatch.setattr("orchestrator.agents.failover.notify", lambda msg: sent.append(msg))

    p1 = MockAgent("claude-code", available=False, avail_msg="not logged in")

    agent = FailoverAgent([p1])
    agent.run(AgentRunRequest(project_path=Path("."), prompt="vytvor feature"))

    assert len(sent) == 1
    assert "claude-code: nedostupný (not logged in)" in sent[0]
    assert "Čas žádného resetu/retry není u žádného providera znám." in sent[0]


# -- 4b. force_failover_on_protocol_error() advances past the active provider

def test_force_failover_on_protocol_error_advances_and_skips_next_run(caplog):
    caplog.set_level(logging.INFO)
    p1 = MockAgent("claude-code", available=True)
    p2 = MockAgent("antigravity", available=True)

    agent = FailoverAgent([p1, p2])
    assert agent.active_provider_name == "claude-code"

    advanced = agent.force_failover_on_protocol_error("nevraci platny JSON kontrakt")

    assert advanced is True
    assert agent.active_provider_name == "antigravity"
    assert "Přepínám na" in caplog.text

    # A subsequent run() call must skip the now protocol-incompatible
    # provider entirely, not just start from wherever the index was left.
    result = agent.run(AgentRunRequest(project_path=Path("."), prompt="vytvor feature"))
    assert result.output_text == "result from antigravity"
    assert len(p1.run_calls) == 0
    assert len(p2.run_calls) == 1


def test_force_failover_on_protocol_error_returns_false_on_last_provider():
    """When the currently active provider is the last one configured,
    force_failover_on_protocol_error() must report there is nowhere left to
    go (False) so the caller (autonomous.py) stops the whole run instead of
    silently retrying the same protocol-incompatible provider again."""
    p1 = MockAgent("claude-code", available=True)
    agent = FailoverAgent([p1])

    assert agent.force_failover_on_protocol_error("nevraci platny JSON kontrakt") is False


def test_force_failover_on_audit_quality_advances_and_skips_provider(caplog):
    caplog.set_level(logging.INFO)
    p1 = MockAgent("hermes", available=True)
    p2 = MockAgent("gemini", available=True)

    agent = FailoverAgent([p1, p2])
    assert agent.force_failover_on_audit_quality("audit bez konkrétního ověření") is True
    assert agent.active_provider_name == "gemini"
    assert agent._describe_status_for_notify("hermes").startswith("hermes: AUDIT_INADEQUATE")

    result = agent.run(AgentRunRequest(project_path=Path("."), prompt="audit"))
    assert result.output_text == "result from gemini"
    assert len(p1.run_calls) == 0
    assert len(p2.run_calls) == 1
    assert "věcně nedostatečný audit" in caplog.text


def test_force_failover_on_audit_quality_returns_false_on_last_provider():
    p1 = MockAgent("hermes", available=True)
    agent = FailoverAgent([p1])

    assert agent.force_failover_on_audit_quality("audit bez konkrétního ověření") is False
    assert agent.active_provider_name == "hermes"


# -- 4c. force_failover_on_budget_exceeded() advances past the active provider
# (per-job provider-specific financial cap, see autonomous.py's
# _provider_budget_usd - mirrors force_failover_on_protocol_error above).

def test_force_failover_on_budget_exceeded_advances_and_skips_next_run(caplog):
    caplog.set_level(logging.INFO)
    p1 = MockAgent("claude-code", available=True)
    p2 = MockAgent("antigravity", available=True)

    agent = FailoverAgent([p1, p2])
    assert agent.active_provider_name == "claude-code"

    advanced = agent.force_failover_on_budget_exceeded("prekrocen max_budget_usd")

    assert advanced is True
    assert agent.active_provider_name == "antigravity"
    assert "Přepínám na" in caplog.text

    # A subsequent run() call must skip the now budget-exceeded provider
    # entirely, not just start from wherever the index was left.
    result = agent.run(AgentRunRequest(project_path=Path("."), prompt="vytvor feature"))
    assert result.output_text == "result from antigravity"
    assert len(p1.run_calls) == 0
    assert len(p2.run_calls) == 1


def test_force_failover_on_budget_exceeded_returns_false_on_last_provider():
    """When the currently active provider is the last one configured,
    force_failover_on_budget_exceeded() must report there is nowhere left to
    go (False) so the caller (autonomous.py) stops the whole run instead of
    silently continuing to spend past the configured cap."""
    p1 = MockAgent("claude-code", available=True)
    agent = FailoverAgent([p1])

    assert agent.force_failover_on_budget_exceeded("prekrocen max_budget_usd") is False


def test_describe_status_for_notify_reports_budget_exceeded():
    p1 = MockAgent("claude-code", available=True)
    agent = FailoverAgent([p1])
    agent.force_failover_on_budget_exceeded("utraceno $12.50 > limit $10.00")

    assert "claude-code: BUDGET_EXCEEDED" in agent._describe_status_for_notify("claude-code")


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


def test_failover_timeout_switches_provider(caplog):
    caplog.set_level(logging.INFO)

    def p1_run(request):
        return AgentRunResult(
            success=False,
            output_text="",
            error="Claude Code neodpověděl do 600s (timeout).",
            timed_out=True,
        )

    p1 = MockAgent("claude-code", run_fn=p1_run)
    p2 = MockAgent("antigravity")
    agent = FailoverAgent([p1, p2])

    result = agent.run(AgentRunRequest(project_path=Path("."), prompt="vytvor feature"))

    assert result.success is True
    assert len(p1.run_calls) == 1
    assert len(p2.run_calls) == 1
    assert "překročil timeout" in caplog.text


def test_failover_all_timeouts_returns_timeout_not_limited():
    def timeout(name):
        return lambda request: AgentRunResult(
            success=False,
            output_text="",
            error=f"{name} timeout",
            timed_out=True,
        )

    agent = FailoverAgent([
        MockAgent("claude-code", run_fn=timeout("claude")),
        MockAgent("antigravity", run_fn=timeout("antigravity")),
    ])

    result = agent.run(AgentRunRequest(project_path=Path("."), prompt="vytvor feature"))

    assert result.success is False
    assert result.timed_out is True
    assert result.limited is False
    assert result.error == "antigravity timeout"


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

    # Explicit provider still must not fail over, but a real quota limit
    # is now represented as WAITING_FOR_PROVIDER rather than a hard ERROR.
    assert result.status == AutonomousStatus.WAITING_FOR_PROVIDER
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
                output_text='{"items": [{"index": 0, "done": true}, {"index": 1, "done": false}], "notes": "bod 0 hotov"}',
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
                output_text=_audit_response(request),
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


def test_failover_preserves_usage_from_limited_provider_before_fallback():
    limited = MockAgent("p1", available=True, run_fn=lambda request: AgentRunResult(
        success=False, output_text="", error="429", limited=True,
        input_tokens=40, output_tokens=2, total_tokens=42,
        model="model-p1",
    ))
    success = MockAgent("p2", available=True, run_fn=lambda request: AgentRunResult(
        success=True, output_text="ok", input_tokens=10, output_tokens=3, total_tokens=13,
        model="model-p2",
    ))

    result = FailoverAgent([limited, success]).run(
        AgentRunRequest(project_path=Path("."), prompt="test")
    )

    assert [event["provider"] for event in result.usage_events] == ["p1", "p2"]
    assert [event["model"] for event in result.usage_events] == ["model-p1", "model-p2"]
    assert sum(event["total_tokens"] for event in result.usage_events) == 55

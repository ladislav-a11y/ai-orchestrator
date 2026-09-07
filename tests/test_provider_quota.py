from datetime import datetime, timezone
from pathlib import Path

from orchestrator.agents.base import Agent, AgentRunRequest, AgentRunResult
from orchestrator.agents.failover import FailoverAgent
from orchestrator.provider_quota import ProviderQuotaLedger


class _MockAgent(Agent):
    def __init__(self, name: str):
        self.name = name
        self.run_calls: list[AgentRunRequest] = []

    def is_available(self):
        return True, "ok"

    def run(self, request: AgentRunRequest):
        self.run_calls.append(request)
        return AgentRunResult(success=True, output_text=f"result from {self.name}")


def test_daily_ledger_persists_usage_and_applies_safety_margin(tmp_path: Path):
    path = tmp_path / "provider-quota.json"
    ledger = ProviderQuotaLedger(
        path,
        limits={"groq": 100},
        safety_margins={"groq": 10},
    )

    allowed, snapshot = ledger.preflight("groq", 90)
    assert allowed is True
    assert snapshot["daily_remaining_tokens"] == 100

    ledger.record_usage("groq", 20)
    reloaded = ProviderQuotaLedger(
        path,
        limits={"groq": 100},
        safety_margins={"groq": 10},
    )
    allowed, snapshot = reloaded.preflight("groq", 71)
    assert allowed is False
    assert snapshot["daily_used_tokens"] == 20
    assert snapshot["daily_remaining_tokens"] == 80
    assert "safely available" in snapshot["reason"]


def test_provider_receipt_quota_blocks_before_api_call_and_fails_over(tmp_path: Path):
    path = tmp_path / "provider-quota.json"
    ProviderQuotaLedger(
        path,
        limits={"groq": 100},
        safety_margins={"groq": 10},
    ).observe_quota(
        "groq",
        {
            "limit_tokens": 100,
            "used_tokens": 95,
            "remaining_tokens": 5,
            "retry_at": "2099-01-01T00:00:00+00:00",
            "source": "provider_429_tpd",
        },
    )
    groq = _MockAgent("groq")
    codex = _MockAgent("codex")
    agent = FailoverAgent(
        [groq, codex],
        quota_state_path=path,
        daily_token_limits={"groq": 100},
        daily_token_safety_margins={"groq": 10},
        token_budgets={"groq": 80},
    )

    result = agent.run(AgentRunRequest(project_path=tmp_path, prompt="small task"))

    assert result.success is True
    assert groq.run_calls == []
    assert len(codex.run_calls) == 1
    quota_status = agent.provider_status_snapshot()["groq"]
    assert quota_status["state"] == "LIMITED"
    assert quota_status["quota"]["daily_used_tokens"] == 95
    assert quota_status["quota"]["blocked_until"] == "2099-01-01T00:00:00+00:00"


def test_corrupt_daily_ledger_fails_closed(tmp_path: Path):
    path = tmp_path / "provider-quota.json"
    path.write_text("not json", encoding="utf-8")

    ledger = ProviderQuotaLedger(path, limits={"groq": 100})
    allowed, snapshot = ledger.preflight("groq", 1)

    assert allowed is False
    assert snapshot["state"] == "QUOTA_STATE_UNKNOWN"

from pathlib import Path

import pytest

from poc.hermes_agent.adapter import FakeLocalTransport, HermesAgent, HermesRunRequest
from poc.hermes_agent.fallback import HermesBudgetExceeded, HermesFailover


def _request() -> HermesRunRequest:
    return HermesRunRequest(project_path=Path("."), prompt="hi")


def test_failover_uses_first_healthy_provider():
    healthy = HermesAgent(transport=FakeLocalTransport())
    failover = HermesFailover(providers=[healthy])

    outcome = failover.run(_request())
    assert outcome.provider_name == "hermes-agent-poc"
    assert outcome.result.success is True


def test_failover_switches_when_first_provider_is_limited():
    limited = HermesAgent(transport=FakeLocalTransport(fail_after=0))
    healthy = HermesAgent(transport=FakeLocalTransport())
    failover = HermesFailover(providers=[limited, healthy])

    outcome = failover.run(_request())
    assert outcome.result.success is True
    assert any("limited=True" in attempt for attempt in outcome.attempts)


def test_failover_reports_limited_when_all_providers_limited():
    limited_a = HermesAgent(transport=FakeLocalTransport(fail_after=0))
    limited_b = HermesAgent(transport=FakeLocalTransport(fail_after=0))
    failover = HermesFailover(providers=[limited_a, limited_b])

    outcome = failover.run(_request())
    assert outcome.result.success is False
    assert outcome.result.limited is True


def test_failover_enforces_call_budget():
    always_limited = HermesAgent(transport=FakeLocalTransport(fail_after=0))
    failover = HermesFailover(providers=[always_limited], max_total_calls=1)

    failover.run(_request())  # consumes the only allowed call
    with pytest.raises(HermesBudgetExceeded):
        failover.run(_request())


def test_failover_skips_locally_unavailable_provider():
    class UnavailableTransport(FakeLocalTransport):
        def is_available(self):
            return False, "not installed"

    unavailable = HermesAgent(transport=UnavailableTransport())
    healthy = HermesAgent(transport=FakeLocalTransport())
    failover = HermesFailover(providers=[unavailable, healthy])

    outcome = failover.run(_request())
    assert outcome.result.success is True
    assert any("unavailable" in attempt for attempt in outcome.attempts)

"""Fallback/recovery and limit enforcement for the Hermes Agent PoC (DoD
point 3). Same *pattern* as `orchestrator/agents/failover.py`'s
`FailoverAgent`, reimplemented standalone against `HermesAgent` so this PoC
never imports production code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from poc.hermes_agent.adapter import HermesAgent, HermesRunRequest, HermesRunResult


@dataclass
class HermesFailoverResult:
    result: HermesRunResult
    provider_name: str
    attempts: list[str]


class HermesBudgetExceeded(RuntimeError):
    """Raised when the configured per-run call budget is exhausted without a
    successful result - the caller decides what to do next (stop, wait,
    etc.); this class never retries silently past the cap."""


class HermesFailover:
    """Tries a list of HermesAgent providers in order, skipping ones that are
    locally unavailable, and failing over on `limited=True` - same behaviour
    class as production `FailoverAgent.run()`, minus provider-specific
    session/usage bookkeeping this PoC does not need."""

    def __init__(self, providers: list[HermesAgent], max_total_calls: Optional[int] = None):
        if not providers:
            raise ValueError("HermesFailover requires at least one provider.")
        self.providers = providers
        self.max_total_calls = max_total_calls
        self._calls_made = 0

    def run(self, request: HermesRunRequest) -> HermesFailoverResult:
        attempts: list[str] = []
        last_result: Optional[HermesRunResult] = None

        for provider in self.providers:
            available, reason = provider.is_available()
            if not available:
                attempts.append(f"{provider.name}: unavailable ({reason})")
                continue

            if self.max_total_calls is not None and self._calls_made >= self.max_total_calls:
                raise HermesBudgetExceeded(
                    f"Call budget of {self.max_total_calls} exhausted after "
                    f"attempts: {attempts}"
                )

            self._calls_made += 1
            result = provider.run(request)
            last_result = result
            attempts.append(f"{provider.name}: limited={result.limited} success={result.success}")

            if result.limited:
                continue

            return HermesFailoverResult(result=result, provider_name=provider.name, attempts=attempts)

        if last_result is not None:
            return HermesFailoverResult(result=last_result, provider_name=self.providers[-1].name, attempts=attempts)

        return HermesFailoverResult(
            result=HermesRunResult(
                success=False,
                output_text="",
                error=f"No configured provider was available: {attempts}",
                limited=True,
            ),
            provider_name="none",
            attempts=attempts,
        )

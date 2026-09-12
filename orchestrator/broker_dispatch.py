"""v2 AO dispatch between the offer-only provider broker and providers.

The broker selects and describes a provider; it never executes provider work.
This facade is the orchestrator's AO-like boundary: it asks the broker for an
offer, translates the returned ``lang`` contract into ``AgentRunRequest``,
calls the selected provider directly, and publishes the provider-owned status
back to the broker.  Provider selection and retry policy remain broker-owned.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
import json
import logging
from typing import Any, Optional

from orchestrator.agents.base import Agent, AgentRunRequest, AgentRunResult
from orchestrator.model_routing import derive_task_profile, normalize_task_profile, profile_summary
from orchestrator.provider_broker import ProviderBroker


MAX_PROVIDER_ATTEMPTS = 5


def _receipt_schema(receipt: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    response_format = receipt.get("response_format")
    if not isinstance(response_format, Mapping):
        return None
    required = response_format.get("required_fields")
    if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
        return None
    properties = {
        field: {"type": "string"}
        for field in required
        if field in {"answer", "model"}
    }
    if set(properties) != set(required):
        return None
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _schema_prompt(schema: Mapping[str, Any]) -> str:
    """Render a caller schema for providers whose lang requires prompt text."""
    return (
        "Return exactly one JSON object matching this JSON Schema. "
        "Do not add fields or surrounding explanation.\n"
        + json.dumps(schema, ensure_ascii=False)
    )


def _lang_request(request: AgentRunRequest, offer: Mapping[str, Any]) -> AgentRunRequest:
    """Apply the selected provider's language contract to one AO request."""
    lang = offer.get("lang")
    if not isinstance(lang, Mapping):
        raise ValueError("Broker nenabídl platný lang kontrakt providera.")
    receipt = lang.get("task_execution_receipt")
    if not isinstance(receipt, Mapping):
        raise ValueError("lang kontrakt neobsahuje task_execution_receipt.")
    wrapper = receipt.get("prompt_wrapper")
    if not isinstance(wrapper, str) or not wrapper.strip():
        raise ValueError("lang kontrakt neobsahuje task_execution_receipt.prompt_wrapper.")

    prompt = request.prompt
    # A caller-owned structured output contract is authoritative for that
    # operation.  The generic answer/model receipt cannot be appended to it:
    # Inbox planning, for example, must return the top-level ``tasks`` object
    # and its schema forbids the receipt's extra fields.  The actual model is
    # still reported out-of-band by AgentRunResult.model and the AO envelope.
    caller_has_structured_schema = isinstance(request.output_schema, dict)
    agent_fields = receipt.get("agent_request_fields")
    separate_receipt = isinstance(agent_fields, Mapping) and "receipt_prompt" in agent_fields
    receipt_prompt = (
        wrapper.strip()
        if separate_receipt and not caller_has_structured_schema
        else request.receipt_prompt
    )
    if not separate_receipt and not caller_has_structured_schema and wrapper.strip() not in prompt:
        prompt = f"{prompt}\n\n---\n{wrapper.strip()}"

    output_schema = request.output_schema
    if output_schema is None:
        declared_schema = agent_fields.get("output_schema") if isinstance(agent_fields, Mapping) else None
        output_schema = declared_schema if isinstance(declared_schema, dict) else _receipt_schema(receipt)

    # Some provider contracts accept output_schema only as a common AO field
    # and require the concrete JSON constraint in the prompt.  Read that
    # requirement from lang instead of hardcoding an adapter-specific rule.
    ao_fields = lang.get("input_from_ao", {}).get("fields") if isinstance(lang.get("input_from_ao"), Mapping) else None
    schema_contract = ao_fields.get("output_schema") if isinstance(ao_fields, Mapping) else None
    if (
        isinstance(output_schema, dict)
        and isinstance(schema_contract, str)
        and "prompt" in schema_contract.casefold()
        and _schema_prompt(output_schema) not in prompt
    ):
        prompt = f"{prompt}\n\n---\n{_schema_prompt(output_schema)}"

    # The broker's selected model is the only model value forwarded to a
    # provider.  A caller's requested_model cannot override the broker offer.
    selected_model = offer.get("model")
    requested_model = selected_model.strip() if isinstance(selected_model, str) and selected_model.strip() else None
    return replace(
        request,
        prompt=prompt,
        output_schema=output_schema,
        receipt_prompt=receipt_prompt,
        requested_model=requested_model,
    )


def _unavailable_result(offer: Mapping[str, Any]) -> AgentRunResult:
    state = str(offer.get("state") or "UNKNOWN").upper()
    reason = str(offer.get("reason") or f"Broker nenabídl dostupného providera: {state}.")
    return AgentRunResult(
        success=False,
        output_text="",
        error=reason,
        limited=state in {"LIMITED", "NONE_AVAILABLE"},
        unavailable=state == "UNAVAILABLE",
        retry_after_seconds=offer.get("retry_after_seconds"),
    )


class BrokerBackedAgent(Agent):
    """v2 orchestrator facade that behaves like an ``Agent`` to the runner."""

    name = "provider-broker"

    def __init__(self, broker: ProviderBroker, logger: Any = None) -> None:
        self.broker = broker
        self.logger = logger or logging.getLogger(__name__)
        self.active_provider_name: Optional[str] = None
        self._provider_statuses: dict[str, dict[str, Any]] = {}
        self._next_dispatch_exclusions: set[str] = set()

    def is_available(self) -> tuple[bool, str]:
        return True, "Brokerová dispatch vrstva je připravená."

    def provider_status_snapshot(self) -> dict[str, dict[str, Any]]:
        return {provider: dict(status) for provider, status in self._provider_statuses.items()}

    def run(self, request: AgentRunRequest) -> AgentRunResult:
        caller = str(request.caller or "unknown")
        source = str(request.source or "unknown")
        task_profile = normalize_task_profile(request.task_profile)
        if not task_profile:
            task_profile = derive_task_profile(request.prompt)
        self.logger.info(
            "v2 broker-provider dispatch start caller=%s source=%s reason=%s task_profile=%s broker_action=select_provider",
            caller,
            source,
            request.selection_reason or "unspecified",
            profile_summary(task_profile),
        )
        pending_exclusions = self._next_dispatch_exclusions
        self._next_dispatch_exclusions = set()
        attempted = [
            provider for provider in self.broker.providers
            if provider in pending_exclusions
        ]
        results: list[AgentRunResult] = []

        for attempt in range(1, MAX_PROVIDER_ATTEMPTS + 1):
            query: str | dict[str, Any] = "select_provider"
            if attempted:
                # Selection remains broker-owned.  The exclusion is only the
                # broker's input for this dispatch retry; AO never chooses a
                # provider itself.
                query = {
                    "command": "select_provider",
                    "exclude_providers": list(attempted),
                    "task_profile": task_profile,
                }
            elif query == "select_provider":
                query = {"command": "select_provider", "task_profile": task_profile}
            response = self.broker.ask(query)
            offer = response.get("offer") if isinstance(response, Mapping) else None
            if not isinstance(offer, Mapping) or offer.get("state") != "AVAILABLE":
                unavailable = _unavailable_result(offer if isinstance(offer, Mapping) else {})
                if results:
                    unavailable.error = (
                        f"Pokus {attempt}/{MAX_PROVIDER_ATTEMPTS}: "
                        f"broker nenabídl dalšího providera: {unavailable.error}"
                    )
                    unavailable.usage_events = _combined_usage(list(zip(attempted, results)))
                    known_retries = [
                        float(result.retry_after_seconds)
                        for result in results
                        if result.retry_after_seconds is not None
                        and float(result.retry_after_seconds) >= 0
                    ]
                    if known_retries:
                        # v2 keeps the earliest provider-owned retry evidence
                        # when the broker has no further offer. Otherwise the
                        # final NONE_AVAILABLE envelope would lose the only
                        # actionable wake-up time observed during failover.
                        unavailable.retry_after_seconds = min(known_retries)
                    if len(attempted) > 1:
                        unavailable.selection_reason = (
                            f"failover: {' -> '.join(attempted)}"
                        )
                self.logger.info(
                    "v2 broker-provider dispatch end caller=%s source=%s provider=none model=none status=%s attempt=%s",
                    caller,
                    source,
                    str(offer.get("state") if isinstance(offer, Mapping) else "UNKNOWN").upper(),
                    attempt,
                )
                return unavailable

            provider_name = offer.get("provider")
            if not isinstance(provider_name, str) or not provider_name.strip():
                result = AgentRunResult(
                    success=False,
                    output_text="",
                    error="Broker v nabídce neuvedl providera.",
                    unavailable=True,
                )
                results.append(result)
                continue
            provider_name = provider_name.strip()
            attempted.append(provider_name)
            selected_model = offer.get("model")
            self.logger.info(
                "v2 broker-provider selected caller=%s source=%s provider=%s model=%s attempt=%s/%s",
                caller,
                source,
                provider_name,
                selected_model if isinstance(selected_model, str) and selected_model.strip() else "none",
                attempt,
                MAX_PROVIDER_ATTEMPTS,
            )
            provider = self.broker.providers.get(provider_name)
            if provider is None:
                result = AgentRunResult(
                    success=False,
                    output_text="",
                    error=f"Broker nabídl neznámého providera {provider_name!r}.",
                    unavailable=True,
                )
            else:
                required = request.required_capabilities
                supported = getattr(provider, "supported_capabilities", None)
                if required and supported is not None and not required.issubset(supported):
                    result = AgentRunResult(
                        success=False,
                        output_text="",
                        error=f"Provider {provider_name} nesplňuje požadované capability.",
                        capability_incompatible=True,
                        unavailable=True,
                    )
                else:
                    try:
                        provider_request = _lang_request(request, offer)
                    except (TypeError, ValueError, json.JSONDecodeError) as exc:
                        result = AgentRunResult(
                            success=False,
                            output_text="",
                            error=f"Brokerem dodaný lang kontrakt nelze použít: {exc}",
                            unavailable=True,
                        )
                    else:
                        self.active_provider_name = provider_name
                        result = provider.run(provider_request)

            results.append(result)
            status = getattr(result, "provider_status", None)
            if isinstance(status, Mapping):
                self._provider_statuses[provider_name] = dict(status)
            self.logger.info(
                "v2 broker-provider dispatch result caller=%s source=%s provider=%s model=%s success=%s attempt=%s/%s",
                caller,
                source,
                provider_name,
                result.model or "unknown",
                result.success,
                attempt,
                MAX_PROVIDER_ATTEMPTS,
            )
            # Every unsuccessful provider call is a broker retry candidate.
            # The AO must not wait for the next PM tick after a normal
            # provider error: ask the broker again immediately and let its
            # notes/order decide the next provider.  The specialised flags
            # retain their meaning for diagnostics and for providers that
            # expose a more precise failure class.
            retryable = (
                not result.success
                or result.limited
                or result.timed_out
                or result.unavailable
                or result.capability_incompatible
                or request.failover_on_error
            )
            if result.success or not retryable or attempt >= MAX_PROVIDER_ATTEMPTS:
                result.usage_events = _combined_usage(list(zip(attempted, results)))
                if len(attempted) > 1:
                    result.selection_reason = (
                        f"failover: {' -> '.join(attempted)}"
                    )
                return result

            self.logger.warning(
                "v2 broker-provider failure caller=%s source=%s provider=%s; "
                "žádám broker o dalšího providera (další pokus %s/%s)",
                caller,
                source,
                provider_name,
                attempt + 1,
                MAX_PROVIDER_ATTEMPTS,
            )

        # The loop always returns, this is only a defensive guard.
        return results[-1] if results else _unavailable_result({})

    def force_failover_on_protocol_error(self, reason: str) -> bool:
        """Ask the broker whether another provider can take the next AO call.

        Autonomous execution may detect a malformed provider response only
        after ``run()`` has returned.  It may request a retry, but it must
        not select a provider itself.  Probe the broker now so a positive
        answer stays inside the current orchestrator run; the next
        ``run()`` repeats the normal broker offer flow while excluding the
        provider that violated the response contract.
        """
        current = self.active_provider_name
        if not current:
            return False
        response = self.broker.ask(
            {
                "command": "select_provider",
                "exclude_providers": [current],
            }
        )
        offer = response.get("offer") if isinstance(response, Mapping) else None
        available = (
            isinstance(offer, Mapping)
            and offer.get("state") == "AVAILABLE"
            and isinstance(offer.get("provider"), str)
            and bool(offer.get("provider").strip())
        )
        if not available:
            self.logger.warning(
                "v2 broker-provider protocol failover unavailable provider=%s reason=%s",
                current,
                reason,
            )
            return False
        self._next_dispatch_exclusions.add(current)
        self.logger.warning(
            "v2 broker-provider protocol failover armed provider=%s next_provider=%s reason=%s",
            current,
            offer.get("provider"),
            reason,
        )
        return True


def _combined_usage(
    results: list[tuple[str, AgentRunResult]],
) -> list[dict[str, Any]]:
    """Preserve usage for every physical provider attempt in the result."""
    combined: list[dict[str, Any]] = []
    fields = (
        "input_tokens",
        "output_tokens",
        "thinking_tokens",
        "total_tokens",
        "cost_usd",
    )
    for provider, result in results:
        if result.usage_events:
            combined.extend(result.usage_events)
            continue
        # Keep the provider sequence visible even when a failed adapter did
        # not expose a usage event. Null usage is represented as zero by the
        # usage contract; an absent model remains absent, never inferred.
        combined.append({
            "provider": provider,
            "model": result.model,
            "source": "reported",
            **{field: getattr(result, field, None) for field in fields},
        })
    return combined


def build_broker_backed_agent(config: Any, logger: Any = None, agent_builder: Any = None) -> BrokerBackedAgent:
    """Build the v2 facade without adding execution behavior to the broker."""
    from orchestrator.provider_broker import build_provider_broker

    return BrokerBackedAgent(
        build_provider_broker(config, logger=logger, agent_builder=agent_builder),
        logger=logger,
    )

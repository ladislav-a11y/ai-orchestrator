"""Abstract interface every AI agent/provider must implement.

Keep this surface small and provider-agnostic so a future OpenAI Codex
agent (or a review agent) can be added by writing one new class, without
touching the queue, runner, CLI, or API.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


def model_from_paths(sources: list[dict[str, Any]], paths: list[str]) -> Optional[str]:
    """Return the first provider-reported model found in configured JSON paths."""
    for source in sources:
        for path in paths:
            value: Any = source
            for part in path.split("."):
                if not isinstance(value, dict) or part not in value:
                    value = None
                    break
                value = value[part]
            if isinstance(value, str) and value.strip():
                return value.strip()
            if (
                isinstance(value, dict)
                and path.rsplit(".", 1)[-1] in {"modelUsage", "model_usage"}
            ):
                names = [str(name).strip() for name in value if str(name).strip()]
                if names:
                    return ", ".join(names)
    return None


@dataclass
class AgentRunRequest:
    project_path: Path
    prompt: str
    # Extra instructions appended for a fix-attempt (e.g. failing test output).
    # None on the first attempt.
    context: Optional[str] = None
    session_id: Optional[str] = None  # to resume a previous run of the same task
    # Optional JSON Schema for the provider's final response. Providers with
    # native structured output should enforce it; others may rely on prompt.
    output_schema: Optional[dict[str, Any]] = None
    # Optional provider-language receipt instruction. It is kept separate from
    # the work prompt so a tool-enabled provider can apply it only during its
    # tool-free finalization phase.
    receipt_prompt: Optional[str] = None
    # Explicit per-call model override (e.g. AI Project Manager routing by
    # task type/complexity). When set and non-empty, a provider that supports
    # passing a model to its CLI uses this instead of its configured default
    # for THIS call only - config.yaml is never mutated. A provider adapter
    # never validates this string against a model catalog (none exists in
    # this orchestrator, see PROVIDER_MODEL_ROUTING_RESEARCH.md); an invalid
    # value is rejected by the underlying CLI like any other bad --model
    # value, surfacing as an ordinary AgentRunResult(success=False, ...).
    requested_model: Optional[str] = None
    # Capabilities the caller requires from the provider for this request.
    # A provider with a declared capability set must be rejected before its
    # API is called when it cannot satisfy these requirements.
    required_capabilities: frozenset[str] = field(default_factory=frozenset)
    # Opaque, caller-supplied reason for this provider/model request (e.g.
    # "explicit_agent", "default_agent", or a PM-owned task-classification
    # tag). The orchestrator never interprets or validates this string - it
    # is carried through so the receipt on AgentRunResult.selection_reason
    # can echo it (or be overridden with the actual failover reason, see
    # FailoverAgent).
    selection_reason: Optional[str] = None
    # Central failover may retry a provider-specific execution error for
    # structured, read-only operations such as Inbox planning. Ordinary
    # autonomous implementation requests keep the fail-closed behavior
    # unless the caller explicitly opts in.
    failover_on_error: bool = False
    # Remaining hard token budget for this provider in the current
    # orchestrated job. FailoverAgent fills this from its provider policy;
    # adapters that support token-aware limits must stop before the next
    # physical request when the budget cannot safely fit it.
    max_total_tokens: Optional[int] = None


@dataclass
class AgentRunResult:
    success: bool
    output_text: str
    raw_response: Optional[dict] = None
    session_id: Optional[str] = None
    cost_usd: Optional[float] = None
    error: Optional[str] = None
    # Count of tool calls the agent wanted to make but were denied by the
    # permission system (e.g. sandboxed Bash). Kept out of `output_text` -
    # any caller parsing a strict contract out of the agent's own message
    # (see autonomous.py) must only ever see what the agent itself said.
    permission_denials: int = 0
    # The denied actions themselves (tool name/input etc.), exactly as
    # reported by the agent's own raw response - so a human/log can see
    # *which* commands were denied, not just how many. Same rationale as
    # `permission_denials` for staying out of `output_text`.
    permission_denial_details: list[dict[str, Any]] = field(default_factory=list)
    # How many repeated test-invocation attempts (pytest/python/unittest/cmd
    # variants tried again after the first permission denial for that
    # command class) the orchestrator's own PreToolUse hook
    # (orchestrator/hooks/test_command_guard.py) short-circuited during this
    # run, instead of letting the agent keep retrying - see
    # ClaudeCodeAgent.run() and claude_settings.py's `hooks.PreToolUse`.
    breaker_saved_attempts: int = 0
    # Token usage, when the provider's own response reports it (e.g.
    # AntigravityAgent - see orchestrator/agents/antigravity.py). None for a
    # provider/response that doesn't report a given figure.
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    thinking_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    # Provider-attributed usage for every physical CLI call represented by
    # this result. FailoverAgent preserves limited attempts here as well as
    # the final provider call, so callers never lose spent tokens on fallback.
    # ``source`` is ``reported`` for provider metadata; estimates must use a
    # different explicit value and are never mixed into reported totals.
    usage_events: list[dict[str, Any]] = field(default_factory=list)
    # Provider-reported quota information, when the provider exposes it (for
    # example Groq's TPD Limit/Used/Requested values on a 429). This is kept
    # separate from usage totals because a rejected request has no usage
    # event, but its quota evidence is still needed by central failover.
    quota_snapshot: Optional[dict[str, Any]] = None
    # True if the provider's failure looks like a quota/rate/session limit
    # rather than an ordinary error (e.g. Antigravity's RESOURCE_EXHAUSTED /
    # "quota has been exceeded" responses) - callers (autonomous.py) can use
    # this to back off and retry instead of treating it as a hard failure.
    # `success` still stays False for a limited response; this is additional
    # detail, not a replacement status enum.
    limited: bool = False
    # True when the orchestrator's own per-provider token budget stopped the
    # call. This is distinct from a provider-reported quota/rate limit.
    token_budget_exceeded: bool = False
    # True when the provider process exceeded its configured wall-clock
    # timeout. This is distinct from a quota limit so FailoverAgent can move
    # to the next provider without misreporting the cause as LIMITED.
    timed_out: bool = False
    # True when this provider cannot serve the request in the current local
    # environment (for example an unsupported CLI/account authentication
    # state). This is distinct from LIMITED so failover can continue without
    # falsely reporting quota exhaustion.
    unavailable: bool = False
    # True when the provider cannot satisfy the request's declared
    # capabilities. This is distinct from local/account unavailability and
    # from provider quota/rate limits.
    capability_incompatible: bool = False
    # Seconds to wait before retrying, when the provider's own response
    # includes that information. None if unknown/not provided.
    retry_after_seconds: Optional[float] = None
    # Model identity reported by the provider for this physical call. The PM
    # must not infer it from a configured catalog or requested model. This is
    # the authoritative provider-confirmed identity when model_source is
    # "reported" or "reported_receipt". A receipt is accepted for Codex
    # according to its lang contract; provider-specific authority remains
    # defined by the provider's lang file.
    model: Optional[str] = None
    # How `model` was established: "reported" when the provider's own CLI
    # response confirmed it, "reported_receipt" when the provider's required
    # task receipt supplied it without machine metadata, "requested" when it is only known because this
    # call's AgentRunRequest.requested_model was passed to the CLI (not yet
    # confirmed by the provider), "configured" when it is only known because
    # config.yaml's static value was passed. None when `model` itself is None
    # (no value was ever sent or reported - never invent one, see
    # claude_code.py's _reported_model()).
    model_source: Optional[str] = None
    # Model explicitly requested by the caller/broker. This is never evidence
    # that the provider actually used the model.
    requested_model: Optional[str] = None
    # Exact model string returned inside a task receipt. Whether it is
    # authoritative or diagnostic is defined by the provider's lang contract.
    receipt_model: Optional[str] = None
    # Record-only comparison of requested, metadata, and receipt identities.
    model_verification: Optional[dict[str, Any]] = None
    # Machine-passable reason for the ACTUAL provider selection this result
    # represents. Single-provider adapters echo AgentRunRequest.selection_reason
    # unchanged (they have no extra insight of their own). FailoverAgent
    # overrides this with the real mechanism (e.g. "failover: groq LIMITED
    # -> antigravity") whenever it advanced past another provider first, so a
    # caller never has to reconstruct the reason from provider_statuses.
    selection_reason: Optional[str] = None


class Agent(ABC):
    """Base class for any implementation agent (Claude Code, Codex, ...)."""

    name: str = "base-agent"
    # None means the adapter does not publish a capability contract and the
    # central failover must not guess. Concrete adapters may publish a finite
    # set to enable fail-closed preflight checks.
    supported_capabilities: Optional[frozenset[str]] = None

    @abstractmethod
    def is_available(self) -> tuple[bool, str]:
        """Return (available, message). Must never raise."""
        raise NotImplementedError

    @abstractmethod
    def run(self, request: AgentRunRequest) -> AgentRunResult:
        """Execute the agent on the given project. Must never raise for
        expected failure modes (agent error, timeout, non-zero exit) -
        those should come back as AgentRunResult(success=False, error=...).
        Only truly unexpected programming errors should raise.
        """
        raise NotImplementedError

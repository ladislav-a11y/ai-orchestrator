"""Failover agent implementation for automatic provider fallback.

In autonomous development mode, users should not need to know which provider
currently has available quota. FailoverAgent tries providers in a configured
order (default: claude-code -> antigravity -> codex), skips locally
unavailable providers, and automatically fails over to the next provider
when a provider returns AgentRunResult(limited=True) due to quota/rate/session
limits.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from orchestrator.agents.base import Agent, AgentRunRequest, AgentRunResult
from orchestrator.config import Config


@dataclass
class ProviderStatus:
    name: str
    available: bool = True
    unavailable_reason: Optional[str] = None
    limited: bool = False
    limited_error: Optional[str] = None
    retry_after_seconds: Optional[float] = None


class FailoverAgent(Agent):
    name = "failover"

    def __init__(
        self,
        providers: list[Agent],
        logger: Optional[logging.Logger] = None,
    ):
        if not providers:
            raise ValueError("FailoverAgent vyžaduje alespoň jednoho providera v seznamu.")
        self.providers = providers
        self.logger = logger or logging.getLogger("orchestrator")
        self._provider_statuses: dict[str, ProviderStatus] = {}
        self._sessions: dict[str, str] = {}
        self._active_provider_index: int = 0

    @property
    def active_agent(self) -> Agent:
        idx = min(self._active_provider_index, len(self.providers) - 1)
        return self.providers[idx]

    @property
    def active_provider_name(self) -> str:
        return getattr(self.active_agent, "name", str(self.active_agent))

    def is_available(self) -> tuple[bool, str]:
        """Check if at least one configured provider is available and not limited."""
        available_names: list[str] = []
        notes: list[str] = []

        for p in self.providers:
            p_name = getattr(p, "name", str(p))
            status = self._provider_statuses.get(p_name)
            if status and status.limited:
                notes.append(f"{p_name} (LIMITED)")
                continue
            ok, msg = p.is_available()
            if ok:
                available_names.append(p_name)
            else:
                notes.append(f"{p_name} (nedostupný: {msg})")

        if available_names:
            return True, f"Dostupní provideři: {', '.join(available_names)}"
        return False, f"Žádný provider není k dispozici ({'; '.join(notes)})."

    def run(self, request: AgentRunRequest) -> AgentRunResult:
        last_result: Optional[AgentRunResult] = None
        order_names = [getattr(p, "name", str(p)) for p in self.providers]

        while self._active_provider_index < len(self.providers):
            provider = self.providers[self._active_provider_index]
            provider_name = getattr(provider, "name", str(provider))

            status = self._provider_statuses.get(provider_name)
            if status and status.limited:
                self.logger.info(
                    "Provider '%s' je v tomto běhu již označen jako LIMITED, přeskakuji.",
                    provider_name,
                )
                self._active_provider_index += 1
                continue

            # Verify local availability before using the provider
            is_avail, avail_msg = provider.is_available()
            if not is_avail:
                self._provider_statuses[provider_name] = ProviderStatus(
                    name=provider_name,
                    available=False,
                    unavailable_reason=avail_msg,
                )
                self.logger.info(
                    "Provider '%s' není lokálně dostupný (%s), přeskakuji na dalšího providera.",
                    provider_name,
                    avail_msg,
                )
                self._active_provider_index += 1
                continue

            self.logger.info(
                "Vybrán provider '%s' (z pořadí %s).",
                provider_name,
                order_names,
            )

            provider_session = self._sessions.get(provider_name)
            effective_request = AgentRunRequest(
                project_path=request.project_path,
                prompt=request.prompt,
                context=request.context,
                session_id=provider_session,
            )

            result = provider.run(effective_request)
            last_result = result
            if result.session_id:
                self._sessions[provider_name] = result.session_id

            if result.limited:
                retry_note = (
                    f", retry po {result.retry_after_seconds}s"
                    if result.retry_after_seconds is not None
                    else ""
                )
                self._provider_statuses[provider_name] = ProviderStatus(
                    name=provider_name,
                    available=True,
                    limited=True,
                    limited_error=result.error,
                    retry_after_seconds=result.retry_after_seconds,
                )

                next_index = self._active_provider_index + 1
                if next_index < len(self.providers):
                    next_name = getattr(
                        self.providers[next_index], "name", str(self.providers[next_index])
                    )
                    self.logger.warning(
                        "Provider '%s' vrátil LIMITED (vyčerpána kvóta/limit%s): %s. Přepínám na providera '%s'.",
                        provider_name,
                        retry_note,
                        result.error or "limit vyčerpán",
                        next_name,
                    )
                else:
                    self.logger.warning(
                        "Provider '%s' vrátil LIMITED (vyčerpána kvóta/limit%s): %s. Žádný další provider v pořadí nezbývá.",
                        provider_name,
                        retry_note,
                        result.error or "limit vyčerpán",
                    )

                self._active_provider_index += 1
                continue

            # Normal result (success=True or ordinary error with limited=False)
            return result

        self.logger.error(
            "Všichni konfigurovaní provideři (%s) jsou nedostupní nebo LIMITED.",
            ", ".join(order_names),
        )
        known_retries = [
            status.retry_after_seconds
            for status in self._provider_statuses.values()
            if status.limited and status.retry_after_seconds is not None
        ]
        retry_after_seconds = min(known_retries) if known_retries else None

        return AgentRunResult(
            success=False,
            output_text="",
            error="V\u0161ichni konfigurovan\u00ed provide\u0159i (" + ", ".join(order_names) + ") jsou nedostupn\u00ed nebo LIMITED.",
            limited=True,
            retry_after_seconds=retry_after_seconds,
        )


def build_failover_agent(
    config: Config,
    provider_order: Optional[list[str]] = None,
    logger: Optional[logging.Logger] = None,
    agent_builder=None,
) -> FailoverAgent:
    if agent_builder is None:
        from orchestrator.agents.registry import build_agent as agent_builder

    order = provider_order or config.provider_order or ["claude-code", "antigravity", "codex"]
    providers = [agent_builder(name, config) for name in order]
    return FailoverAgent(providers=providers, logger=logger)

"""Failover agent implementation for automatic provider fallback.

In autonomous development mode, users should not need to know which provider
currently has available quota. FailoverAgent tries providers in a configured
order (default: hermes -> gemini -> antigravity -> claude-code -> codex), skips locally
unavailable providers, and automatically fails over to the next provider
when a provider returns AgentRunResult(limited=True) due to quota/rate/session
limits, times out, or reports an unavailable local/account runtime.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from orchestrator.agents.base import Agent, AgentRunRequest, AgentRunResult
from orchestrator.config import Config
from orchestrator.slack_notify import notify


@dataclass
class ProviderStatus:
    name: str
    available: bool = True
    unavailable_reason: Optional[str] = None
    limited: bool = False
    limited_error: Optional[str] = None
    retry_after_seconds: Optional[float] = None
    # Set by force_failover_on_protocol_error() - a provider that repeatedly
    # fails to return the required JSON contract (see autonomous.py's
    # PROTOCOL_ERROR_STREAK_LIMIT), not a quota/rate limit. Kept distinct
    # from `limited` so logs/state never claim a provider is out of quota
    # when the real problem is a protocol incompatibility.
    protocol_incompatible: bool = False
    protocol_incompatible_reason: Optional[str] = None
    # Set by force_failover_on_budget_exceeded() - this job's cumulative
    # reported spend for this provider passed its configured per-provider
    # max_budget_usd (see autonomous.py's _provider_budget_usd/
    # BUDGET_EXCEEDED). Kept distinct from `limited` so logs/state never
    # claim a provider is out of *quota* when the real reason is our own
    # configured financial cap for this job.
    budget_exceeded: bool = False
    budget_exceeded_reason: Optional[str] = None


def _format_duration(seconds: float) -> str:
    """Human-readable "za Xh Ym"/"za Xm"/"za Xs" duration for a
    retry_after_seconds value - matches how the production incident report
    of 26.8.2026 described provider resets ("reset cca +70h36m")."""
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes}m"
    if minutes:
        return f"{minutes}m{secs}s"
    return f"{secs}s"


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
        self._last_notified_provider: Optional[str] = None

    @property
    def active_agent(self) -> Agent:
        idx = min(self._active_provider_index, len(self.providers) - 1)
        return self.providers[idx]

    @property
    def active_provider_name(self) -> str:
        return getattr(self.active_agent, "name", str(self.active_agent))

    def force_failover_on_protocol_error(self, reason: str) -> bool:
        """Advance past the currently active provider because it repeatedly
        failed to return the required JSON contract (see autonomous.py's
        PROTOCOL_ERROR_STREAK_LIMIT) - a protocol incompatibility, not a
        quota/rate limit, but one that deserves the same right to fail over
        to another configured provider instead of stopping the whole run.

        Returns True if another provider is now active (the run should
        keep going), False if the currently active provider was already the
        last one configured (the caller must stop the run instead)."""
        provider_name = self.active_provider_name
        order_names = [getattr(p, "name", str(p)) for p in self.providers]
        self._provider_statuses[provider_name] = ProviderStatus(
            name=provider_name,
            available=True,
            protocol_incompatible=True,
            protocol_incompatible_reason=reason,
        )

        next_index = self._active_provider_index + 1
        if next_index < len(self.providers):
            next_name = order_names[next_index]
            self.logger.warning(
                "Provider '%s' opakovaně nevrací platný JSON kontrakt (%s). Přepínám na "
                "providera '%s'.",
                provider_name, reason, next_name,
            )
            notify(
                f"[AI Orchestrator] Provider {provider_name} opakovaně porušuje JSON protokol; "
                f"přepínám na {next_name}. Důvod: {reason}"
            )
            self._active_provider_index += 1
            return True

        self.logger.warning(
            "Provider '%s' opakovaně nevrací platný JSON kontrakt (%s). Žádný další provider v "
            "pořadí %s nezbývá.",
            provider_name, reason, order_names,
        )
        return False

    def force_failover_on_budget_exceeded(self, reason: str) -> bool:
        """Advance past the currently active provider because this job's
        cumulative reported spend on it exceeded its configured
        max_budget_usd (see autonomous.py's _provider_budget_usd) - a
        financial safety cap this orchestrator enforces itself, not a
        provider-reported quota/rate limit, but one that deserves the same
        right to fail over to another configured provider instead of
        stopping the whole run outright.

        Returns True if another provider is now active (the run should
        keep going), False if the currently active provider was already the
        last one configured (the caller must stop the run instead)."""
        provider_name = self.active_provider_name
        order_names = [getattr(p, "name", str(p)) for p in self.providers]
        self._provider_statuses[provider_name] = ProviderStatus(
            name=provider_name,
            available=True,
            budget_exceeded=True,
            budget_exceeded_reason=reason,
        )

        next_index = self._active_provider_index + 1
        if next_index < len(self.providers):
            next_name = order_names[next_index]
            self.logger.warning(
                "Provider '%s' překročil finanční limit nastavený pro tuto úlohu (%s). "
                "Přepínám na providera '%s'.",
                provider_name, reason, next_name,
            )
            notify(
                f"[AI Orchestrator] Provider {provider_name} překročil svůj finanční limit pro "
                f"tuto úlohu; přepínám na {next_name}. Důvod: {reason}"
            )
            self._active_provider_index += 1
            return True

        self.logger.warning(
            "Provider '%s' překročil finanční limit nastavený pro tuto úlohu (%s). Žádný další "
            "provider v pořadí %s nezbývá.",
            provider_name, reason, order_names,
        )
        return False

    def _describe_status_for_notify(self, name: str) -> str:
        """One human-readable line per provider for the "all exhausted"
        Slack notification - see DoD requirement (produkční incident
        cb501524e47e, 26.8.2026): the message must show the state of EACH
        provider individually, not just their names."""
        status = self._provider_statuses.get(name)
        if status is None:
            return f"{name}: stav neznámý"
        if status.limited:
            retry = (
                f", reset za {_format_duration(status.retry_after_seconds)}"
                if status.retry_after_seconds is not None
                else ", čas resetu neznámý"
            )
            return f"{name}: LIMITED ({status.limited_error or 'limit vyčerpán'}{retry})"
        if status.protocol_incompatible:
            return f"{name}: PROTOCOL_ERROR ({status.protocol_incompatible_reason})"
        if status.budget_exceeded:
            return f"{name}: BUDGET_EXCEEDED ({status.budget_exceeded_reason})"
        if not status.available:
            return f"{name}: nedostupný ({status.unavailable_reason})"
        return f"{name}: stav neznámý"

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
            if status and status.protocol_incompatible:
                notes.append(f"{p_name} (PROTOCOL_ERROR)")
                continue
            if status and status.budget_exceeded:
                notes.append(f"{p_name} (BUDGET_EXCEEDED)")
                continue
            if status and not status.available:
                notes.append(f"{p_name} (nedostupný: {status.unavailable_reason or 'neznámý důvod'})")
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
        usage_events: list[dict] = []
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
            if status and status.protocol_incompatible:
                self.logger.info(
                    "Provider '%s' je v tomto běhu již označen jako protokolově nekompatibilní, "
                    "přeskakuji.",
                    provider_name,
                )
                self._active_provider_index += 1
                continue
            if status and status.budget_exceeded:
                self.logger.info(
                    "Provider '%s' je v tomto běhu již označen jako BUDGET_EXCEEDED, přeskakuji.",
                    provider_name,
                )
                self._active_provider_index += 1
                continue
            if status and not status.available:
                self.logger.info(
                    "Provider '%s' je v tomto běhu již označen jako nedostupný, přeskakuji.",
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
            if provider_name != self._last_notified_provider:
                notify(
                    f"[AI Orchestrator] Aktivní provider: {provider_name} | pořadí: "
                    + " → ".join(order_names)
                )
                self._last_notified_provider = provider_name

            provider_session = self._sessions.get(provider_name)
            effective_request = AgentRunRequest(
                project_path=request.project_path,
                prompt=request.prompt,
                context=request.context,
                session_id=provider_session,
                output_schema=request.output_schema,
            )

            started_at = time.monotonic()
            result = provider.run(effective_request)
            elapsed_seconds = time.monotonic() - started_at
            self.logger.info(
                "Provider '%s' dokončil volání za %.1fs (success=%s, limited=%s, timed_out=%s).",
                provider_name, elapsed_seconds, result.success, result.limited, result.timed_out,
            )
            last_result = result
            event = {
                "provider": provider_name,
                "source": "reported",
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "thinking_tokens": result.thinking_tokens,
                "total_tokens": result.total_tokens,
                "cost_usd": result.cost_usd,
            }
            if any(event[key] is not None for key in event if key not in {"provider", "source"}):
                usage_events.extend(result.usage_events or [event])
            if result.session_id:
                self._sessions[provider_name] = result.session_id

            if result.timed_out:
                self._provider_statuses[provider_name] = ProviderStatus(
                    name=provider_name,
                    available=False,
                    unavailable_reason=result.error or "timeout",
                )
                next_index = self._active_provider_index + 1
                if next_index < len(self.providers):
                    next_name = getattr(
                        self.providers[next_index], "name", str(self.providers[next_index])
                    )
                    self.logger.warning(
                        "Provider '%s' překročil timeout: %s. Přepínám na providera '%s'.",
                        provider_name, result.error or "timeout", next_name,
                    )
                    notify(
                        f"[AI Orchestrator] Provider {provider_name} překročil timeout; "
                        f"přepínám na {next_name}."
                    )
                    self._active_provider_index += 1
                    continue
                self.logger.error(
                    "Provider '%s' překročil timeout a žádný další provider v pořadí %s nezbývá.",
                    provider_name, order_names,
                )
                break

            if result.unavailable:
                self._provider_statuses[provider_name] = ProviderStatus(
                    name=provider_name,
                    available=False,
                    unavailable_reason=result.error or "provider je pro tento běh nedostupný",
                )
                next_index = self._active_provider_index + 1
                if next_index < len(self.providers):
                    next_name = getattr(
                        self.providers[next_index], "name", str(self.providers[next_index])
                    )
                    self.logger.warning(
                        "Provider '%s' je nedostupný: %s. Přepínám na providera '%s'.",
                        provider_name, result.error or "neznámý důvod", next_name,
                    )
                    notify(
                        f"[AI Orchestrator] Provider {provider_name} je nedostupný; "
                        f"přepínám na {next_name}. Důvod: {result.error or 'neznámý důvod'}"
                    )
                    self._active_provider_index += 1
                    continue
                self.logger.error(
                    "Provider '%s' je nedostupný a žádný další provider v pořadí %s nezbývá.",
                    provider_name, order_names,
                )
                break

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
                    notify(
                        f"[AI Orchestrator] Provider {provider_name} vyčerpal limit; "
                        f"přepínám na {next_name}."
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
            result.usage_events = usage_events or result.usage_events
            return result

        if last_result is not None and not last_result.limited:
            last_result.usage_events = usage_events or last_result.usage_events
            return last_result

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

        # DoD (produkční incident 26.8.2026, task cb501524e47e): Slack musí
        # dostat jednoznačnou zprávu o stavu KAŽDÉHO providera zvlášť (ne jen
        # jejich jména) a o nejbližším známém resetu/retry, ne obecné
        # "provider X vyčerpal limit" hlášení z jednotlivých přepnutí výše.
        status_lines = [self._describe_status_for_notify(name) for name in order_names]
        retry_note = (
            f"Nejbližší známý reset/retry: za {_format_duration(retry_after_seconds)}."
            if retry_after_seconds is not None
            else "Čas žádného resetu/retry není u žádného providera znám."
        )
        notify(
            "[AI Orchestrator] Všichni provideři jsou LIMITED/nedostupní - autonomní běh "
            "přechází do WAITING_FOR_PROVIDER:\n- " + "\n- ".join(status_lines) + f"\n{retry_note}"
        )

        return AgentRunResult(
            success=False,
            output_text="",
            error="V\u0161ichni konfigurovan\u00ed provide\u0159i (" + ", ".join(order_names) + ") jsou nedostupn\u00ed nebo LIMITED.",
            limited=True,
            retry_after_seconds=retry_after_seconds,
            usage_events=usage_events,
        )


def build_failover_agent(
    config: Config,
    provider_order: Optional[list[str]] = None,
    logger: Optional[logging.Logger] = None,
    agent_builder=None,
) -> FailoverAgent:
    if agent_builder is None:
        from orchestrator.agents.registry import build_agent as agent_builder

    order = provider_order or config.provider_order or [
        "hermes", "gemini", "antigravity", "claude-code", "codex"
    ]
    providers = [agent_builder(name, config) for name in order]
    return FailoverAgent(providers=providers, logger=logger)

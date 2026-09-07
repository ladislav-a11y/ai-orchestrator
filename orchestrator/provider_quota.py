"""Durable, conservative provider quota accounting.

The provider API is the authority for a reported quota error, but it does not
expose Groq TPD remaining before every request.  This small ledger therefore
keeps the orchestrator's own usage across short-lived PM/AO processes and
stores provider-reported Limit/Used/Requested evidence when a 429 occurs.
It is deliberately provider-neutral so another API provider can opt in later.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number >= 0 else None


def _as_int(value: Any) -> Optional[int]:
    number = _as_float(value)
    if number is None:
        return None
    return int(number)


class ProviderQuotaLedger:
    """Persist local provider usage and fail closed when its gate is unclear."""

    def __init__(
        self,
        path: Path,
        *,
        limits: Optional[dict[str, int]] = None,
        safety_margins: Optional[dict[str, int]] = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.path = Path(path)
        self._limits = {
            name: max(1, int(value))
            for name, value in (limits or {}).items()
            if value is not None
        }
        self._safety_margins = {
            name: max(0, int(value))
            for name, value in (safety_margins or {}).items()
            if value is not None
        }
        self._clock = clock
        self._state: dict[str, Any] = {"version": 1, "providers": {}}
        self._load_error: Optional[str] = None
        self._load()

    def _window_key(self) -> str:
        return self._clock().astimezone(timezone.utc).date().isoformat()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or not isinstance(raw.get("providers", {}), dict):
                raise ValueError("provider quota ledger must contain an object of providers")
            self._state = {"version": 1, "providers": dict(raw["providers"])}
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._load_error = f"provider quota ledger nelze načíst: {exc}"

    def _entry(self, provider: str) -> dict[str, Any]:
        providers = self._state.setdefault("providers", {})
        entry = providers.setdefault(provider, {})
        if not isinstance(entry, dict):
            entry = {}
            providers[provider] = entry
        if entry.get("window_key") != self._window_key():
            entry.clear()
            entry.update(
                {
                    "window_key": self._window_key(),
                    "used_tokens": 0,
                    "remaining_tokens": None,
                    "blocked_until": None,
                    "source": "local_window_reset",
                }
            )
        entry.setdefault("limit_tokens", self._limits.get(provider))
        entry.setdefault("safety_margin_tokens", self._safety_margins.get(provider, 0))
        entry.setdefault("used_tokens", 0)
        return entry

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            temporary.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass

    def preflight(self, provider: str, required_tokens: int) -> tuple[bool, dict[str, Any]]:
        """Return whether a whole bounded job may be sent to ``provider``."""
        if provider not in self._limits:
            return True, {}
        if self._load_error:
            return False, {"state": "QUOTA_STATE_UNKNOWN", "reason": self._load_error}

        entry = self._entry(provider)
        now = self._clock().astimezone(timezone.utc)
        blocked_until = entry.get("blocked_until")
        if isinstance(blocked_until, str) and blocked_until.strip():
            try:
                deadline = datetime.fromisoformat(blocked_until)
                if deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=timezone.utc)
                if deadline.astimezone(timezone.utc) > now:
                    snapshot = self.snapshot(provider)
                    snapshot.update(
                        {
                            "state": "LIMITED",
                            "reason": "provider reported a quota window still in cooldown",
                        }
                    )
                    return False, snapshot
            except (TypeError, ValueError, OverflowError):
                return False, {
                    "state": "QUOTA_STATE_UNKNOWN",
                    "reason": "provider quota ledger contains an invalid blocked_until",
                }

        required = max(1, int(required_tokens))
        limit = _as_int(entry.get("limit_tokens"))
        used = _as_int(entry.get("used_tokens")) or 0
        reported_remaining = _as_int(entry.get("remaining_tokens"))
        margin = _as_int(entry.get("safety_margin_tokens")) or 0
        if limit is None:
            return False, {
                "state": "QUOTA_STATE_UNKNOWN",
                "reason": f"provider {provider} has no configured daily token limit",
            }

        local_remaining = max(0, limit - used - margin)
        available = local_remaining
        if reported_remaining is not None:
            available = min(available, max(0, reported_remaining - margin))
        if required > available:
            snapshot = self.snapshot(provider)
            snapshot.update(
                {
                    "state": "LIMITED",
                    "reason": (
                        f"local daily quota gate requires {required} tokens, "
                        f"but only {available} are safely available"
                    ),
                }
            )
            return False, snapshot
        return True, self.snapshot(provider)

    def record_usage(self, provider: str, tokens: Optional[int]) -> None:
        value = _as_int(tokens)
        if value is None or value <= 0 or provider not in self._limits or self._load_error:
            return
        entry = self._entry(provider)
        entry["used_tokens"] = (_as_int(entry.get("used_tokens")) or 0) + value
        if entry.get("remaining_tokens") is not None:
            entry["remaining_tokens"] = max(
                0, (_as_int(entry.get("remaining_tokens")) or 0) - value
            )
        entry["source"] = "reported_usage"
        entry["updated_at"] = self._clock().astimezone(timezone.utc).isoformat()
        self._save()

    def observe_quota(self, provider: str, snapshot: Optional[dict[str, Any]]) -> None:
        if provider not in self._limits or not isinstance(snapshot, dict) or self._load_error:
            return
        entry = self._entry(provider)
        limit = _as_int(snapshot.get("limit_tokens"))
        used = _as_int(snapshot.get("used_tokens"))
        remaining = _as_int(snapshot.get("remaining_tokens"))
        if limit is not None:
            entry["limit_tokens"] = limit
        if used is not None:
            entry["used_tokens"] = max(_as_int(entry.get("used_tokens")) or 0, used)
        if remaining is not None:
            entry["remaining_tokens"] = remaining
        retry_at = snapshot.get("retry_at")
        if isinstance(retry_at, str) and retry_at.strip():
            entry["blocked_until"] = retry_at
        entry["source"] = snapshot.get("source") or "provider_error"
        entry["updated_at"] = self._clock().astimezone(timezone.utc).isoformat()
        self._save()

    def snapshot(self, provider: str) -> dict[str, Any]:
        if provider not in self._limits:
            return {}
        entry = self._entry(provider)
        limit = _as_int(entry.get("limit_tokens"))
        used = _as_int(entry.get("used_tokens")) or 0
        remaining = _as_int(entry.get("remaining_tokens"))
        if remaining is None and limit is not None:
            remaining = max(0, limit - used)
        return {
            "daily_limit_tokens": limit,
            "daily_used_tokens": used,
            "daily_remaining_tokens": remaining,
            "daily_safety_margin_tokens": _as_int(entry.get("safety_margin_tokens")) or 0,
            "blocked_until": entry.get("blocked_until"),
            "source": entry.get("source"),
        }

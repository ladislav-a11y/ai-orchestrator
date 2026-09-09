"""Provider-local rolling rate limiter for the Groq Free Plan.

The limiter owns only Groq's four published dimensions.  It does not select
providers and it does not inspect task results.  A reservation is made before
each physical API request and reconciled with the provider-reported usage
afterwards.  The daily window is deliberately rolling for local protection;
provider-supplied reset evidence remains authoritative when Groq reports it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading
import uuid
from typing import Any, Callable, Optional


GROQ_LIMITS = {
    "rpm": 30,
    "rpd": 1000,
    "tpm": 8000,
    "tpd": 200000,
}
MINUTE_WINDOW = timedelta(seconds=60)
DAY_WINDOW = timedelta(days=1)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class GroqReservation:
    reservation_id: str
    estimated_tokens: int
    reserved_at: str


class GroqRateLimitExceeded(Exception):
    """Raised when a local RPM/TPM/RPD/TPD check blocks an API call."""

    def __init__(self, status: dict[str, Any]):
        self.status = status
        super().__init__(status.get("reason") or "Groq rate limit reached")


class GroqRateLimiter:
    """v2 persisted, sequential limiter for one Groq account/model.

    This is provider-owned state. It never selects a provider and never
    evaluates a task result; it only reports whether a next Groq API request
    can be reserved under the four configured free-tier dimensions.
    """

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.path = Path(path) if path is not None else None
        self._clock = clock
        self._lock = threading.RLock()
        self._state: dict[str, Any] = {"version": 1, "events": []}
        self._load()

    def _load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("events"), list):
                self._state = {"version": 1, "events": list(raw["events"])}
        except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
            # A corrupt local limiter state must not be mistaken for provider
            # confirmation. Start a fresh local window and let any real 429
            # become the authoritative status.
            self._state = {"version": 1, "events": []}

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        temporary.replace(self.path)

    def _prune(self, now: datetime) -> None:
        cutoff = now - DAY_WINDOW
        retained = []
        for event in self._state.get("events", []):
            if not isinstance(event, dict):
                continue
            timestamp = _parse_time(event.get("reserved_at"))
            if timestamp is not None and timestamp > cutoff:
                retained.append(event)
        self._state["events"] = retained

    def _events(self, now: datetime, window: timedelta) -> list[dict[str, Any]]:
        cutoff = now - window
        return [
            event
            for event in self._state["events"]
            if (timestamp := _parse_time(event.get("reserved_at"))) is not None
            and timestamp > cutoff
        ]

    @staticmethod
    def _token_sum(events: list[dict[str, Any]]) -> int:
        return sum(max(0, int(event.get("tokens", 0))) for event in events)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = self._clock().astimezone(timezone.utc)
            self._prune(now)
            minute = self._events(now, MINUTE_WINDOW)
            day = self._events(now, DAY_WINDOW)
            return {
                "limits": dict(GROQ_LIMITS),
                "usage": {
                    "rpm": len(minute),
                    "rpd": len(day),
                    "tpm": self._token_sum(minute),
                    "tpd": self._token_sum(day),
                },
                "remaining": {
                    "rpm": max(0, GROQ_LIMITS["rpm"] - len(minute)),
                    "rpd": max(0, GROQ_LIMITS["rpd"] - len(day)),
                    "tpm": max(0, GROQ_LIMITS["tpm"] - self._token_sum(minute)),
                    "tpd": max(0, GROQ_LIMITS["tpd"] - self._token_sum(day)),
                },
                "checked_at": _iso(now),
            }

    def status(self) -> Optional[dict[str, Any]]:
        """Return a v2 LIMITED status only when a next request is blocked."""
        snapshot = self.snapshot()
        limited_dimensions = [
            name for name in ("rpm", "rpd", "tpm", "tpd")
            if snapshot["remaining"][name] <= 0
        ]
        if not limited_dimensions:
            return None
        return {
            "state": "LIMITED",
            "limited_dimensions": limited_dimensions,
            "limits": snapshot["limits"],
            "usage": snapshot["usage"],
            "remaining": snapshot["remaining"],
            "checked_at": snapshot["checked_at"],
            "reason": (
                "Lokální Groq limiter blokuje další požadavek kvůli limitu: "
                + ", ".join(limited_dimensions)
            ),
            "source": "provider_local_rate_limiter",
        }

    def _reset_for(self, events: list[dict[str, Any]], window: timedelta) -> Optional[str]:
        timestamps = [_parse_time(event.get("reserved_at")) for event in events]
        timestamps = [timestamp for timestamp in timestamps if timestamp is not None]
        return _iso(min(timestamps) + window) if timestamps else None

    def reserve(self, estimated_tokens: int) -> GroqReservation:
        estimated = max(1, int(estimated_tokens))
        with self._lock:
            now = self._clock().astimezone(timezone.utc)
            self._prune(now)
            minute = self._events(now, MINUTE_WINDOW)
            day = self._events(now, DAY_WINDOW)
            current = {
                "rpm": len(minute),
                "rpd": len(day),
                "tpm": self._token_sum(minute),
                "tpd": self._token_sum(day),
            }
            required = {"rpm": 1, "rpd": 1, "tpm": estimated, "tpd": estimated}
            violated = [
                name
                for name, limit in GROQ_LIMITS.items()
                if current[name] + required[name] > limit
            ]
            if violated:
                reset_candidates = []
                for name in violated:
                    events = minute if name in {"rpm", "tpm"} else day
                    reset = self._reset_for(events, MINUTE_WINDOW if name in {"rpm", "tpm"} else DAY_WINDOW)
                    if reset:
                        reset_candidates.append(reset)
                retry_at = max(reset_candidates) if reset_candidates else None
                status = {
                    "state": "LIMITED",
                    "limited_dimensions": violated,
                    "limits": dict(GROQ_LIMITS),
                    "usage": current,
                    "requested": required,
                    "remaining": {
                        name: max(0, GROQ_LIMITS[name] - current[name])
                        for name in GROQ_LIMITS
                    },
                    "retry_at": retry_at,
                    "checked_at": _iso(now),
                    "reason": (
                        "Lokální Groq limiter odmítl požadavek kvůli limitu: "
                        + ", ".join(violated)
                    ),
                    "source": "provider_local_rate_limiter",
                }
                raise GroqRateLimitExceeded(status)

            reservation = GroqReservation(uuid.uuid4().hex, estimated, _iso(now))
            self._state["events"].append(
                {
                    "reservation_id": reservation.reservation_id,
                    "reserved_at": reservation.reserved_at,
                    "tokens": estimated,
                    "status": "reserved",
                }
            )
            self._save()
            return reservation

    def settle(
        self,
        reservation: GroqReservation,
        actual_tokens: Optional[int],
        *,
        uncertain: bool = False,
    ) -> None:
        with self._lock:
            for event in self._state["events"]:
                if event.get("reservation_id") != reservation.reservation_id:
                    continue
                if actual_tokens is not None:
                    event["tokens"] = max(0, int(actual_tokens))
                    event["status"] = "completed"
                elif not uncertain:
                    event["tokens"] = 0
                    event["status"] = "released"
                else:
                    # An accepted request without usage metadata must remain
                    # conservatively reserved until its rolling window ends.
                    event["status"] = "usage_unknown"
                break
            self._save()

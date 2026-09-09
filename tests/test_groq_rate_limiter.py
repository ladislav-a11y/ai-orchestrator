from datetime import datetime, timedelta, timezone

import pytest

from orchestrator.agents.groq_rate_limiter import (
    GroqRateLimitExceeded,
    GroqRateLimiter,
)


def test_v2_reservation_is_reconciled_with_provider_usage():
    now = datetime(2026, 9, 9, tzinfo=timezone.utc)
    limiter = GroqRateLimiter(clock=lambda: now)

    reservation = limiter.reserve(100)
    limiter.settle(reservation, 37)

    snapshot = limiter.snapshot()
    assert snapshot["usage"] == {"rpm": 1, "rpd": 1, "tpm": 37, "tpd": 37}


def test_v2_limiter_reports_each_exhausted_dimension():
    now = datetime(2026, 9, 9, tzinfo=timezone.utc)
    limiter = GroqRateLimiter(clock=lambda: now)
    for _ in range(30):
        limiter.reserve(1)

    with pytest.raises(GroqRateLimitExceeded) as raised:
        limiter.reserve(1)

    status = raised.value.status
    assert status["state"] == "LIMITED"
    assert "rpm" in status["limited_dimensions"]
    assert status["limits"]["rpm"] == 30
    assert status["retry_at"] is not None


def test_v2_rolling_window_releases_old_reservations():
    current = [datetime(2026, 9, 9, tzinfo=timezone.utc)]
    limiter = GroqRateLimiter(clock=lambda: current[0])
    limiter.reserve(8000)
    current[0] += timedelta(seconds=61)

    reservation = limiter.reserve(1)
    limiter.settle(reservation, 1)
    assert limiter.snapshot()["usage"]["tpm"] == 1

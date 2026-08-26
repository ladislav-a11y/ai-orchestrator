"""Best-effort Slack status notifications for unattended orchestration."""

from __future__ import annotations

import json
import logging
import os
from urllib.request import Request, urlopen

logger = logging.getLogger("orchestrator")


def notify(message: str) -> None:
    """Post status through the configured incoming webhook without blocking work."""
    if os.environ.get("AI_PM_SLACK_ENABLED") != "1":
        return
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook:
        return
    request = Request(
        webhook,
        data=json.dumps({"text": message}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:  # noqa: S310 - configured Slack webhook
            if response.status != 200:
                logger.warning("Slack notification failed with HTTP %s", response.status)
    except Exception as exc:  # noqa: BLE001 - reporting must never break work
        logger.warning("Slack notification error: %s", exc)

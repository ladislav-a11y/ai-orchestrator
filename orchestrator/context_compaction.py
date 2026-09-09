"""Shared bounds for context crossing the PM -> AO dispatch boundary.

Canonical task text, Definition of Done and checkpoint data are never
silently truncated.  Only diagnostic/history fields may be compacted, and
they retain an explicit marker plus their newest tail.
"""

from __future__ import annotations

import json
from typing import Any


MAX_HISTORY_CHARS = 2400
MAX_PROJECT_STATUS_CHARS = 4000
MAX_TEST_OUTPUT_CHARS = 2000
MAX_CANONICAL_FIELD_CHARS = 16000
MAX_PLANNER_INPUT_CHARS = 16000

HISTORY_TRUNCATION_MARKER = "\n...[starší kontext zkrácen; zachován nejnovější stav]...\n"


class ContextOverflowError(ValueError):
    """A canonical dispatch field is too large to pass safely."""


def compact_history(value: Any, limit: int = MAX_HISTORY_CHARS) -> str:
    """Bound a diagnostic/history value while preserving its newest state."""
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    available = max(0, limit - len(HISTORY_TRUNCATION_MARKER))
    return (
        HISTORY_TRUNCATION_MARKER + text[-available:]
        if available
        else HISTORY_TRUNCATION_MARKER.strip()
    )


def require_canonical_text(value: Any, *, field: str, limit: int = MAX_CANONICAL_FIELD_CHARS) -> str:
    """Return canonical task text unchanged, or fail closed when oversized."""
    text = "" if value is None else str(value)
    if len(text) > limit:
        raise ContextOverflowError(
            f"refusing AI dispatch: canonical {field} exceeds the safe limit of {limit} characters"
        )
    return text


def require_planner_input(payload: dict) -> dict:
    """Validate the complete read-only Inbox payload before provider use."""
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > MAX_PLANNER_INPUT_CHARS:
        raise ContextOverflowError(
            "refusing Inbox planning: canonical source/project input exceeds "
            f"the safe limit of {MAX_PLANNER_INPUT_CHARS} characters"
        )
    return payload

"""Shared normalization for Definition-of-Done / --spec source text.

AI Project Manager appends a trailing "<!-- PM-CHECKPOINT {...} -->" HTML
comment to the spec text it hands to `--spec`, containing a fresh, random
"run_id" it mints on EVERY scheduler tick that resubmits "the same" card -
confirmed against real queue rows: two spec_text values for the same card
were byte-for-byte identical except inside this block. Anything that treats
spec text as an identity key for "is this the same autonomous goal" (queue
dedup in queue.py, DoD checkpoint file naming/hash in
autonomous_checkpoint.py) must compare/hash this normalized form, or every
resubmission looks like a brand new goal.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

_PM_CHECKPOINT_RE = re.compile(r"<!--\s*PM-CHECKPOINT.*?-->", re.DOTALL)
_PM_CHECKPOINT_PAYLOAD_RE = re.compile(
    r"<!--\s*PM-CHECKPOINT\s*(.*?)-->", re.DOTALL
)
_LIVE_RESULT_RE = re.compile(r"<!--\s*LIVE-RESULT\s*:.*?-->", re.DOTALL | re.IGNORECASE)


def normalize_spec_text(spec_text: Optional[str]) -> Optional[str]:
    if spec_text is None:
        return None
    normalized = _PM_CHECKPOINT_RE.sub("", spec_text)
    # Results are mutable observations for a stable DoD declaration. They
    # must not mint a new queue/checkpoint identity every time Trello adds a
    # fresh production observation.
    return _LIVE_RESULT_RE.sub("", normalized).strip()


def extract_pm_checkpoint(spec_text: Optional[str]) -> Optional[dict[str, Any]]:
    """Return a valid structured PM-CHECKPOINT envelope, if present.

    Trello/AI Project Manager is the authoritative task state.  In
    particular, an explicit ``"checkpoint": {}`` must be able to invalidate
    stale local autonomous progress after a controller correction.
    """
    if not spec_text:
        return None
    match = _PM_CHECKPOINT_PAYLOAD_RE.search(spec_text)
    if match is None:
        return None
    try:
        payload = json.loads(match.group(1))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None

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

import re
from typing import Optional

_PM_CHECKPOINT_RE = re.compile(r"<!--\s*PM-CHECKPOINT.*?-->", re.DOTALL)


def normalize_spec_text(spec_text: Optional[str]) -> Optional[str]:
    if spec_text is None:
        return None
    return _PM_CHECKPOINT_RE.sub("", spec_text).strip()

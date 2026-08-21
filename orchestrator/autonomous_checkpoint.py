"""Persistent Definition-of-Done checkpoints, kept across separate autonomous
runs (separate process invocations) of the same project+spec.

Problem this solves: `run_autonomous_loop` (see `autonomous.py`) already keeps
DoD progress monotonic *within* one run - but every new run (new `orchestrator
autonomous ...` invocation, e.g. after Ctrl+C, a session limit, or a crash)
used to start `parse_definition_of_done()` from scratch, so items already
verified done in a previous run had to be re-claimed and re-verified again,
burning iterations/tokens for zero new progress.

Design:
  - One checkpoint file per (project, exact spec text) pair, so switching a
    project between two different spec files never mixes up their progress
    and never silently reuses stale progress for a *changed* spec (see
    `checkpoint_path` / `_fingerprint`) - the file name itself is derived
    from a hash of the project path and the exact Definition-of-Done source
    text, so editing the spec (adding/removing/reordering a checklist item)
    naturally lands on a different file and the old checkpoint is simply
    never found (`load_checkpoint` also re-checks the hash and project path
    stored *inside* the file as defense in depth, and refuses to apply a
    checkpoint whose item texts do not line up 1:1 with the freshly parsed
    DoD list - see `apply_checkpoint`).
  - The orchestrator - never the agent - decides what a checkpoint contains:
    callers pass the already-verified `DoDItem` list from `autonomous.py`'s
    monotonic merge (see its module docstring), so a checkpoint can only ever
    persist state the orchestrator's own loop already trusted; nothing here
    reopens or re-derives "done" from an agent claim.
  - `save_checkpoint` writes atomically (`os.replace` of a temp file), so a
    process killed mid-write (Ctrl+C, OOM, crash) can never leave a
    half-written, corrupt checkpoint behind - the previous good checkpoint
    (or none) survives.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

CHECKPOINT_SUBDIR = "autonomous_checkpoints"


@dataclass
class DoDCheckpoint:
    project_path: str
    spec_hash: str
    goal: str
    items: list[dict]
    run_id: str
    saved_at: str


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def checkpoint_path(data_dir: Path, project_path: Path, dod_source: str) -> Path:
    """Deterministic path for the (project, exact spec text) pair. Nothing
    reads the file's *name* for validation - `load_checkpoint` also verifies
    the hash/path stored inside the file - but keying the name on the same
    fingerprint means a changed spec simply never resolves to the old file,
    which is what gives "safe invalidation on spec change" for free instead
    of relying on cache-style logic."""
    project_fp = _fingerprint(str(Path(project_path).resolve()))[:16]
    spec_fp = _fingerprint(dod_source)[:16]
    return data_dir / CHECKPOINT_SUBDIR / f"{project_fp}-{spec_fp}.json"


def load_checkpoint(data_dir: Path, project_path: Path, dod_source: str) -> Optional[DoDCheckpoint]:
    """Returns None whenever there is nothing safe to reuse: no file, an
    unreadable/corrupt file, or a file whose stored spec hash / project path
    does not match the caller's current spec/project exactly. Never raises -
    a missing or bad checkpoint must never abort an autonomous run, it just
    means the run starts from the DoD's own checkbox state, same as before
    this module existed."""
    path = checkpoint_path(data_dir, project_path, dod_source)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None

    spec_hash = _fingerprint(dod_source)
    if raw.get("spec_hash") != spec_hash:
        return None
    if raw.get("project_path") != str(Path(project_path).resolve()):
        return None
    items = raw.get("items")
    if not isinstance(items, list):
        return None

    return DoDCheckpoint(
        project_path=raw["project_path"],
        spec_hash=spec_hash,
        goal=raw.get("goal") or "",
        items=items,
        run_id=raw.get("run_id") or "",
        saved_at=raw.get("saved_at") or "",
    )


def apply_checkpoint(dod_items: list, checkpoint: DoDCheckpoint) -> int:
    """Restore `done=True` state from `checkpoint` onto a freshly parsed
    `dod_items` list, in place. Returns how many items were actually flipped
    from not-done to done (i.e. genuinely restored, not already done from the
    spec's own checkbox state).

    Deliberately all-or-nothing: if the checkpoint's item texts do not line
    up 1:1, in order, with the freshly parsed list (different count, or any
    text mismatch), nothing is applied and 0 is returned - a matching
    `spec_hash` should already guarantee this never happens (parsing is a
    pure function of the exact spec text the hash covers), but this is cheap
    insurance against ever silently mis-mapping one item's "done" state onto
    a different item.
    """
    saved_items = checkpoint.items
    if len(saved_items) != len(dod_items):
        return 0
    for item, saved in zip(dod_items, saved_items):
        if not isinstance(saved, dict) or saved.get("text") != item.text:
            return 0

    restored = 0
    for item, saved in zip(dod_items, saved_items):
        if bool(saved.get("done")) and not item.done:
            item.done = True
            restored += 1
    return restored


def save_checkpoint(
    data_dir: Path,
    project_path: Path,
    dod_source: str,
    goal: str,
    dod_items: list,
    run_id: str,
) -> Path:
    """Persist the orchestrator's current, verified DoD state. Called after
    every iteration (not just at the end of a run) so progress survives
    Ctrl+C, a session limit, or a crash - see module docstring. Writes to a
    temp file and `os.replace`s it into place so a write that is interrupted
    mid-way never corrupts the previously saved checkpoint."""
    path = checkpoint_path(data_dir, project_path, dod_source)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "project_path": str(Path(project_path).resolve()),
        "spec_hash": _fingerprint(dod_source),
        "goal": goal,
        "items": [{"text": item.text, "done": item.done} for item in dod_items],
        "run_id": run_id,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    tmp_path = path.with_name(path.name + f".{run_id}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)
    return path

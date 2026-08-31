"""Git / memory / skills / Trello integration checks for the Hermes Agent PoC
(DoD point 2).

The deterministic checks operate only on caller-supplied isolated paths and
never use the network. ``check_trello_api_reachable`` is the sole explicit
opt-in exception: a credential-free, read-only reachability check against the
real Trello API, never called by the offline E2E smoke or default tests.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping, Optional


# ---------------------------------------------------------------------------
# Git (read-only status/log against a caller-supplied, isolated repo path)
# ---------------------------------------------------------------------------


class GitCheckError(RuntimeError):
    pass


def git_read_only_status(repo_path: Path) -> dict[str, Any]:
    """Run only read-only Git commands (status, rev-parse, log) against
    `repo_path`. Never calls `add`/`commit`/`push`/anything mutating - proves
    a Hermes-style agent can observe repo state without needing write access
    to Git itself (commits stay the orchestrator's job, per AGENTS.md rule
    11, exactly like the existing providers)."""
    commands = {
        "is_inside_work_tree": ["git", "rev-parse", "--is-inside-work-tree"],
        "status_porcelain": ["git", "status", "--porcelain"],
        "current_branch": ["git", "rev-parse", "--abbrev-ref", "HEAD"],
    }
    results: dict[str, Any] = {}
    for key, cmd in commands.items():
        completed = subprocess.run(
            cmd, cwd=repo_path, capture_output=True, text=True, check=False
        )
        if completed.returncode != 0:
            raise GitCheckError(f"'{' '.join(cmd)}' failed: {completed.stderr.strip()}")
        results[key] = completed.stdout.strip()
    return results


def init_scratch_git_repo(scratch_dir: Path) -> Path:
    """Create and commit a throwaway Git repo under `scratch_dir/repo`, for
    callers that need a real (but disposable) repo to run `git_read_only_status`
    against - never this project's own repo. Shared by `e2e_smoke.py` and
    `point2_status.py` so both exercise the exact same real `git` calls."""
    repo = scratch_dir / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "hermes-poc@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Hermes PoC"], cwd=repo, check=True)
    (repo / "README.md").write_text("hermes poc scratch repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    return repo


# ---------------------------------------------------------------------------
# Memory (simple JSON-file-backed key/value store scoped to a given dir)
# ---------------------------------------------------------------------------


class MemoryStore:
    """Minimal persistent memory scoped to a single directory the caller
    controls. No global state, no writes outside `root`."""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, key: str) -> Path:
        safe_key = "".join(c if c.isalnum() or c in "-_" else "_" for c in key)
        return self.root / f"{safe_key}.json"

    def save(self, key: str, value: Any) -> None:
        self._path_for(key).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def load(self, key: str) -> Optional[Any]:
        path = self._path_for(key)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def list_keys(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.json"))


# ---------------------------------------------------------------------------
# Skills (pluggable named callables)
# ---------------------------------------------------------------------------


class SkillRegistry:
    def __init__(self):
        self._skills: dict[str, Callable[..., Any]] = {}

    def register(self, name: str, fn: Callable[..., Any]) -> None:
        self._skills[name] = fn

    def invoke(self, name: str, **kwargs: Any) -> Any:
        if name not in self._skills:
            raise KeyError(f"Unknown skill '{name}'. Registered: {sorted(self._skills)}")
        return self._skills[name](**kwargs)

    def names(self) -> list[str]:
        return sorted(self._skills)


def word_count_skill(text: str) -> int:
    """Example skill proving the registry can hold a real, pure callable."""
    return len(text.split())


# ---------------------------------------------------------------------------
# Trello (fake in-memory client - no real network call in this sandbox)
# ---------------------------------------------------------------------------


class FakeTrelloClient:
    """In-memory stand-in for a Trello REST client, matching the shape of
    calls this project's Trello handoff already documents (README.md ch.6:
    labels, LIVE-EVIDENCE/LIVE-RESULT comments, card comments) so the PoC can
    prove the *contract* is implementable without real Trello credentials or
    network access in this sandbox (see README.md "Sandbox limitations")."""

    def __init__(self):
        self._boards: dict[str, dict[str, Any]] = {}

    def seed_card(self, card_id: str, labels: list[str]) -> None:
        self._boards[card_id] = {"labels": list(labels), "comments": []}

    def get_labels(self, card_id: str) -> list[str]:
        if card_id not in self._boards:
            raise KeyError(f"Unknown card_id '{card_id}'")
        return list(self._boards[card_id]["labels"])

    def add_comment(self, card_id: str, text: str) -> None:
        if card_id not in self._boards:
            raise KeyError(f"Unknown card_id '{card_id}'")
        self._boards[card_id]["comments"].append(text)

    def get_comments(self, card_id: str) -> list[str]:
        if card_id not in self._boards:
            raise KeyError(f"Unknown card_id '{card_id}'")
        return list(self._boards[card_id]["comments"])


# ---------------------------------------------------------------------------
# Trello (real, keyless, read-only reachability/contract check)
# ---------------------------------------------------------------------------

TRELLO_API_BASE_URL = "https://api.trello.com/1"


def verify_trello_read_access(
    *, key: str, token: str, board_id: str, list_id: str, timeout_seconds: float = 10.0
) -> dict[str, Any]:
    """Verify real credentials and configured resources without mutating Trello.

    Only existence/relationship booleans and a card count are returned; secrets,
    card text, member data and board contents are deliberately never persisted.
    """
    if not all((key, token, board_id, list_id)):
        raise ValueError("Trello key, token, board ID and list ID are required")

    query = urllib.parse.urlencode({"key": key, "token": token, "fields": "idBoard"})
    request = urllib.request.Request(
        f"{TRELLO_API_BASE_URL}/lists/{urllib.parse.quote(list_id, safe='')}?{query}",
        headers={"User-Agent": "hermes-agent-poc/1.0 (read-only verification)"},
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        configured_list = json.loads(response.read().decode("utf-8"))

    cards_query = urllib.parse.urlencode(
        {"key": key, "token": token, "fields": "id", "filter": "open"}
    )
    cards_request = urllib.request.Request(
        f"{TRELLO_API_BASE_URL}/lists/{urllib.parse.quote(list_id, safe='')}/cards?{cards_query}",
        headers={"User-Agent": "hermes-agent-poc/1.0 (read-only verification)"},
    )
    with urllib.request.urlopen(cards_request, timeout=timeout_seconds) as response:
        cards = json.loads(response.read().decode("utf-8"))

    belongs_to_board = configured_list.get("idBoard") == board_id
    return {
        "authenticated": True,
        "list_read": True,
        "cards_read": True,
        "list_belongs_to_configured_board": belongs_to_board,
        "open_card_count": len(cards),
        "ok": belongs_to_board,
    }


def check_trello_api_reachable(timeout_seconds: float = 10.0) -> dict[str, Any]:
    """Real, live, read-only, credential-free check against the ACTUAL
    Trello REST API (`api.trello.com`) - added because `FakeTrelloClient`
    above is a pure in-memory mock and was correctly flagged by an
    independent audit as not real verification of "Trello" (see
    README.md "Zivé ověření dosažitelnosti Trello API").

    This project has no Trello API key/token anywhere (Trello integration
    is owned entirely by an external "AI Project Manager" tool - see
    README.md ch.6/9 and outbox/README.md; this orchestrator repo never
    stores Trello credentials), so a real board/card/label read is not
    possible from here without a human supplying credentials. What CAN be
    verified live, safely, and without any credential is the same class of
    fact `OllamaCliTransport.is_available()` or `hermes doctor` (no
    `--live`) verify for their own provider: that the real service exists,
    is reachable, and responds according to its documented contract.

    `GET /members/me` without a key/token is a stable, publicly documented
    Trello behavior: it deterministically returns HTTP 400 with a
    `text/plain` body `"invalid token"` - confirmed live from this sandbox.
    Never touches any board/card/data, never sends a credential, matches
    the "explicit, correct, credential-required" contract rather than a
    silent failure - the same distinction this project draws everywhere
    else between "unavailable" and "misconfigured"."""
    url = f"{TRELLO_API_BASE_URL}/members/me"
    request = urllib.request.Request(
        url, headers={"User-Agent": "hermes-agent-poc/1.0 (ai-orchestrator isolated PoC)"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as resp:
            return {
                "reachable": True,
                "http_status": resp.status,
                "body": resp.read().decode("utf-8", errors="replace")[:200],
                "note": "unexpected 2xx without credentials",
            }
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:200]
        return {
            "reachable": True,
            "http_status": exc.code,
            "body": body,
            "note": "real Trello API reached; HTTP 4xx expected without a key/token",
        }
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return {
            "reachable": False,
            "http_status": None,
            "body": None,
            "note": f"Trello API unreachable: {exc}",
        }


# ---------------------------------------------------------------------------
# Trello (offline, machine-readable status of the human-consent gate)
# ---------------------------------------------------------------------------

TRELLO_CREDENTIAL_ENV_VARS: tuple[str, ...] = (
    "TRELLO_KEY",
    "TRELLO_TOKEN",
    "TRELLO_BOARD_ID",
    "TRELLO_INBOX_LIST",
)
TRELLO_HUMAN_CONSENT_ENV_VAR = "TRELLO_LIVE_READ_HUMAN_CONSENT"
TRELLO_HUMAN_CONSENT_VALUE = "I_CONSENT_TO_SEND_TRELLO_CREDENTIALS"


def trello_authenticated_read_gate_status(
    env: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """Report, without any network call and without ever exposing a secret
    value, whether `verify_trello_read_access()` (via
    `live_trello_read_smoke.py`) is currently blocked and why.

    This only *observes* `TRELLO_LIVE_READ_HUMAN_CONSENT` - it never sets it.
    An autonomous agent must never grant that consent to itself (see
    `live_trello_read_smoke.py` module docstring); this function exists so
    that fact is machine-checkable in one place (e.g. from `e2e_smoke.py`)
    instead of only documented in prose in README.md.
    """
    source = env if env is not None else os.environ
    missing_credentials = [
        name for name in TRELLO_CREDENTIAL_ENV_VARS if not source.get(name)
    ]
    human_consent_present = source.get(TRELLO_HUMAN_CONSENT_ENV_VAR) == TRELLO_HUMAN_CONSENT_VALUE
    blocked = bool(missing_credentials) or not human_consent_present
    if missing_credentials:
        reason = f"missing environment variables: {missing_credentials}"
    elif not human_consent_present:
        reason = (
            f"credentials present but {TRELLO_HUMAN_CONSENT_ENV_VAR} was not set "
            "by a human right before this check"
        )
    else:
        reason = "unblocked: credentials and human consent are both present"
    return {
        "credentials_present": not missing_credentials,
        "human_consent_present": human_consent_present,
        "blocked": blocked,
        "reason": reason,
    }

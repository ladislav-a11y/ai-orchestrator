"""Autonomous development loop: implement -> test -> evaluate -> fix -> repeat.

See ARCHITECTURE.md ("Autonomní vývojový režim") for the full picture. This
reuses the same `Agent` interface, the same test-running helper and the same
"never commit on failing tests" rule as `runner.py` - autonomous mode is not a
new trust boundary, just a loop around the same pipeline building blocks.

Hard safety limits (do not weaken without an explicit user request, see
AGENTS.md):
  - `max_iterations` is always capped at `ABSOLUTE_MAX_ITERATIONS` - the loop
    can never run "forever" no matter what a caller passes in.
  - if the same (unmet Definition-of-Done items, test result) signature
    repeats `NO_PROGRESS_LIMIT` iterations in a row, the loop stops and is
    reported as `blocked` instead of silently looping. Only iterations with
    a verified, comparable state count towards this - an iteration where the
    agent's JSON was unparsable/incomplete (even after the cheap repair
    reprompt below), where the independent audit pass could not be parsed,
    or where a configured test command was somehow not actually run, is
    recorded as a protocol error and excluded from the no-progress
    comparison instead of being treated as "no progress" (see
    `_apply_dod_updates` and the `protocol_error` handling in
    `run_autonomous_loop`).
  - a protocol error is NOT free to repeat forever just because it is
    excluded from the no-progress signature above (see incident run
    7fffd21835174d9fb9a29237c897f6d2: Codex made real changes and passed
    tests in iterations 1-7, but never once returned the required DoD JSON,
    the single repair reprompt failed every time too, and the run kept
    starting brand new full implementation iterations against an unchanged
    DoD until it burned through the provider's whole usage limit). A
    separate counter, `PROTOCOL_ERROR_STREAK_LIMIT`, tracks *consecutive*
    unresolved protocol errors (i.e. still broken after the one cheap
    repair). Once that low threshold is hit: if `agent` exposes
    `force_failover_on_protocol_error()` (see agents/failover.py) and
    another configured provider is available, the run fails over to it and
    keeps going (a repeated protocol violation is exactly the kind of
    provider-side incompatibility failover exists for, not just quota/rate
    limits); otherwise the run stops immediately with
    `AutonomousStatus.PROTOCOL_ERROR` instead of continuing to spend
    iterations/tokens on an agent that cannot follow the contract.
  - a commit is only ever attempted when every Definition of Done item is
    marked done AND the orchestrator's own test run (never the agent's
    claim) passed on that same iteration AND the independent audit pass
    (see below) did not reject any item.
  - Definition-of-Done items are only ever parsed from explicit checklist
    lines (`- [ ] ...` / `- [x] ...`); once an item is verified done by the
    orchestrator's own merge, a later *executor* claim can never un-mark it
    (see `parse_definition_of_done` and `_apply_dod_updates`) - only the
    independent audit pass may reopen a falsely-claimed item.

Cost/reliability design (see run 11b4aaae08b4, where 8 iterations in a row
had tests_passed=True but were misreported as protocol_error, burning a
whole session's budget for zero recorded progress - root cause: the CLI
wrapper appended a human-readable note after the agent's own JSON payload,
and the parser did a naive full-string `json.loads` with no tolerance for
that trailing text):
  - `_extract_json` tolerates surrounding prose, a markdown fence, or
    trailing text after the JSON object (it scans for balanced `{...}`
    objects instead of requiring the whole message to be pure JSON).
  - a genuinely malformed/incomplete response gets exactly one cheap repair
    reprompt (`_build_repair_prompt`) asking the agent to *only* resend the
    JSON for the still-missing indices - never a fresh full implementation
    iteration.
  - each iteration only ever asks about a small batch of currently-unmet DoD
    items (`DOD_BATCH_SIZE`), not the whole list every time, to keep prompts
    (and the agent's required response) small regardless of how large the
    Definition of Done is.
  - once the executor claims every item done and the orchestrator's own test
    run agrees, a separate, independent "audit" pass (`_run_audit`) reviews
    the claim before a commit is attempted - inspired by the
    Manager/Executor/Auditor split (no dependency on lh-harness: this is a
    second prompt against the same `Agent`, instructed to verify only, never
    to implement).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from orchestrator.agents.base import Agent, AgentRunRequest
from orchestrator.config import Config
from orchestrator.git_utils import (
    GitError,
    commit as git_commit,
    current_branch,
    current_head,
    has_uncommitted_changes,
    is_git_repo,
    origin_url,
    remote_branch_head,
    status_porcelain,
)
from orchestrator.runner import run_test_command, tail_text
from orchestrator.slack_notify import notify

DEFAULT_MAX_ITERATIONS = 10
# Sanity ceiling - independent of what a caller/CLI flag requests, so a typo
# like "--max-iterations 9999" can never turn this into an unbounded loop.
ABSOLUTE_MAX_ITERATIONS = 50
# How many consecutive iterations with an unchanged (unmet DoD items, test
# result) signature before the run is declared "blocked" (no progress).
NO_PROGRESS_LIMIT = 3
# How many consecutive iterations with an unresolved protocol error (agent's
# JSON still invalid/incomplete even after the one cheap repair reprompt)
# before the run gives up on the current provider - see module docstring
# and incident run 7fffd21835174d9fb9a29237c897f6d2. Deliberately low (not
# NO_PROGRESS_LIMIT): a protocol error carries no information about whether
# the underlying task is stuck, so there is no reason to give it as many
# free passes as genuine no-progress gets.
PROTOCOL_ERROR_STREAK_LIMIT = 2
# Max number of currently-unmet DoD items presented to the agent in a single
# iteration. Keeps the prompt (and the required JSON response) small and
# reliable regardless of how large the overall Definition of Done is - a
# real run against a 66-item spec is what originally motivated this (see
# module docstring): asking for a 66-entry JSON response every iteration is
# both expensive and fragile.
DOD_BATCH_SIZE = 8
HERMES_DOD_BATCH_SIZE = 1
RUNTIME_CONTRACT_PATH = Path(__file__).resolve().parents[2] / "AI_PROJECT_RUNTIME.md"


def _runtime_contract_text() -> str:
    """Load the machine contract; the human handbook is never prompted."""
    return RUNTIME_CONTRACT_PATH.read_text(encoding="utf-8").strip()

# Marker line that opens every independent audit prompt, so a caller can
# recognize (and tests can simulate) the audit role distinctly from the
# executor role, even though both go through the same `Agent`.
AUDIT_MARKER = "AUDITORSKÁ KONTROLA"

# Some Trello cards include the final controller/audit gate in their visible
# DoD (for example, "ai-orchestrator vydá accepted/rejected verdikt").  That
# is evidence owned by this orchestrator, not implementation work an executor
# can complete.  Keep the item in the final audit contract, but do not ask an
# executor to repeat the implementation loop just to claim it.
_CONTROLLER_AUDIT_GATE_RE = re.compile(
    r"accepted\s*/\s*rejected.*(?:ai[- ]orchestrator|audit)"
    r"|(?:ai[- ]orchestrator|audit).*accepted\s*/\s*rejected"
    r"|(?:nezávisl|independent)\w*\s+audit",
    re.IGNORECASE,
)


def _controller_audit_gate_indices(items: list[DoDItem]) -> set[int]:
    """Return DoD indices owned by the orchestrator's final audit gate."""
    return {
        index for index, item in enumerate(items)
        if item.live_command is None and _CONTROLLER_AUDIT_GATE_RE.search(item.text or "")
    }


def controller_finalization_from_spec(spec_text: str) -> Optional[dict]:
    """Extract a controller proof only from an explicit audit-mode spec."""
    if not re.search(r'"mode"\s*:\s*"audit"', spec_text or ""):
        return None
    decoder = json.JSONDecoder()
    for match in re.finditer(r"(?m)^\s*\{", spec_text or ""):
        try:
            value, _ = decoder.raw_decode(spec_text[match.start():])
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        candidates = [value.get("finalization")]
        checkpoint = value.get("checkpoint")
        if isinstance(checkpoint, dict):
            candidates.insert(0, checkpoint.get("finalization"))
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get("commit_hash"):
                return candidate
    return None


def controller_finalization_is_current(project_path: Path, finalization: object) -> bool:
    """Verify a persisted controller proof without invoking an AI provider."""
    if not isinstance(finalization, dict):
        return False
    required = (
        finalization.get("status") == "completed"
        and finalization.get("done") is True
        and isinstance(finalization.get("committed"), bool)
        and finalization.get("clean") is True
        and finalization.get("tests_passed") is True
        and finalization.get("pushed") is True
        and bool(finalization.get("commit_hash"))
        and finalization.get("remote_commit") == finalization.get("commit_hash")
    )
    if not required or not is_git_repo(project_path):
        return False
    head = current_head(project_path)
    branch = current_branch(project_path)
    return bool(
        head
        and head == finalization.get("commit_hash")
        and branch == finalization.get("branch")
        and not status_porcelain(project_path)
        and origin_url(project_path) == finalization.get("remote")
        and remote_branch_head(project_path, branch) == head
    )


class AutonomousStatus(str, Enum):
    RUNNING = "running"
    WAITING_FOR_PROVIDER = "waiting_for_provider"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    MAX_ITERATIONS = "max_iterations"
    ERROR = "error"
    # A protocol error (agent JSON invalid/incomplete even after the one
    # cheap repair reprompt) repeated PROTOCOL_ERROR_STREAK_LIMIT times in a
    # row with no other provider left to fail over to - see module
    # docstring and PROTOCOL_ERROR_STREAK_LIMIT. Distinct from BLOCKED
    # (which means genuinely no progress on a verified state) so callers
    # and the Trello/Slack handoff can report the real reason instead of a
    # generic "stuck".
    PROTOCOL_ERROR = "protocol_error"
    # This job's cumulative reported spend on the active provider exceeded
    # that provider's configured max_budget_usd (see _provider_budget_usd)
    # and no other configured provider was available to fail over to - a
    # financial safety cap the orchestrator enforces itself, distinct from
    # WAITING_FOR_PROVIDER (a provider-reported quota/rate limit).
    BUDGET_EXCEEDED = "budget_exceeded"


@dataclass
class DoDItem:
    text: str
    done: bool = False
    # Optional orchestrator-run verification for integration/production
    # assertions.  The agent may claim the item, but it cannot make it done:
    # a zero exit code and the expected stdout/stderr fragment are required.
    live_command: Optional[str] = None
    live_expected: Optional[str] = None
    live_evidence: Optional[dict] = None


@dataclass
class IterationLog:
    index: int
    prompt: str
    agent_output: str
    agent_error: Optional[str]
    tests_passed: Optional[bool]
    test_output: Optional[str]
    dod_snapshot: list[dict]
    note: str = ""
    protocol_error: bool = False
    # Which global DoD indices were actually asked about this iteration
    # (see DOD_BATCH_SIZE) - empty when nothing was unmet and the executor
    # call was skipped entirely (straight to test+audit verification).
    requested_indices: list[int] = field(default_factory=list)
    # len(prompt) for the main executor prompt, logged so prompt-size/token
    # consumption is visible per iteration (see module docstring).
    prompt_chars: int = 0
    # Whether the cheap single repair reprompt (see module docstring) was
    # attempted this iteration because the first response was malformed.
    repair_attempted: bool = False
    repair_succeeded: bool = False
    # Whether the independent audit pass ran this iteration, and what it
    # found - only set once the executor claims every DoD item is done and
    # the orchestrator's own tests agree.
    audit_performed: bool = False
    audit_rejected_indices: list[int] = field(default_factory=list)
    audit_protocol_error: bool = False
    # Whether the one allowed cheap audit repair reprompt was attempted this
    # iteration (mirrors AuditOutcome.audit_repair_attempted).
    audit_repair_attempted: bool = False
    agent_name: Optional[str] = None
    usage: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class AutonomousResult:
    status: AutonomousStatus
    iterations: list[IterationLog] = field(default_factory=list)
    dod_items: list[DoDItem] = field(default_factory=list)
    committed: bool = False
    commit_hash: Optional[str] = None
    error: Optional[str] = None
    retry_after_seconds: Optional[float] = None
    # How many DoD items were already done() at the very start of this run
    # because a checkpoint from an earlier, separate run was restored (see
    # autonomous_checkpoint.py / OrchestratorService.run_autonomous) - 0 for
    # a run that started clean. Set by the caller after the loop returns;
    # run_autonomous_loop itself has no knowledge of checkpoints.
    restored_from_checkpoint: int = 0
    # How many repeated test-invocation attempts the PreToolUse circuit
    # breaker (orchestrator/hooks/test_command_guard.py) short-circuited
    # across every agent.run() call in this run (executor + repair + audit)
    # - see AgentRunResult.breaker_saved_attempts.
    breaker_saved_attempts: int = 0
    # Total number of iterations in this run whose executor or audit
    # response was an unresolved protocol error (invalid/incomplete JSON
    # even after the one cheap repair reprompt) - see
    # PROTOCOL_ERROR_STREAK_LIMIT. Reported so a caller (CLI output, the
    # outbox handoff to AI Project Manager, Slack) can see how much of a
    # run's iteration/token budget was wasted on protocol violations rather
    # than real work, even on a run that did not end in PROTOCOL_ERROR.
    protocol_error_total: int = 0
    # Sum of len(prompt) (main executor prompt + its repair reprompt, when
    # attempted) for every iteration counted in protocol_error_total - a
    # cheap, dependency-free stand-in for wasted token usage, in the same
    # units as IterationLog.prompt_chars/the module's "~tokens odhadem" log
    # lines (chars // 4).
    protocol_error_wasted_prompt_chars: int = 0
    # Whether *any* iteration in this run attempted the cheap audit repair
    # reprompt: tracks (max 1 per iter) so the outbox/CLI can report whether
    # audit recovery was tried at all. Aggregated by `snapshot()` from the
    # per-iteration logs. Mirrors per-iteration IterationLog.audit_repair_attempted.
    audit_repair_attempted: bool = False
    # Reported provider metadata only. Missing usage is represented by an
    # empty list/totals with null values and never fails the run.
    usage_events: list[dict] = field(default_factory=list)
    usage_by_provider: dict[str, dict] = field(default_factory=dict)
    usage_total: dict = field(default_factory=dict)
    # Per-provider availability/limit receipt from FailoverAgent. Sequence
    # records who was called; this preserves every provider's retry deadline.
    provider_statuses: dict[str, dict] = field(default_factory=dict)


# -- Definition of Done parsing ---------------------------------------------

_CHECKBOX_RE = re.compile(r"^[-*]\s*\[([ xX])\]\s+(.+)$")
_INLINE_CHECKBOX_RE = re.compile(r"\[([ xX])\]\s*([^\[]+?)(?=\s*\[[ xX]\]|\s*$)")
_LIVE_EVIDENCE_RE = re.compile(r"\s*<!--\s*LIVE-EVIDENCE\s*:\s*(\{.*\})\s*-->\s*$", re.IGNORECASE)
_LIVE_RESULT_RE = re.compile(r"<!--\s*LIVE-RESULT\s*:\s*(\{.*?\})\s*-->", re.IGNORECASE)


def _parse_item(text: str, done: bool) -> DoDItem:
    """Parse an optional machine-readable live verification declaration.

    Syntax (kept inside a Markdown comment so Trello/spec prose stays tidy):
    ``item <!-- LIVE-EVIDENCE: {"command":"...","expect":"..."} -->``.
    Invalid/incomplete metadata is intentionally left in the item text and
    never silently treated as a live-verified item.
    """
    match = _LIVE_EVIDENCE_RE.search(text)
    if not match:
        return DoDItem(text=text.strip(), done=done)
    try:
        metadata = json.loads(match.group(1))
    except json.JSONDecodeError:
        return DoDItem(text=text.strip(), done=done)
    command = metadata.get("command") if isinstance(metadata, dict) else None
    expected = metadata.get("expect") if isinstance(metadata, dict) else None
    if not isinstance(command, str) or not command.strip() or not isinstance(expected, str):
        return DoDItem(text=text.strip(), done=done)
    return DoDItem(
        text=text[: match.start()].strip(), done=done,
        live_command=command.strip(), live_expected=expected,
    )


def _dod_dict(item: DoDItem) -> dict:
    return {
        "text": item.text,
        "done": item.done,
        "live_verification": (
            {"command": item.live_command, "expect": item.live_expected}
            if item.live_command is not None else None
        ),
        "live_evidence": item.live_evidence,
    }


def _usage_from_result(result, provider: Optional[str], stage: str, iteration: int) -> list[dict]:
    events = list(result.usage_events or [])
    if not events:
        event = {
            "provider": provider or "unknown", "source": "reported",
            "model": result.model,
            "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
            "thinking_tokens": result.thinking_tokens, "total_tokens": result.total_tokens,
            "cost_usd": result.cost_usd,
        }
        if any(event[k] is not None for k in ("input_tokens", "output_tokens", "thinking_tokens", "total_tokens", "cost_usd")):
            events = [event]
    return [{**event, "stage": stage, "iteration": iteration} for event in events]


def _usage_summary(events: list[dict]) -> tuple[dict[str, dict], dict]:
    fields = ("input_tokens", "output_tokens", "thinking_tokens", "total_tokens", "cost_usd")
    by_provider: dict[str, dict] = {}
    for event in events:
        bucket = by_provider.setdefault(event.get("provider") or "unknown", {key: None for key in fields})
        for key in fields:
            value = event.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                bucket[key] = (bucket[key] or 0) + value
        bucket["source"] = "reported"
    total = {key: None for key in fields}
    for bucket in by_provider.values():
        for key in fields:
            if bucket.get(key) is not None:
                total[key] = (total[key] or 0) + bucket[key]
    total["source"] = "reported" if by_provider else None
    return by_provider, total


def _provider_budget_usd(config: Config, provider_name: Optional[str]) -> Optional[float]:
    """Look up the per-job financial cap configured for `provider_name`
    (see ClaudeCodeAgentConfig/AntigravityAgentConfig/CodexAgentConfig's
    max_budget_usd). None means "no limit configured" - never treated as a
    zero-budget cap."""
    mapping = {
        "claude-code": config.claude_code.max_budget_usd,
        "antigravity": config.antigravity.max_budget_usd,
        "codex": config.codex.max_budget_usd,
    }
    return mapping.get(provider_name or "")


def _apply_declared_live_results(spec_text: str, items: list[DoDItem]) -> None:
    """Attach externally performed, read-only live checks to DoD items.

    The orchestrator deliberately does not execute commands embedded in a
    Trello/spec string. AI Project Manager (or a human operator) performs
    the declared check and appends a result comment containing ``index``,
    integer ``exit_code`` and string ``output``. Pass/fail is derived here,
    never trusted from a producer-supplied boolean.
    """
    for match in _LIVE_RESULT_RE.finditer(spec_text):
        try:
            result = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if not isinstance(result, dict):
            continue
        index, exit_code, output = result.get("index"), result.get("exit_code"), result.get("output")
        if (
            not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(items)
            or not isinstance(exit_code, int) or isinstance(exit_code, bool)
            or not isinstance(output, str)
        ):
            continue
        item = items[index]
        if item.live_command is None:
            continue
        passed = exit_code == 0 and (item.live_expected or "") in output
        item.live_evidence = {
            "command": item.live_command,
            "expect": item.live_expected,
            "exit_code": exit_code,
            "output": tail_text(output, 2000),
            "passed": passed,
        }


def _enforce_live_evidence(items: list[DoDItem]) -> None:
    """An agent claim or checked box cannot replace integration evidence."""
    for item in items:
        if item.live_command is not None and not (
            isinstance(item.live_evidence, dict) and item.live_evidence.get("passed") is True
        ):
            item.done = False


def parse_definition_of_done(spec_text: str) -> list[DoDItem]:
    """Split a free-form spec into individual Definition-of-Done items.

    Only lines matching a checklist checkbox ("- [ ] ..." / "- [x] ...", or
    the same with "*") ever become a DoD item - checkbox state is kept as
    the starting `done` value. Everything else (markdown headings, blank
    lines, plain prose, plain "- ..." bullets without a checkbox, numbered
    lists) is deliberately ignored: a real spec file like
    "dod-station-agent-v1.md" mixes "# Heading" / "## Section" lines with
    "- [ ] ..." checklist items, and a heading can never be "done" - if it
    were parsed as an item, the Definition of Done could never be fully
    satisfied and the run would misreport a real completion as blocked or
    stuck at max_iterations forever.

    A spec with zero checkbox lines (e.g. a plain one-line --goal with no
    --spec file at all) falls back to a single item holding the whole text,
    so a goal-only invocation still always produces >=1 item.
    """
    items: list[DoDItem] = []
    for raw_line in spec_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = _CHECKBOX_RE.match(line)
        if m:
            items.append(_parse_item(m.group(2).strip(), m.group(1).lower() == "x"))
            continue
        if "[" in line:
            for inline_match in _INLINE_CHECKBOX_RE.finditer(line):
                text = inline_match.group(2).strip()
                if text:
                    items.append(_parse_item(text, inline_match.group(1).lower() == "x"))
    if not items:
        stripped = spec_text.strip()
        if stripped:
            items.append(DoDItem(text=stripped))
    else:
        # AI Project Manager specs intentionally repeat the Trello task in
        # the Goal section and render the same requirements again as a
        # Markdown checklist. Treat equal requirement text as one DoD item,
        # otherwise a 9-point card becomes 18 points and burns extra provider
        # iterations. Contradictory checkbox states merge conservatively.
        unique: dict[str, DoDItem] = {}
        for item in items:
            existing = unique.get(item.text)
            if existing is None:
                unique[item.text] = item
                continue
            existing.done = existing.done and item.done
            if existing.live_command is None and item.live_command is not None:
                existing.live_command = item.live_command
                existing.live_expected = item.live_expected
        items = list(unique.values())
    _apply_declared_live_results(spec_text, items)
    _enforce_live_evidence(items)
    return items


# -- agent <-> JSON evaluation contract --------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _iter_balanced_objects(text: str):
    """Yield every top-level, brace-balanced `{...}` substring of `text`.

    String contents (including escaped quotes/braces inside them) are
    tracked so braces inside JSON string values never confuse the depth
    count. This is what lets `_extract_json` recover a valid JSON object
    even when the agent's response has extra prose or a stray note before
    or after it (see module docstring - real run 11b4aaae08b4 had exactly
    this shape: valid JSON immediately followed by a plain-text note)."""
    depth = 0
    start = None
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    yield text[start : i + 1]
                    start = None


def _extract_json(text: str) -> Optional[dict]:
    """Best-effort recovery of the single JSON object an agent was asked to
    return as its last message. Tries, in order: the whole message as-is,
    the contents of a markdown code fence, then every balanced `{...}`
    substring of the message (preferring the *last* one, since the contract
    asks for the JSON to be the final thing in the response) - the first of
    these that parses as a JSON object wins. Never raises; returns None if
    nothing in the text parses as a JSON object at all."""
    text = (text or "").strip()
    if not text:
        return None

    candidates: list[str] = [text]
    fence = _FENCE_RE.search(text)
    if fence:
        candidates.append(fence.group(1).strip())
    candidates.extend(reversed(list(_iter_balanced_objects(text))))

    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _select_batch(dod_items: list[DoDItem], batch_size: int = DOD_BATCH_SIZE) -> list[int]:
    """Pick the next small batch of globally-unmet DoD item indices to ask
    the agent about, in original order. Returns [] once every item is
    already marked done - callers use that to skip the (expensive)
    executor call entirely and go straight to test+audit verification."""
    return [idx for idx, item in enumerate(dod_items) if not item.done][:batch_size]


def _agent_batch_size(agent: Agent) -> int:
    """Keep Nous-free Hermes handoffs to one concrete DoD item.

    Hermes has a materially smaller practical tool-call/output budget than
    the other production agents. The failover wrapper exposes its current
    provider, so a later iteration can widen again after Hermes hands off to
    another provider.
    """
    provider = getattr(agent, "active_provider_name", getattr(agent, "name", ""))
    return HERMES_DOD_BATCH_SIZE if provider == "hermes" else DOD_BATCH_SIZE


def _apply_dod_updates(
    dod_items: list[DoDItem], requested_indices: list[int], parsed: Optional[dict]
) -> tuple[str, bool, list[int]]:
    """Merge the agent's self-reported JSON into dod_items in place.

    The orchestrator has no independent way to check arbitrary natural-
    language DoD items itself, so it cannot fully "verify" the agent's
    claim - but it does not have to trust it blindly either: the merge is
    monotonic (`done = done or claimed_done`), so a DoD item already
    verified done in an earlier iteration can never be silently flipped
    back to not-done by a later, possibly sloppier executor response (only
    the independent audit pass may do that - see `_run_audit`). Actual
    completion is still independently gated on the orchestrator's own test
    run in `run_autonomous_loop`, not on the agent's claim.

    `requested_indices` is the batch that was actually asked about this
    round (see `_select_batch`/DOD_BATCH_SIZE) - only those indices need to
    be covered for the response to count as protocol-clean; a valid entry
    for an index outside the batch is still applied (harmless, and lets a
    generous agent report extra progress) but is not required.

    Returns (notes, protocol_error, missing_indices). protocol_error is True
    whenever the response is missing, not JSON, has no "items" list,
    contains any entry with a malformed/out-of-range index, or fails to
    cover every index in `requested_indices` - i.e. whenever the contract
    described in `_build_iteration_prompt` was not honestly followed.
    `missing_indices` (a subset of `requested_indices`) is what a repair
    reprompt should ask about again. Callers must not fold a protocol_error
    iteration into no-progress tracking (see AGENTS.md rule 9 /
    ARCHITECTURE.md): an agent that garbles its output format is not the
    same as an agent that is stuck.
    """
    if not parsed or not isinstance(parsed.get("items"), list):
        return (
            "Agent nevrátil platný JSON stav Definition of Done, ponechávám předchozí stav.",
            True,
            list(requested_indices),
        )

    entries = parsed["items"]
    seen: set[int] = set()
    malformed_entry = False
    valid_updates: list[tuple[int, bool]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            malformed_entry = True
            continue
        idx = entry.get("index")
        done = entry.get("done")
        if not (
            isinstance(idx, int)
            and not isinstance(idx, bool)
            and 0 <= idx < len(dod_items)
            and isinstance(done, bool)
        ):
            malformed_entry = True
            continue
        seen.add(idx)
        valid_updates.append((idx, done))

    missing = [idx for idx in requested_indices if idx not in seen]
    protocol_error = malformed_entry or bool(missing)

    if not protocol_error:
        for idx, done in valid_updates:
            dod_items[idx].done = dod_items[idx].done or done

    notes = parsed.get("notes")
    notes = notes if isinstance(notes, str) else ""
    if protocol_error:
        hint = (
            f"[protokol] JSON odpověď nepokrývala požadované indexy {missing} nebo obsahovala "
            'neplatný záznam - pole "items" musí mít pro každý požadovaný index přesně jeden '
            'záznam s celočíselným "index" a boolean "done".'
        )
        notes = f"{notes} {hint}".strip()
    return notes, protocol_error, missing


def _build_repair_prompt(requested_indices: list[int], dod_items: list[DoDItem]) -> str:
    """A short, cheap reprompt used at most once per iteration when the
    agent's first response didn't honestly follow the JSON contract (see
    `_apply_dod_updates`). Deliberately does NOT repeat the goal, the full
    DoD list, git status or test output - it only asks the agent to resend
    its own already-done evaluation in the right shape, explicitly telling
    it not to do any further implementation work. This must stay cheap: it
    is not a second implementation attempt (see module docstring)."""
    lines = [
        "Tvá poslední odpověď v této iteraci nebyla platný JSON stavový objekt podle zadaného "
        "kontraktu (nebo nepokryla všechny požadované indexy). NEDĚLEJ žádné další úpravy kódu "
        "ani nic dalšího neimplementuj - jen znovu nahlas stav již odvedené práce.",
        "",
        "Pošli VÝHRADNĚ jeden JSON objekt (žádný markdown blok, žádný text před ani za ním), "
        "s přesně jedním záznamem pro každý z těchto indexů:",
        '{"items": [{"index": 0, "done": true}], "notes": "strucne shrnuti"}',
        f"Požadované indexy: {requested_indices}.",
        "Položky pro kontext:",
    ]
    for idx in requested_indices:
        lines.append(f"{idx}. {dod_items[idx].text}")
    return "\n".join(lines)


def _build_audit_repair_prompt(
    dod_items: list[DoDItem],
    previous_test_output: Optional[str],
) -> str:
    """Cheap, bounded reprompt used at most once per audit when the auditor's
    first response was not valid audit JSON. Never a second full review pass -
    it only asks the auditor to resend its own evaluation in the right shape.

    Kept short: no goal repetition, no full project-status rebuild, no
    re-listing of every DoD item's full text unless the item text is short.
    The contract (schema shape) is restated because that is what broke.
    """
    lines = [
        "Tvá předchozí audit odpověď nebyla platný JSON podle zadaného kontraktu.",
        "NEDĚLEJ žádnou novou kontrolu, nic neměň - jen znovu pošli svůj výsledek "
        "v tomto tvaru (jenom JSON, bez markdownu, bez textu před ani za ním):",
        json.dumps(
            {
                "items": [
                    {"index": i, "accepted": True, "evidence": "kratky dulez pro index i"}
                    for i in range(len(dod_items))
                ],
                "notes": "strucne zduvodneni",
            },
            ensure_ascii=False,
            indent=2,
        ),
        "",
        "Každý index musí mít právě jeden záznam s 'accepted' (bool) a 'evidence' "
        "(konkretni soubor/symbol/test/live vystup).",
        f"Počet bodů k ověření: {len(dod_items)}.",
    ]
    if previous_test_output:
        lines.append("")
        lines.append("Výstup testů (k dispozici pro kontext):")
        lines.append(previous_test_output[:800])
    return "\n".join(lines)


def _dod_response_schema(requested_indices: list[int]) -> dict:
    """Strict final-response contract for implementation and repair calls."""
    return {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "minItems": len(requested_indices),
                "maxItems": len(requested_indices),
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer", "enum": requested_indices},
                        "done": {"type": "boolean"},
                    },
                    "required": ["index", "done"],
                    "additionalProperties": False,
                },
            },
            "notes": {"type": "string", "maxLength": 1200},
        },
        "required": ["items", "notes"],
        "additionalProperties": False,
    }


def _audit_response_schema(item_count: int) -> dict:
    """Strict final-response contract for the independent audit call."""
    indices = list(range(item_count))
    return {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "minItems": item_count,
                "maxItems": item_count,
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer", "enum": indices},
                        "accepted": {"type": "boolean"},
                        "evidence": {"type": "string"},
                    },
                    "required": ["index", "accepted", "evidence"],
                    "additionalProperties": False,
                },
            },
            "notes": {"type": "string"},
        },
        "required": ["items", "notes"],
        "additionalProperties": False,
    }


def _project_status_text(project_path: Path) -> str:
    if not is_git_repo(project_path):
        return "(projekt není Git repozitář, stav nelze zobrazit)"
    status = status_porcelain(project_path)
    return status.strip() or "(žádné neuložené změny)"


def _build_iteration_prompt(
    goal: str,
    dod_items: list[DoDItem],
    requested_indices: list[int],
    iteration: int,
    max_iterations: int,
    project_status: str,
    test_command: Optional[str],
    tests_passed: Optional[bool],
    test_output: Optional[str],
    previous_notes: str,
) -> str:
    """Build a compact per-iteration checkpoint instead of resending the
    whole Definition of Done (and everything else) every time: only the
    current batch of unmet items (`requested_indices`, see DOD_BATCH_SIZE)
    is listed in full, the rest of the DoD is summarized as a count, and the
    previous *verified* test result (from the orchestrator's own run, not
    the agent's claim - see `run_autonomous_loop`) plus the agent's own
    previous-iteration notes carry whatever continuity is needed. The Claude
    Code session itself is resumed (`--resume`) so conversational context
    from earlier iterations is not lost either."""
    done_count = sum(1 for item in dod_items if item.done)
    total = len(dod_items)
    lines = [
        f"Autonomní vývojová iterace {iteration}/{max_iterations}.",
        "",
        "Kanonický runtime contract (machine rules):",
        _runtime_contract_text(),
        "",
        f"Cíl projektu: {goal}",
        "",
        f"Definition of Done: {done_count}/{total} bodů celkem už ověřeno jako splněno "
        "(nesplněné body se hlásí kumulativně, jednou splněný bod se sem už nevrací). "
        f"Níže je dávka {len(requested_indices)} aktuálně NESPLNĚNÝCH bodů, na kterou se máš "
        "zaměřit v této iteraci:",
    ]
    for idx in requested_indices:
        item = dod_items[idx]
        lines.append(f"{idx}. [NESPLNĚNO] {item.text}")
        if item.live_command is not None:
            lines.append(
                f"   LIVE DŮKAZ VYŽADOVÁN: krok={item.live_command!r}; očekávaný výstup="
                f"{item.live_expected!r}. Bez LIVE-RESULT od externí integrace tento bod neoznačuj hotový."
            )

    lines += ["", "Aktuální stav projektu (git status --porcelain):", project_status]

    if test_command:
        if tests_passed is None:
            lines += ["", f"Testovací příkaz: {test_command} (výsledek z minulé iterace není k dispozici)"]
        else:
            lines += [
                "",
                f"Výsledek testů z minulé iterace, ověřeno orchestrátorem ({test_command}): "
                f"{'PROŠLY' if tests_passed else 'SELHALY'}",
            ]
            if not tests_passed and test_output:
                lines += ["Výstup testů (může být zkrácený):", tail_text(test_output, 2000)]

    if previous_notes:
        lines += ["", "Poznámka z předchozí iterace:", previous_notes]

    lines += [
        "",
        "Uprav projekt tak, aby splnil NESPLNĚNÉ body Definition of Done vypsané výše. Pokud "
        "testy z minulé iterace selhaly, nejdřív oprav příčinu selhání. Neměň nic, co s cílem a "
        "Definition of Done nesouvisí.",
        "",
        "Až skončíš, tvá úplně poslední odpověď musí být výhradně jeden JSON objekt (žádný "
        "markdown blok, žádný text před ani za ním) přesně v tomto tvaru:",
        '{"items": [{"index": 0, "done": true}, {"index": 1, "done": false}], '
        '"notes": "strucne shrnuti pro pristi iteraci"}',
        f'Pole "items" musí obsahovat přesně jeden záznam pro každý z těchto indexů, s upřímným '
        f"vyhodnocením podle reálného stavu souborů a testů - ne podle úmyslu: {requested_indices}.",
    ]
    return "\n".join(lines)


def _build_audit_prompt(
    goal: str,
    dod_items: list[DoDItem],
    project_status: str,
    test_command: Optional[str],
    tests_passed: Optional[bool],
    test_output: Optional[str],
) -> str:
    """Independent verification prompt, only ever sent once the executor
    claims every DoD item is done and the orchestrator's own test run
    agrees. Unlike `_build_iteration_prompt`, this lists the *whole* DoD
    list (there is nothing left to batch - everything is being checked
    exactly once, right before a commit) but explicitly forbids making any
    changes: this is a read-only review pass (Manager/Executor/Auditor
    style), not a second implementation attempt."""
    lines = [
        f"{AUDIT_MARKER} (nezávislá kontrola před dokončením běhu - NEDĚLEJ žádné změny v kódu "
        "ani v souborech, pouze ověřuj).",
        "",
        "Kanonický runtime contract (machine rules):",
        _runtime_contract_text(),
        "",
        f"Cíl projektu: {goal}",
        "",
        "Implementační agent tvrdí, že jsou splněny všechny následující body Definition of Done:",
    ]
    for idx, item in enumerate(dod_items):
        lines.append(f"{idx}. {item.text}")

    controller_gate_indices = _controller_audit_gate_indices(dod_items)
    if controller_gate_indices:
        lines += [
            "",
            "Controller-owned final audit gate:",
            "Indexy " + ", ".join(str(index) for index in sorted(controller_gate_indices))
            + " označují pouze povinnost ai-orchestratoru vydat finální accepted/rejected verdikt; "
            "nejsou to další soubory nebo testy k ověřování.",
            "Po vlastním ověření všech ostatních bodů musíš i pro tento index vrátit "
            "accepted=true, pokud jsou všechny věcné body ověřené; použij konkrétní evidence "
            "z vlastních testů/prohlídky. Pokud je věcný bod neověřen nebo selhal, odmítni tento "
            "gate s odkazem na konkrétní odmítnutý index.",
        ]

    lines += ["", "Aktuální stav projektu (git status --porcelain):", project_status]

    if test_command:
        result_label = "nespuštěny" if tests_passed is None else ("PROŠLY" if tests_passed else "SELHALY")
        lines += ["", f"Výsledek testů ({test_command}) ověřený orchestrátorem (ne agentem): {result_label}"]
        if test_output:
            lines += ["Výstup testů:", tail_text(test_output, 1500)]

    lines += [
        "",
        "Nezávisle over každý bod (přečti relevantní soubory/diff, nespoléhej na poznámky "
        "z předchozích iterací) - NEIMPLEMENTUJ nic nového, nic neměň. Pokud najdeš bod, který "
        "ve skutečnosti splněný není, uveď jeho index.",
        "",
        "Až skončíš, tvá úplně poslední odpověď musí být výhradně jeden JSON objekt (žádný "
        "markdown blok, žádný text před ani za ním) přesně v tomto tvaru:",
        '{"items":[{"index":0,"accepted":false,"evidence":"soubor/test/live vystup"}],'
        '"notes":"strucne zduvodneni"}',
        "Pole items musí obsahovat právě jeden záznam pro KAŽDÝ index. Evidence musí být konkrétní "
        "a dohledatelná (soubor+symbol, test, nebo live výstup); samotné tvrzení implementačního "
        "agenta ani obecné 'testy prošly' není důkaz splnění daného bodu. Pokud důkaz chybí, nastav "
        "accepted=false.",
    ]
    return "\n".join(lines)


@dataclass
class AuditOutcome:
    rejected_indices: list[int]
    notes: str
    protocol_error: bool
    session_id: Optional[str]
    breaker_saved_attempts: int = 0
    # Set when the audit call itself failed because every configured
    # provider is exhausted/unavailable (AgentRunResult.limited), NOT
    # because the agent returned malformed JSON. Kept distinct from
    # `protocol_error` so the caller can correctly transition the whole run
    # to WAITING_FOR_PROVIDER instead of counting this towards
    # PROTOCOL_ERROR_STREAK_LIMIT - see run_autonomous_loop.
    limited: bool = False
    retry_after_seconds: Optional[float] = None
    error: Optional[str] = None
    usage_events: list[dict] = field(default_factory=list)
    # Whether the one allowed cheap audit repair reprompt was attempted this
    # iteration (tracked so the log/outbox can show whether the audit recovered
    # via repair or failed cleanly - mirrors executor's repair_attempted).
    audit_repair_attempted: bool = False


def _validate_audit_response(
    parsed: dict,
    expected_indices: set[int],
    item_count: int,
) -> tuple[bool, list[int], list[str]]:
    """Validate an already-parsed audit JSON against the strict contract.

    Accepts only the structured format
    (``{"items": [{"index":..., "accepted":..., "evidence":...}]}``).
    The former flat format is rejected because it cannot carry evidence for
    every index and would weaken the independent-audit boundary.

    Returns (protocol_error, rejected_indices, evidence_lines). When
    protocol_error is True the response is structurally broken and must not
    be trusted at all; rejected_indices is only meaningful when protocol_error
    is False.
    """
    audit_items = parsed.get("items")

    # Legacy flat format is intentionally no longer accepted.  It has no
    # per-index evidence, so an empty rejection list could falsely accept an
    # entire project without an independent proof for each DoD item.
    if not isinstance(audit_items, list):
        legacy_rejected = parsed.get("rejected_indices")
        if isinstance(legacy_rejected, list) and all(isinstance(i, int) for i in legacy_rejected):
            return True, [], ["legacy audit response lacks per-index evidence"]
        return True, [], []

    seen: set[int] = set()
    rejected: list[int] = []
    evidence_lines: list[str] = []
    for item in audit_items:
        if not isinstance(item, dict):
            return True, [], []
        idx = item.get("index")
        accepted = item.get("accepted")
        evidence = item.get("evidence")
        if (
            not isinstance(idx, int)
            or isinstance(idx, bool)
            or idx not in expected_indices
            or idx in seen
            or not isinstance(accepted, bool)
            or not isinstance(evidence, str)
            or not evidence.strip()
        ):
            return True, [], []
        seen.add(idx)
        if not accepted:
            rejected.append(idx)
        evidence_lines.append(f"{idx}:{'OK' if accepted else 'REJECT'} {evidence.strip()}")

    if seen != expected_indices:
        missing = sorted(expected_indices - seen)
        if missing:
            evidence_lines.append(f"missing={missing}")
        return True, [], evidence_lines

    rejected.sort()
    return False, rejected, evidence_lines


# A valid audit response can still be unusable when the provider explicitly
# says it did not inspect the checkout. Keep this detector narrow: only an
# all-rejected response with multiple refusal/non-verification markers may
# trigger a quality fallback. A concrete item-specific rejection remains the
# independent verdict.
_AUDIT_INADEQUATE_MARKERS = (
    "nelze samostatně potvrdit",
    "nelze samostatne potvrdit",
    "nebylo ověřeno",
    "nebylo overeno",
    "nebylo provedeno",
    "audit nebyl proveden",
    "živé ověření nebylo provedeno",
    "zive overeni nebylo provedeno",
    "neprovedeno",
    "neproveden",
    "neprovedla",
    "nemá přístup",
    "nema pristup",
    "agent nemá",
    "agent nema",
    "agent nemůže",
    "agent nemuze",
    "cannot confirm",
    "cannot verify",
    "cannot independently",
    "not independently",
    "not inspected",
    "not verified",
    "not performed",
    "no live verification",
    # Some providers describe a non-verdict as a review plan rather than an
    # explicit refusal. Treat that wording as inadequate when every item is
    # rejected, so the failover chain can obtain the actual independent audit.
    "needs verification",
    "pending audit verdict",
)


def _audit_needs_quality_fallback(
    rejected_indices: list[int],
    evidence_lines: list[str],
    item_count: int,
) -> bool:
    """Detect a valid-looking audit that is only a generic refusal.

    Legitimate concrete rejection is retained. The fallback is for the case
    where every DoD item was rejected while the evidence says the audit was
    not actually performed.
    """
    if item_count <= 0 or len(rejected_indices) != item_count:
        return False
    evidence = " ".join(evidence_lines).casefold()
    matched = {marker for marker in _AUDIT_INADEQUATE_MARKERS if marker in evidence}
    return len(matched) >= 2


def _run_audit(
    agent: Agent,
    project_path: Path,
    goal: str,
    dod_items: list[DoDItem],
    project_status: str,
    test_command: Optional[str],
    tests_passed: Optional[bool],
    test_output: Optional[str],
    session_id: Optional[str],
    run_id: str,
    iteration: int,
    logger,
    max_iterations: int,
) -> AuditOutcome:
    """One independent, read-only verification call, made only when the
    executor claims completion and the orchestrator's own tests agree (see
    `run_autonomous_loop`). Never re-implements anything - it only ever
    confirms or rejects the executor's claim, so a commit is never gated on
    the executor's self-report alone.

    When the agent's response is not valid audit JSON, it gets exactly one
    cheap repair reprompt (see `_build_audit_repair_prompt`) - never a full
    re-audit iteration. If the repair also fails, the error is recorded as a
    protocol error and excluded from no-progress tracking (same as the
    executor repair path).
    """
    prompt = _build_audit_prompt(goal, dod_items, project_status, test_command, tests_passed, test_output)
    logger.info(
        "Autonomní běh %s: iterace %s - všechny body tvrzeny jako splněné, spouštím nezávislý "
        "audit (%s znaků, ~%s tokenů odhadem, schema %s znaků)",
        run_id, iteration, len(prompt), len(prompt) // 4, len(_audit_response_schema(len(dod_items))),
    )
    audit_schema = _audit_response_schema(len(dod_items))
    result = agent.run(
        AgentRunRequest(
            project_path=project_path,
            prompt=prompt,
            session_id=session_id,
            output_schema=audit_schema,
        )
    )
    new_session_id = result.session_id or session_id
    saved = result.breaker_saved_attempts
    provider = getattr(agent, "active_provider_name", getattr(agent, "name", None))
    audit_usage = _usage_from_result(result, provider, "audit", iteration)
    if not result.success:
        if result.limited:
            # All configured providers are exhausted/unavailable - this is
            # not a protocol violation, it must propagate as
            # WAITING_FOR_PROVIDER (see incident cb501524e47e, 26.8.2026,
            # and the module docstring's PROTOCOL_ERROR_STREAK_LIMIT note).
            return AuditOutcome(
                [], "Audit narazil na vyčerpané/nedostupné providery.", True, new_session_id, saved,
                limited=True, retry_after_seconds=result.retry_after_seconds, error=result.error,
                usage_events=audit_usage,
            )
        detail = result.error or "provider nevrátil bližší důvod"
        logger.warning(
            "Autonomní běh %s: iterace %s - auditní provider selhal: %s",
            run_id, iteration, detail,
        )
        return AuditOutcome(
            [], f"Audit selhal (chyba agenta): {detail}", True, new_session_id,
            saved, error=detail, usage_events=audit_usage,
        )

    parsed = _extract_json(result.output_text)
    if not parsed:
        # One cheap repair reprompt - never a re-audit iteration (same
        # contract as the executor repair path, see module docstring).
        return _audit_with_repair(
            agent, project_path, goal, dod_items, project_status, test_command,
            tests_passed, test_output, session_id, run_id, iteration, logger,
            max_iterations, new_session_id=new_session_id, saved=saved, provider=provider,
            usage=audit_usage, raw_output=result.output_text,
        )

    expected_indices = set(range(len(dod_items)))
    protocol_error, rejected, evidence_lines = _validate_audit_response(
    parsed, expected_indices, len(dod_items),
    )
    if protocol_error:
        # Try one repair before giving up on this audit.
        return _audit_with_repair(
            agent, project_path, goal, dod_items, project_status, test_command,
            tests_passed, test_output, session_id, run_id, iteration, logger,
            max_iterations, new_session_id=new_session_id, saved=saved, provider=provider,
            usage=audit_usage, evidence_lines=evidence_lines, raw_output=result.output_text,
        )
    notes = parsed.get("notes")
    notes = notes if isinstance(notes, str) else ""
    if evidence_lines:
        notes = (notes + " | " + " ; ".join(evidence_lines)).strip(" |")

    # The final accepted/rejected gate is issued by ai-orchestrator itself,
    # not audited as a separate implementation fact. If the provider has
    # supplied concrete acceptance evidence for every substantive item but
    # mechanically rejected only this controller-owned marker, the controller
    # can close its own gate without reopening implementation work.
    controller_gate_indices = _controller_audit_gate_indices(dod_items)
    if controller_gate_indices and set(rejected).issubset(controller_gate_indices):
        notes = (
            f"{notes} | controller-owned gate resolved by ai-orchestrator from "
            "evidence-backed acceptance of all substantive audit items"
        ).strip(" |")
        rejected = [index for index in rejected if index not in controller_gate_indices]

    if _audit_needs_quality_fallback(rejected, evidence_lines, len(dod_items)):
        fallback_reason = (
            "audit vrátil zamítnutí všech bodů bez konkrétního ověření checkoutu"
        )
        force_quality_failover = getattr(agent, "force_failover_on_audit_quality", None)
        if callable(force_quality_failover) and force_quality_failover(fallback_reason):
            logger.warning(
                "Autonomní běh %s: iterace %s - první auditní odpověď je věcně "
                "nedostatečná, opakuji audit u dalšího providera.",
                run_id, iteration,
            )
            fallback_result = agent.run(
                AgentRunRequest(
                    project_path=project_path,
                    prompt=prompt,
                    # Do not carry the first provider's audit session into the
                    # independent fallback call.
                    session_id=None,
                    output_schema=audit_schema,
                )
            )
            fallback_session_id = fallback_result.session_id
            fallback_provider = getattr(
                agent, "active_provider_name", getattr(agent, "name", None)
            )
            audit_usage.extend(
                _usage_from_result(
                    fallback_result, fallback_provider, "audit-fallback", iteration
                )
            )
            if not fallback_result.success:
                detail = fallback_result.error or "záložní auditní provider nevrátil bližší důvod"
                if fallback_result.limited:
                    return AuditOutcome(
                        [],
                        f"První audit byl věcně nedostatečný; záložní audit narazil na limit: {detail}",
                        True,
                        fallback_session_id or new_session_id,
                        saved,
                        limited=True,
                        retry_after_seconds=fallback_result.retry_after_seconds,
                        error=detail,
                        usage_events=audit_usage,
                    )
                return AuditOutcome(
                    [],
                    f"První audit byl věcně nedostatečný; záložní audit selhal: {detail}",
                    True,
                    fallback_session_id or new_session_id,
                    saved,
                    error=detail,
                    usage_events=audit_usage,
                )

            fallback_parsed = _extract_json(fallback_result.output_text)
            if not fallback_parsed:
                return AuditOutcome(
                    [],
                    "První audit byl věcně nedostatečný; záložní audit nevrátil JSON.",
                    True,
                    fallback_session_id or new_session_id,
                    saved,
                    error="záložní audit nevrátil JSON",
                    usage_events=audit_usage,
                )
            fallback_protocol_error, fallback_rejected, fallback_evidence = _validate_audit_response(
                fallback_parsed, expected_indices, len(dod_items),
            )
            if fallback_protocol_error:
                return AuditOutcome(
                    [],
                    "První audit byl věcně nedostatečný; záložní audit porušil auditní JSON kontrakt.",
                    True,
                    fallback_session_id or new_session_id,
                    saved,
                    error="záložní audit porušil JSON kontrakt",
                    usage_events=audit_usage,
                )
            fallback_notes = fallback_parsed.get("notes")
            fallback_notes = fallback_notes if isinstance(fallback_notes, str) else ""
            combined_evidence = [
                f"první auditní provider: {fallback_reason}",
                *fallback_evidence,
            ]
            fallback_notes = (fallback_notes + " | " + " ; ".join(combined_evidence)).strip(" |")
            logger.info(
                "Autonomní běh %s: iterace %s - záložní nezávislý audit dokončen, "
                "zamítnuto %s bod(ů).",
                run_id, iteration, len(fallback_rejected),
            )
            return AuditOutcome(
                fallback_rejected,
                fallback_notes,
                False,
                fallback_session_id or new_session_id,
                saved,
                usage_events=audit_usage,
            )

        # In a forced single-provider run, do not turn a generic refusal into
        # a real rejection that would reopen implementation work. Without an
        # actual independent verdict the run must stop fail-closed.
        logger.warning(
            "Autonomní běh %s: iterace %s - auditní odpověď je věcně nedostatečná "
            "a není nakonfigurován dostupný auditní fallback.",
            run_id, iteration,
        )
        return AuditOutcome(
            [],
            "Auditní odpověď byla formálně platná, ale neobsahovala konkrétní nezávislé "
            "ověření; žádný auditní fallback není dostupný. | " + notes,
            True,
            new_session_id,
            saved,
            error="auditní odpověď bez konkrétního ověření",
            usage_events=audit_usage,
        )
    return AuditOutcome(rejected, notes, False, new_session_id, saved, usage_events=audit_usage)


def _run_controller_audit(
    project_path: Path,
    dod_items: list[DoDItem],
    controller_gate_indices: set[int],
    finalization: dict,
    tests_passed: Optional[bool],
    run_id: str,
    logger,
) -> AuditOutcome:
    """Perform the controller-owned, zero-provider audit for finalization."""
    unverified = [
        index for index, item in enumerate(dod_items)
        if not item.done and index not in controller_gate_indices
    ]
    proof_ok = tests_passed is True and not unverified and controller_finalization_is_current(
        project_path, finalization
    )
    evidence = (
        "Controller-owned deterministic audit: finalization proof, current HEAD, branch, "
        "clean working tree, tests passed, origin and remote HEAD verified."
    )
    if proof_ok:
        logger.info("Autonomní běh %s: controller finalization audit accepted without provider", run_id)
        return AuditOutcome([], evidence, False, None, usage_events=[])
    rejected = sorted(set(unverified) | controller_gate_indices)
    reason = evidence + " Důkaz je neúplný nebo aktuální stav repozitáře nesouhlasí."
    logger.warning("Autonomní běh %s: controller finalization audit rejected %s", run_id, rejected)
    return AuditOutcome(rejected, reason, False, None, usage_events=[])


def _audit_with_repair(
    agent: Agent,
    project_path: Path,
    goal: str,
    dod_items: list[DoDItem],
    project_status: str,
    test_command: Optional[str],
    tests_passed: Optional[bool],
    test_output: Optional[str],
    session_id: Optional[str],
    run_id: str,
    iteration: int,
    logger,
    max_iterations: int,
    *,
    new_session_id: Optional[str],
    saved: int,
    provider: Optional[str],
    usage: list[dict],
    evidence_lines: list[str] = (),
    raw_output: str = "",
) -> AuditOutcome:
    """After a failed/invalid first audit response, attempt exactly one cheap
    repair reprompt (resend the JSON, no re-review). If that also fails the
    error is recorded as a protocol error and excluded from no-progress
    tracking (same as the executor repair path).

    This is deliberately separate from `_run_audit` so the main path stays
    linear and the repair codepath cannot accidentally be made recursive.
    """
    repair_prompt = _build_audit_repair_prompt(dod_items, test_output)
    repair_prompt_chars = len(repair_prompt)
    evidence_note = ((" [" + "; ".join(evidence_lines) + "]") if evidence_lines else "")
    if raw_output:
        evidence_note += " raw=" + tail_text(raw_output, 1500)
    logger.info(
        "Autonomní běh %s: iterace %s - audit odpověď nebyla platná, zkouším jeden levný "
        "repair pokus (%s znaků)",
        run_id, iteration, repair_prompt_chars,
    )
    repair_result = agent.run(
        AgentRunRequest(
            project_path=project_path,
            prompt=repair_prompt,
            session_id=new_session_id,
            output_schema=_audit_response_schema(len(dod_items)),
        )
    )
    repair_usage = _usage_from_result(repair_result, provider, "audit-repair", iteration)
    usage.extend(repair_usage)

    if not repair_result.success:
        if repair_result.limited:
            logger.error(
                "Autonomní běh %s: iterace %s/%s - audit repair narazil na "
                "vyčerpané/nedostupné providery, přecházím do WAITING_FOR_PROVIDER: %s",
                run_id, iteration, len(project_status.split("\n")) or 1, repair_result.error,
            )
            return AuditOutcome(
                [], "Audit repair narazil na vyčerpané/nedostupné providery.", True,
                repair_result.session_id or new_session_id, saved,
                audit_repair_attempted=True,
                limited=True, retry_after_seconds=repair_result.retry_after_seconds,
                error=repair_result.error, usage_events=usage,
            )
        logger.warning(
            "Autonomní běh %s: iterace %s/%s - audit i po repair pokusu stále neplatná JSON",
            run_id, iteration, max_iterations,
        )
        return AuditOutcome(
            [], "Audit nevrátil platný JSON ani po repair pokusu." + evidence_note, True,
            repair_result.session_id or new_session_id, saved,
            audit_repair_attempted=True, usage_events=usage,
        )

    repaired_parsed = _extract_json(repair_result.output_text)
    if not repaired_parsed:
        logger.warning(
            "Autonomní běh %s: iterace %s/%s - audit repair odpověď stále nebyla JSON",
            run_id, iteration, max_iterations,
        )
        return AuditOutcome(
            [], "Audit nevrátil platný JSON ani po repair pokusu." + evidence_note, True,
            repair_result.session_id or new_session_id, saved,
            audit_repair_attempted=True, usage_events=usage,
        )

    expected_indices = set(range(len(dod_items)))
    repair_protocol_error, repaired_rejected, repaired_evidence_lines = _validate_audit_response(
        repaired_parsed, expected_indices, len(dod_items),
    )
    if repair_protocol_error:
        logger.warning(
            "Autonomní běh %s: iterace %s/%s - audit repair odpověď stále nebyla platná",
            run_id, iteration, max_iterations,
        )
        return AuditOutcome(
            [], "Audit nevrátil platný JSON ani po repair pokusu." + evidence_note, True,
            repair_result.session_id or new_session_id, saved,
            audit_repair_attempted=True, usage_events=usage,
        )

    repair_notes = repaired_parsed.get("notes")
    repair_notes = repair_notes if isinstance(repair_notes, str) else ""
    combined = list(evidence_lines) + list(repaired_evidence_lines)
    if combined:
        repair_notes = (repair_notes + " | " + " ; ".join(combined)).strip(" |")
    logger.info(
        "Autonomní běh %s: iterace %s - audit repair pokus úspěšný, přijato %s bod(ů)",
        run_id, iteration, len(repaired_rejected),
    )
    return AuditOutcome(
        repaired_rejected, repair_notes, False,
        repair_result.session_id or new_session_id, saved,
        audit_repair_attempted=True, usage_events=usage,
    )


def _iteration_signature(dod_items: list[DoDItem], tests_passed: Optional[bool], test_output: Optional[str]) -> str:
    unmet = sorted(item.text for item in dod_items if not item.done)
    return "|".join([str(tests_passed), tail_text(test_output or "", 500), "||".join(unmet)])


def _commit_if_ready(
    project_path: Path,
    config: Config,
    auto_commit_requested: bool,
    tests_passed: Optional[bool],
    run_id: str,
    goal: str,
    logger,
    preexisting_dirty: bool = False,
) -> tuple[bool, Optional[str], Optional[str]]:
    """Mirrors runner.py's `_maybe_commit` guard rules for the autonomous run:
    only commit when auto_commit was explicitly requested for this run, tests
    did not fail, the target is a Git repo, and there is something to commit.

    `auto_commit_requested` is already the fully-resolved per-run decision
    (OrchestratorService.run_autonomous() falls back to `config.git.auto_commit`
    only when the caller did not explicitly pass `auto_commit=...`) - it must
    not be ANDed with `config.git.auto_commit` again here, or an explicit
    per-run approval (e.g. via the CLI --commit flag) would silently no-op
    whenever the global config default happened to be False."""
    if not auto_commit_requested:
        return False, None, None
    if preexisting_dirty:
        logger.info(
            "Autonomn? b?h %s: commit se p?eskakuje, proto?e pracovn? strom obsahoval zm?ny u? p?ed startem b?hu.",
            run_id,
        )
        return False, None, None
    if tests_passed is False:
        return False, None, None
    if not is_git_repo(project_path):
        logger.info("Autonomní běh %s: %s není Git repozitář, commit se přeskakuje", run_id, project_path)
        return False, None, None
    if not has_uncommitted_changes(project_path):
        logger.info("Autonomní běh %s: žádné změny v pracovním stromu, není co commitnout", run_id)
        return False, None, None

    summary = goal.strip().splitlines()[0][:72] if goal.strip() else "autonomni beh"
    message = f"{config.git.commit_message_prefix}{summary}\n\nAutonomous run {run_id}"
    try:
        commit_hash = git_commit(project_path, message)
        logger.info("Autonomní běh %s: vytvořen commit %s", run_id, commit_hash)
        return True, commit_hash, None
    except GitError as e:
        logger.warning("Autonomní běh %s: commit se nepodařil: %s", run_id, e)
        return False, None, str(e)


# -- test command auto-detection ---------------------------------------------

_PYTHON_PROJECT_MARKERS = (
    "pyproject.toml", "setup.cfg", "setup.py", "pytest.ini", "tox.ini",
    # A project with a "tests/" dir or a root-level test_*.py file and a plain
    # requirements.txt but no
    # packaging metadata file (this repo itself, ai-orchestrator, is one
    # such project) is still unambiguously a real Python test suite, not a
    # guess - without this marker, _detect_test_command returned None here
    # and every autonomous run against this project's own repo silently
    # never executed its test suite (tests_passed stayed None forever
    # instead of a real pass/fail), which is exactly the "unverified DoD
    # item" AGENTS.md rule 9 exists to prevent.
    "requirements.txt",
    # Small generated Inbox projects may keep their first regression test at
    # the repository root (for example test_inbox_import.py) rather than in a
    # tests/ package. That is still an explicit pytest suite, not a guess.
)


def _detect_test_command(project_path: Path) -> Optional[str]:
    """Best-effort fallback test command, used only when autonomous mode was
    not given one (no --test-command, no project/testing config in
    config.yaml). Deliberately narrow: only ever suggests
    "python -m pytest -q", and only when the project looks like an actual
    Python project with a real test suite (a "tests/" directory alongside a
    recognizable Python project marker file) - never invented for a project
    type this cannot recognize. A plain one-off `run` task is unaffected and
    keeps skipping tests when none is configured; this only applies to the
    autonomous loop, where silently never running tests would let a DoD item
    like "all tests pass" go forever unverified (see AGENTS.md rule 9).
    """
    root_test_suite = any(project_path.glob("test_*.py")) or any(
        project_path.glob("*_test.py")
    )
    # A root-level pytest file is an explicit enough signal on its own for a
    # small generated checkout; larger projects still need both a tests/
    # directory and a recognizable Python project marker.
    if root_test_suite:
        return "python -m pytest -q"
    if not (project_path / "tests").is_dir():
        return None
    if not any((project_path / marker).exists() for marker in _PYTHON_PROJECT_MARKERS):
        return None
    return "python -m pytest -q"


def run_autonomous_loop(
    run_id: str,
    project_path: Path,
    goal: str,
    dod_items: list[DoDItem],
    config: Config,
    agent: Agent,
    logger,
    test_command: Optional[str] = None,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    auto_commit_requested: bool = True,
    on_iteration: Optional[Callable[[AutonomousResult], None]] = None,
    preexisting_dirty: Optional[bool] = None,
    controller_finalization: Optional[dict] = None,
    implementation_only: bool = False,
) -> AutonomousResult:
    max_iterations = max(1, min(max_iterations, ABSOLUTE_MAX_ITERATIONS))
    # preexisting_dirty, when passed by OrchestratorService.run_autonomous(),
    # is a snapshot taken BEFORE it calls ensure_project_claude_settings() -
    # that call writes .claude/settings.local.json into a project that
    # doesn't have one yet, which would itself make the tree look "dirty"
    # here and make every first-ever autonomous run against a project
    # silently lose its commit. Falls back to computing it directly for
    # callers that invoke this loop without going through the service (e.g.
    # tests).
    if preexisting_dirty is None:
        preexisting_dirty = is_git_repo(project_path) and has_uncommitted_changes(project_path)
    if preexisting_dirty:
        logger.warning(
            "Autonomn? b?h %s: projekt byl dirty u? p?ed startem; auto-commit je pro tento b?h zak?z?n, "
            "aby orchestr?tor nep?ibral ciz? rozpracovan? zm?ny.",
            run_id,
        )
    if not test_command:
        detected = _detect_test_command(project_path)
        if detected:
            logger.info(
                "Autonomní běh %s: žádný test_command nenakonfigurován, použiji detekovaný '%s'",
                run_id, detected,
            )
            test_command = detected
    iterations: list[IterationLog] = []
    session_id: Optional[str] = None
    previous_notes = ""
    last_signature: Optional[str] = None
    same_signature_count = 0
    prev_tests_passed: Optional[bool] = None
    prev_test_output: Optional[str] = None
    total_prompt_chars = 0
    breaker_saved_total = 0
    protocol_error_streak = 0
    protocol_error_total = 0
    protocol_error_wasted_chars = 0
    usage_events: list[dict] = []

    def note_breaker_savings(saved: int) -> None:
        """Surface the PreToolUse circuit breaker's short-circuit count
        (orchestrator/hooks/test_command_guard.py) in this run's own log,
        the same way permission_denials is surfaced in runner.py - see
        AgentRunResult.breaker_saved_attempts."""
        nonlocal breaker_saved_total
        if saved:
            breaker_saved_total += saved
            logger.info(
                "Autonomní běh %s: circuit breaker ušetřil %s opakovaných pokusů o spuštění "
                "testů (agent je po prvním zamítnutí nezkoušel opakovat jinou variantou "
                "příkazu).",
                run_id, saved,
            )

    def snapshot(status: AutonomousStatus, **extra) -> AutonomousResult:
        usage_by_provider, usage_total = _usage_summary(usage_events)
        status_snapshot = getattr(agent, "provider_status_snapshot", None)
        provider_statuses = status_snapshot() if callable(status_snapshot) else {}
        return AutonomousResult(
            status=status, iterations=list(iterations), dod_items=dod_items,
            audit_repair_attempted=any(it.audit_repair_attempted for it in iterations),
            breaker_saved_attempts=breaker_saved_total,
            protocol_error_total=protocol_error_total,
            protocol_error_wasted_prompt_chars=protocol_error_wasted_chars,
            usage_events=list(usage_events), usage_by_provider=usage_by_provider,
            usage_total=usage_total,
            provider_statuses=provider_statuses,
            **extra,
        )

    logger.info(
        "Autonomní běh %s: start (projekt=%s, max_iterations=%s, DoD položek=%s, dávka=%s)",
        run_id, project_path, max_iterations, len(dod_items), DOD_BATCH_SIZE,
    )

    controller_gate_indices = _controller_audit_gate_indices(dod_items)
    if controller_gate_indices:
        logger.info(
            "Autonomní běh %s: DoD body %s jsou auditní brána kontroleru; "
            "nebudou blokovat implementační iterace",
            run_id, sorted(controller_gate_indices),
        )

    for i in range(1, max_iterations + 1):
        project_status = _project_status_text(project_path)
        batch_size = _agent_batch_size(agent)
        requested_indices = _select_batch(dod_items, batch_size=batch_size)
        requested_indices = [
            index for index in requested_indices if index not in controller_gate_indices
        ]
        if not requested_indices and prev_tests_passed is False:
            # Executor checkboxes are not verified completion when the
            # orchestrator's own tests fail. Reopen one bounded batch so the
            # next iteration actually calls the implementation agent with
            # the failing test output instead of burning every remaining
            # iteration on identical test-only retries.
            requested_indices = list(range(min(batch_size, len(dod_items))))
            for idx in requested_indices:
                dod_items[idx].done = False
            logger.info(
                "Autonomní běh %s: předchozí testy selhaly při všech DoD bodech tvrzených jako "
                "hotové; znovuotevírám opravnou dávku %s pro implementačního agenta",
                run_id, requested_indices,
            )

        agent_output = ""
        agent_error: Optional[str] = None
        protocol_error = False
        repair_attempted = False
        repair_succeeded = False
        repair_prompt_chars = 0
        iteration_usage: list[dict] = []
        notes = previous_notes

        if requested_indices:
            prompt = _build_iteration_prompt(
                goal, dod_items, requested_indices, i, max_iterations, project_status,
                test_command, prev_tests_passed, prev_test_output, previous_notes,
            )
            prompt_chars = len(prompt)
            total_prompt_chars += prompt_chars
            logger.info(
                "Autonomní běh %s: iterace %s/%s - spouštím agenta na dávce %s bodů "
                "(prompt=%s znaků, ~%s tokenů odhadem, celkem za běh ~%s znaků)",
                run_id, i, max_iterations, len(requested_indices),
                prompt_chars, prompt_chars // 4, total_prompt_chars,
            )
            result = agent.run(
                AgentRunRequest(
                    project_path=project_path,
                    prompt=prompt,
                    session_id=session_id,
                    output_schema=_dod_response_schema(requested_indices),
                )
            )
            provider_name = getattr(agent, "active_provider_name", getattr(agent, "name", None))
            iteration_usage.extend(_usage_from_result(result, provider_name, "executor", i))
            usage_events.extend(iteration_usage)
            session_id = result.session_id or session_id
            note_breaker_savings(result.breaker_saved_attempts)

            if not result.success:
                logger.error("Autonomní běh %s: agent v iteraci %s selhal: %s", run_id, i, result.error)
                iterations.append(
                    IterationLog(
                        index=i, prompt=prompt, agent_output=result.output_text, agent_error=result.error,
                        tests_passed=None, test_output=None,
                        dod_snapshot=[_dod_dict(d) for d in dod_items],
                        requested_indices=requested_indices, prompt_chars=prompt_chars,
                        agent_name=getattr(agent, "active_provider_name", getattr(agent, "name", None)),
                    )
                )
                if result.limited:
                    final = snapshot(AutonomousStatus.WAITING_FOR_PROVIDER, error=result.error)
                    final.retry_after_seconds = result.retry_after_seconds
                else:
                    final = snapshot(AutonomousStatus.ERROR, error=result.error)
                if on_iteration:
                    on_iteration(final)
                return final

            agent_output = result.output_text
            parsed = _extract_json(agent_output)
            notes, protocol_error, missing = _apply_dod_updates(dod_items, requested_indices, parsed)

            if protocol_error:
                # Exactly one cheap repair reprompt - never a fresh
                # implementation iteration (see module docstring / req 3).
                repair_attempted = True
                repair_targets = missing or list(requested_indices)
                repair_prompt = _build_repair_prompt(repair_targets, dod_items)
                repair_prompt_chars = len(repair_prompt)
                logger.info(
                    "Autonomní běh %s: iterace %s/%s - odpověď agenta neodpovídá protokolu, "
                    "zkouším jeden levný repair pokus na indexy %s (%s znaků)",
                    run_id, i, max_iterations, repair_targets, len(repair_prompt),
                )
                repair_result = agent.run(
                    AgentRunRequest(
                        project_path=project_path,
                        prompt=repair_prompt,
                        session_id=session_id,
                        output_schema=_dod_response_schema(repair_targets),
                    )
                )
                repair_usage = _usage_from_result(
                    repair_result, getattr(agent, "active_provider_name", getattr(agent, "name", None)),
                    "repair", i,
                )
                iteration_usage.extend(repair_usage)
                usage_events.extend(repair_usage)
                session_id = repair_result.session_id or session_id
                note_breaker_savings(repair_result.breaker_saved_attempts)
                if not repair_result.success and repair_result.limited:
                    # All configured providers are exhausted/unavailable -
                    # this is not a protocol violation, it must propagate as
                    # WAITING_FOR_PROVIDER, not be swallowed into another
                    # unresolved protocol_error iteration (see incident
                    # cb501524e47e, 26.8.2026: without this check, a
                    # provider limit hit specifically during the repair
                    # reprompt call eventually stopped the run as
                    # PROTOCOL_ERROR instead of WAITING_FOR_PROVIDER).
                    logger.error(
                        "Autonomní běh %s: iterace %s/%s - repair pokus narazil na "
                        "vyčerpané/nedostupné providery, přecházím do WAITING_FOR_PROVIDER "
                        "místo protokolové chyby: %s",
                        run_id, i, max_iterations, repair_result.error,
                    )
                    iterations.append(
                        IterationLog(
                            index=i, prompt=prompt, agent_output=agent_output,
                            agent_error=repair_result.error,
                            tests_passed=None, test_output=None,
                            dod_snapshot=[_dod_dict(d) for d in dod_items],
                            note=notes, protocol_error=True,
                            requested_indices=requested_indices, prompt_chars=prompt_chars,
                            repair_attempted=True, repair_succeeded=False,
                            audit_repair_attempted=True,
                            agent_name=getattr(agent, "active_provider_name", getattr(agent, "name", None)),
                            usage=iteration_usage,
                        )
                    )
                    final = snapshot(AutonomousStatus.WAITING_FOR_PROVIDER, error=repair_result.error)
                    final.retry_after_seconds = repair_result.retry_after_seconds
                    if on_iteration:
                        on_iteration(final)
                    return final
                if repair_result.success:
                    repair_parsed = _extract_json(repair_result.output_text)
                    repair_notes, repair_protocol_error, _ = _apply_dod_updates(
                        dod_items, repair_targets, repair_parsed
                    )
                    if not repair_protocol_error:
                        protocol_error = False
                        repair_succeeded = True
                        notes = repair_notes
                    else:
                        notes = f"{notes} [repair se nezdařil: {repair_notes}]".strip()
        else:
            # Nothing left unmet according to the executor's own claims -
            # skip the (expensive) implementation call entirely and go
            # straight to re-testing + the independent audit below.
            prompt = "(žádné nesplněné body - iterace přeskočila volání implementačního agenta)"
            prompt_chars = len(prompt)
            logger.info(
                "Autonomní běh %s: iterace %s/%s - všechny body už tvrzeny jako splněné, "
                "přeskakuji volání agenta a rovnou ověřuji testy/audit",
                run_id, i, max_iterations,
            )

        # Agent claim and local tests cannot close an integration item.
        _enforce_live_evidence(dod_items)

        if test_command:
            tests_passed, test_output = run_test_command(project_path, test_command, logger)
        else:
            tests_passed, test_output = None, None
        prev_tests_passed, prev_test_output = tests_passed, test_output
        previous_notes = notes

        audit_performed = False
        audit_rejected: list[int] = []
        audit_protocol_error = False
        audit_repair_attempted = False
        controller_gate_rejected = False

        # Controller-owned audit gates remain false until the independent
        # audit itself accepts them. They must not force the executor into
        # repeated implementation iterations.
        implementation_done = all(
            item.done or index in controller_gate_indices
            for index, item in enumerate(dod_items)
        )
        all_done = all(item.done for item in dod_items)
        tests_ok = tests_passed is not False
        completed = False
        commit_error: Optional[str] = None
        committed = False
        commit_hash: Optional[str] = None

        if implementation_done and tests_ok and not protocol_error and implementation_only:
            completed = True
            previous_notes = (
                f"{previous_notes} [implementation-only] "
                "Implementation and orchestrator-run tests are complete; "
                "audit is deferred to the separate following workflow tick."
            ).strip()
        elif implementation_done and tests_ok and not protocol_error:
            audit_performed = True
            if controller_finalization is not None:
                audit = _run_controller_audit(
                    project_path, dod_items, controller_gate_indices, controller_finalization,
                    tests_passed, run_id, logger,
                )
            else:
                audit = _run_audit(
                    agent, project_path, goal, dod_items, project_status, test_command,
                    tests_passed, test_output, session_id, run_id, i, logger, max_iterations,
                )
            session_id = audit.session_id or session_id
            note_breaker_savings(audit.breaker_saved_attempts)
            usage_events.extend(audit.usage_events)
            iteration_usage.extend(audit.usage_events)

            if audit.limited:
                # All configured providers are exhausted/unavailable - this
                # is not a protocol violation, it must propagate as
                # WAITING_FOR_PROVIDER (see incident cb501524e47e,
                # 26.8.2026: without this check, a provider limit hit
                # specifically during the independent audit call eventually
                # stopped the run as PROTOCOL_ERROR instead of
                # WAITING_FOR_PROVIDER).
                logger.error(
                    "Autonomní běh %s: iterace %s/%s - audit narazil na vyčerpané/nedostupné "
                    "providery, přecházím do WAITING_FOR_PROVIDER: %s",
                    run_id, i, max_iterations, audit.error,
                )
                audit_repair_attempted = audit.audit_repair_attempted
                iterations.append(
                    IterationLog(
                        index=i, prompt=prompt, agent_output=agent_output, agent_error=audit.error,
                        tests_passed=tests_passed, test_output=test_output,
                        dod_snapshot=[_dod_dict(d) for d in dod_items],
                        note=previous_notes, protocol_error=protocol_error,
                        requested_indices=requested_indices, prompt_chars=prompt_chars,
                        repair_attempted=repair_attempted, repair_succeeded=repair_succeeded,
                        audit_performed=True, audit_rejected_indices=[],
                        audit_protocol_error=True,
                        audit_repair_attempted=audit.audit_repair_attempted,
                        agent_name=getattr(agent, "active_provider_name", getattr(agent, "name", None)),
                        usage=iteration_usage,
                    )
                )
                final = snapshot(AutonomousStatus.WAITING_FOR_PROVIDER, error=audit.error)
                final.retry_after_seconds = audit.retry_after_seconds
                if on_iteration:
                    on_iteration(final)
                return final

            audit_rejected = audit.rejected_indices
            audit_protocol_error = audit.protocol_error
            audit_repair_attempted = audit.audit_repair_attempted

            if audit_protocol_error:
                previous_notes = f"{previous_notes} [audit] {audit.notes}".strip()
            elif audit_rejected:
                controller_gate_rejected = bool(
                    set(audit_rejected) & controller_gate_indices
                )
                for idx in audit_rejected:
                    dod_items[idx].done = False
                logger.warning(
                    "Autonomní běh %s: iterace %s/%s - auditor odmítl %s bod(y), znovuotevírám: %s",
                    run_id, i, max_iterations, len(audit_rejected), audit_rejected,
                )
                previous_notes = (
                    f"{previous_notes} [audit] Auditor odmítl body {audit_rejected}: {audit.notes}"
                ).strip()
            else:
                # Preserve the per-item acceptance evidence in the durable
                # iteration/outbox log as well. Without this, a successful
                # audit left only rejected_indices=[], making an operator
                # unable to distinguish evidence-backed acceptance from a
                # rubber-stamp response.
                previous_notes = (
                    f"{previous_notes} [audit] Auditor přijal všechny body: {audit.notes}"
                ).strip()
                for idx in controller_gate_indices:
                    dod_items[idx].done = True
                all_done = all(item.done for item in dod_items)
                committed, commit_hash, commit_error = _commit_if_ready(
                    project_path, config, auto_commit_requested, tests_passed, run_id, goal, logger,
                    preexisting_dirty=preexisting_dirty,
                )
                completed = True

        iterations.append(
            IterationLog(
                index=i, prompt=prompt, agent_output=agent_output, agent_error=agent_error,
                tests_passed=tests_passed, test_output=test_output,
                dod_snapshot=[_dod_dict(d) for d in dod_items],
                note=previous_notes, protocol_error=protocol_error,
                requested_indices=requested_indices, prompt_chars=prompt_chars,
                repair_attempted=repair_attempted, repair_succeeded=repair_succeeded,
                audit_performed=audit_performed, audit_rejected_indices=audit_rejected,
                audit_protocol_error=audit_protocol_error,
                audit_repair_attempted=audit_repair_attempted,
                agent_name=getattr(agent, "active_provider_name", getattr(agent, "name", None)),
                usage=iteration_usage,
            )
        )
        if controller_gate_rejected:
            reason = (
                "independent audit rejected the controller-owned accepted/rejected gate; "
                "refusing to repeat implementation iterations"
            )
            logger.warning("Autonomní běh %s: %s", run_id, reason)
            final = snapshot(AutonomousStatus.BLOCKED, error=reason)
            if on_iteration:
                on_iteration(final)
            return final
        logger.info(
            "Autonomní běh %s: iterace %s/%s dokončena (testy prošly=%s, nesplněných bodů=%s, "
            "protokolová chyba=%s, repair pokus=%s, audit proveden=%s)",
            run_id, i, max_iterations, tests_passed,
            sum(1 for d in dod_items if not d.done), protocol_error, repair_attempted, audit_performed,
        )
        if on_iteration:
            on_iteration(snapshot(AutonomousStatus.RUNNING))

        if completed:
            final = snapshot(
                AutonomousStatus.COMPLETED, committed=committed, commit_hash=commit_hash, error=commit_error,
            )
            logger.info("Autonomní běh %s: dokončeno (completed), committed=%s", run_id, committed)
            if on_iteration:
                on_iteration(final)
            return final

        # A protocol error (executor JSON still invalid/incomplete after the
        # one cheap repair, or an unparsable audit response) is excluded from
        # the no-progress signature below because it carries no signal about
        # whether the task itself is stuck - but it must not be free to
        # repeat forever either (see incident run
        # 7fffd21835174d9fb9a29237c897f6d2 in the module docstring: 7
        # consecutive protocol errors burned a whole provider's usage limit
        # for zero verified progress before this counter existed). Tracked
        # separately from same_signature_count, with its own low threshold.
        iteration_had_protocol_error = protocol_error or audit_protocol_error
        if iteration_had_protocol_error:
            protocol_error_streak += 1
            protocol_error_total += 1
            protocol_error_wasted_chars += prompt_chars + repair_prompt_chars
        else:
            protocol_error_streak = 0

        if iteration_had_protocol_error and protocol_error_streak >= PROTOCOL_ERROR_STREAK_LIMIT:
            reason = (
                f"Agent {protocol_error_streak}x za sebou nevrátil platný JSON kontrakt "
                "(i po repair pokusu) - jde o protokolovou nekompatibilitu, ne o chybějící "
                "pokrok v úkolu."
            )
            # A repeated protocol violation is a provider-side
            # incompatibility, exactly like a quota/rate limit - so it gets
            # the same right to fail over to another configured provider
            # (see agents/failover.py's FailoverAgent), not just quota/rate
            # limits. Plain single-agent callers (no failover configured)
            # have no such method and fall straight through to stopping.
            force_failover = getattr(agent, "force_failover_on_protocol_error", None)
            if callable(force_failover) and force_failover(reason):
                logger.warning(
                    "Autonomní běh %s: iterace %s/%s - %s Failover proveden, pokračuji dalším "
                    "providerem.",
                    run_id, i, max_iterations, reason,
                )
                protocol_error_streak = 0
            else:
                logger.error(
                    "Autonomní běh %s: iterace %s/%s - %s Žádný další provider není k dispozici, "
                    "zastavuji běh jako PROTOCOL_ERROR místo dalšího plýtvání iteracemi/limitem.",
                    run_id, i, max_iterations, reason,
                )
                notify(
                    f"[AI Orchestrator] Autonomní běh {run_id} zastaven (PROTOCOL_ERROR): {reason} "
                    f"Odhadovaná promarněná spotřeba: ~{protocol_error_wasted_chars // 4} tokenů "
                    f"přes {protocol_error_total} protokolově chybných iterací."
                )
                final = snapshot(AutonomousStatus.PROTOCOL_ERROR, error=reason)
                if on_iteration:
                    on_iteration(final)
                return final

        # Per-job, provider-specific financial hard cap (see
        # ClaudeCodeAgentConfig/AntigravityAgentConfig/CodexAgentConfig.
        # max_budget_usd and _provider_budget_usd): checked once per
        # iteration against this run's own cumulative reported cost_usd for
        # the currently active provider, never against a single call's
        # cost - a provider that has no reported cost_usd (usage tracking is
        # best-effort, see AutonomousResult.usage_events) never triggers
        # this, it can only ever fire on positively confirmed spend.
        budget_provider_name = getattr(agent, "active_provider_name", getattr(agent, "name", None))
        budget_limit = _provider_budget_usd(config, budget_provider_name)
        if budget_limit is not None:
            usage_by_provider, _ = _usage_summary(usage_events)
            spent = (usage_by_provider.get(budget_provider_name) or {}).get("cost_usd")
            if spent is not None and spent > budget_limit:
                budget_reason = (
                    f"Provider '{budget_provider_name}' překročil nakonfigurovaný finanční "
                    f"limit pro tuto úlohu (utraceno ${spent:.2f} > limit ${budget_limit:.2f})."
                )
                force_failover_budget = getattr(agent, "force_failover_on_budget_exceeded", None)
                if callable(force_failover_budget) and force_failover_budget(budget_reason):
                    logger.warning(
                        "Autonomní běh %s: iterace %s/%s - %s Failover proveden, pokračuji "
                        "dalším providerem.",
                        run_id, i, max_iterations, budget_reason,
                    )
                else:
                    logger.error(
                        "Autonomní běh %s: iterace %s/%s - %s Žádný další provider není k "
                        "dispozici, zastavuji běh jako BUDGET_EXCEEDED.",
                        run_id, i, max_iterations, budget_reason,
                    )
                    notify(
                        f"[AI Orchestrator] Autonomní běh {run_id} zastaven (BUDGET_EXCEEDED): "
                        f"{budget_reason}"
                    )
                    final = snapshot(AutonomousStatus.BUDGET_EXCEEDED, error=budget_reason)
                    if on_iteration:
                        on_iteration(final)
                    return final

        # Only a verified, comparable iteration counts towards no-progress:
        # a protocol error (unparsable/incomplete agent JSON, even after the
        # cheap repair attempt), an unparsable audit response, or a missing
        # test result despite a configured test command carries no signal
        # about whether the project itself is stuck, so it must not move
        # (or reset) the no-progress counter either way - see AGENTS.md
        # rule 9 and _apply_dod_updates' docstring.
        test_result_missing = bool(test_command) and tests_passed is None
        if not protocol_error and not test_result_missing and not audit_protocol_error:
            signature = _iteration_signature(dod_items, tests_passed, test_output)
            if signature == last_signature:
                same_signature_count += 1
            else:
                same_signature_count = 1
                last_signature = signature

            if same_signature_count >= NO_PROGRESS_LIMIT:
                logger.warning(
                    "Autonomní běh %s: stejný ověřený stav se opakuje %s iterace za sebou bez "
                    "pokroku, označuji jako blocked", run_id, same_signature_count,
                )
                final = snapshot(AutonomousStatus.BLOCKED)
                if on_iteration:
                    on_iteration(final)
                return final
        else:
            logger.info(
                "Autonomní běh %s: iterace %s/%s nemá ověřitelný stav (protokolová chyba=%s, "
                "chybí výsledek testů=%s, audit chyba=%s) - nepočítá se do detekce bez pokroku",
                run_id, i, max_iterations, protocol_error, test_result_missing, audit_protocol_error,
            )

    logger.warning(
        "Autonomní běh %s: dosažen limit max_iterations=%s bez splnění DoD (odhadem ~%s tokenů "
        "promptů za celý běh)", run_id, max_iterations, total_prompt_chars // 4,
    )
    final = snapshot(AutonomousStatus.MAX_ITERATIONS)
    if on_iteration:
        on_iteration(final)
    return final

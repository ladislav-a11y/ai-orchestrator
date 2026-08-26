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
    has_uncommitted_changes,
    is_git_repo,
    status_porcelain,
)
from orchestrator.runner import run_test_command, tail_text

DEFAULT_MAX_ITERATIONS = 10
# Sanity ceiling - independent of what a caller/CLI flag requests, so a typo
# like "--max-iterations 9999" can never turn this into an unbounded loop.
ABSOLUTE_MAX_ITERATIONS = 50
# How many consecutive iterations with an unchanged (unmet DoD items, test
# result) signature before the run is declared "blocked" (no progress).
NO_PROGRESS_LIMIT = 3
# Max number of currently-unmet DoD items presented to the agent in a single
# iteration. Keeps the prompt (and the required JSON response) small and
# reliable regardless of how large the overall Definition of Done is - a
# real run against a 66-item spec is what originally motivated this (see
# module docstring): asking for a 66-entry JSON response every iteration is
# both expensive and fragile.
DOD_BATCH_SIZE = 8
# Marker line that opens every independent audit prompt, so a caller can
# recognize (and tests can simulate) the audit role distinctly from the
# executor role, even though both go through the same `Agent`.
AUDIT_MARKER = "AUDITORSKÁ KONTROLA"


class AutonomousStatus(str, Enum):
    RUNNING = "running"
    WAITING_FOR_PROVIDER = "waiting_for_provider"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    MAX_ITERATIONS = "max_iterations"
    ERROR = "error"


@dataclass
class DoDItem:
    text: str
    done: bool = False


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
    agent_name: Optional[str] = None

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


# -- Definition of Done parsing ---------------------------------------------

_CHECKBOX_RE = re.compile(r"^[-*]\s*\[([ xX])\]\s+(.+)$")


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
            items.append(DoDItem(text=m.group(2).strip(), done=m.group(1).lower() == "x"))
    if not items:
        stripped = spec_text.strip()
        if stripped:
            items.append(DoDItem(text=stripped))
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
        f"Cíl projektu: {goal}",
        "",
        f"Definition of Done: {done_count}/{total} bodů celkem už ověřeno jako splněno "
        "(nesplněné body se hlásí kumulativně, jednou splněný bod se sem už nevrací). "
        f"Níže je dávka {len(requested_indices)} aktuálně NESPLNĚNÝCH bodů, na kterou se máš "
        "zaměřit v této iteraci:",
    ]
    for idx in requested_indices:
        lines.append(f"{idx}. [NESPLNĚNO] {dod_items[idx].text}")

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
        f"Cíl projektu: {goal}",
        "",
        "Implementační agent tvrdí, že jsou splněny všechny následující body Definition of Done:",
    ]
    for idx, item in enumerate(dod_items):
        lines.append(f"{idx}. {item.text}")

    lines += ["", "Aktuální stav projektu (git status --porcelain):", project_status]

    if test_command:
        result_label = "nespuštěny" if tests_passed is None else ("PROŠLY" if tests_passed else "SELHALY")
        lines += ["", f"Výsledek testů ({test_command}) ověřený orchestrátorem (ne agentem): {result_label}"]
        if tests_passed is False and test_output:
            lines += ["Výstup testů:", tail_text(test_output, 1500)]

    lines += [
        "",
        "Nezávisle over každý bod (přečti relevantní soubory/diff, nespoléhej na poznámky "
        "z předchozích iterací) - NEIMPLEMENTUJ nic nového, nic neměň. Pokud najdeš bod, který "
        "ve skutečnosti splněný není, uveď jeho index.",
        "",
        "Až skončíš, tvá úplně poslední odpověď musí být výhradně jeden JSON objekt (žádný "
        "markdown blok, žádný text před ani za ním) přesně v tomto tvaru:",
        '{"rejected_indices": [], "notes": "strucne zduvodneni"}',
        "Pokud jsou všechny body skutečně splněné, pošli prázdný seznam rejected_indices.",
    ]
    return "\n".join(lines)


@dataclass
class AuditOutcome:
    rejected_indices: list[int]
    notes: str
    protocol_error: bool
    session_id: Optional[str]
    breaker_saved_attempts: int = 0


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
) -> AuditOutcome:
    """One independent, read-only verification call, made only when the
    executor claims completion and the orchestrator's own tests agree (see
    `run_autonomous_loop`). Never re-implements anything - it only ever
    confirms or rejects the executor's claim, so a commit is never gated on
    the executor's self-report alone."""
    prompt = _build_audit_prompt(goal, dod_items, project_status, test_command, tests_passed, test_output)
    logger.info(
        "Autonomní běh %s: iterace %s - všechny body tvrzeny jako splněné, spouštím nezávislý "
        "audit (%s znaků, ~%s tokenů odhadem)",
        run_id, iteration, len(prompt), len(prompt) // 4,
    )
    result = agent.run(AgentRunRequest(project_path=project_path, prompt=prompt, session_id=session_id))
    new_session_id = result.session_id or session_id
    saved = result.breaker_saved_attempts
    if not result.success:
        return AuditOutcome([], "Audit selhal (chyba agenta), zkusim priste znovu.", True, new_session_id, saved)

    parsed = _extract_json(result.output_text)
    if not parsed or not isinstance(parsed.get("rejected_indices"), list):
        return AuditOutcome([], "Audit nevrátil platný JSON, zkusim priste znovu.", True, new_session_id, saved)

    rejected = sorted(
        {idx for idx in parsed["rejected_indices"] if isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx < len(dod_items)}
    )
    notes = parsed.get("notes")
    notes = notes if isinstance(notes, str) else ""
    return AuditOutcome(rejected, notes, False, new_session_id, saved)


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

_PYTHON_PROJECT_MARKERS = ("pyproject.toml", "setup.cfg", "setup.py", "pytest.ini", "tox.ini")


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
        return AutonomousResult(
            status=status, iterations=list(iterations), dod_items=dod_items,
            breaker_saved_attempts=breaker_saved_total, **extra,
        )

    logger.info(
        "Autonomní běh %s: start (projekt=%s, max_iterations=%s, DoD položek=%s, dávka=%s)",
        run_id, project_path, max_iterations, len(dod_items), DOD_BATCH_SIZE,
    )

    for i in range(1, max_iterations + 1):
        project_status = _project_status_text(project_path)
        requested_indices = _select_batch(dod_items)

        agent_output = ""
        agent_error: Optional[str] = None
        protocol_error = False
        repair_attempted = False
        repair_succeeded = False
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
                AgentRunRequest(project_path=project_path, prompt=prompt, session_id=session_id)
            )
            session_id = result.session_id or session_id
            note_breaker_savings(result.breaker_saved_attempts)

            if not result.success:
                logger.error("Autonomní běh %s: agent v iteraci %s selhal: %s", run_id, i, result.error)
                iterations.append(
                    IterationLog(
                        index=i, prompt=prompt, agent_output=result.output_text, agent_error=result.error,
                        tests_passed=None, test_output=None,
                        dod_snapshot=[{"text": d.text, "done": d.done} for d in dod_items],
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
                logger.info(
                    "Autonomní běh %s: iterace %s/%s - odpověď agenta neodpovídá protokolu, "
                    "zkouším jeden levný repair pokus na indexy %s (%s znaků)",
                    run_id, i, max_iterations, repair_targets, len(repair_prompt),
                )
                repair_result = agent.run(
                    AgentRunRequest(project_path=project_path, prompt=repair_prompt, session_id=session_id)
                )
                session_id = repair_result.session_id or session_id
                note_breaker_savings(repair_result.breaker_saved_attempts)
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

        if test_command:
            tests_passed, test_output = run_test_command(project_path, test_command, logger)
        else:
            tests_passed, test_output = None, None
        prev_tests_passed, prev_test_output = tests_passed, test_output
        previous_notes = notes

        audit_performed = False
        audit_rejected: list[int] = []
        audit_protocol_error = False

        all_done = all(item.done for item in dod_items)
        tests_ok = tests_passed is not False
        completed = False
        commit_error: Optional[str] = None
        committed = False
        commit_hash: Optional[str] = None

        if all_done and tests_ok and not protocol_error:
            audit_performed = True
            audit = _run_audit(
                agent, project_path, goal, dod_items, project_status, test_command,
                tests_passed, test_output, session_id, run_id, i, logger,
            )
            session_id = audit.session_id or session_id
            note_breaker_savings(audit.breaker_saved_attempts)
            audit_rejected = audit.rejected_indices
            audit_protocol_error = audit.protocol_error

            if audit_protocol_error:
                previous_notes = f"{previous_notes} [audit] {audit.notes}".strip()
            elif audit_rejected:
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
                committed, commit_hash, commit_error = _commit_if_ready(
                    project_path, config, auto_commit_requested, tests_passed, run_id, goal, logger,
                    preexisting_dirty=preexisting_dirty,
                )
                completed = True

        iterations.append(
            IterationLog(
                index=i, prompt=prompt, agent_output=agent_output, agent_error=agent_error,
                tests_passed=tests_passed, test_output=test_output,
                dod_snapshot=[{"text": d.text, "done": d.done} for d in dod_items],
                note=previous_notes, protocol_error=protocol_error,
                requested_indices=requested_indices, prompt_chars=prompt_chars,
                repair_attempted=repair_attempted, repair_succeeded=repair_succeeded,
                audit_performed=audit_performed, audit_rejected_indices=audit_rejected,
                audit_protocol_error=audit_protocol_error,
                agent_name=getattr(agent, "active_provider_name", getattr(agent, "name", None)),
            )
        )
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

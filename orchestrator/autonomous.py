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
    agent's JSON was unparsable/incomplete, or where a configured test
    command was somehow not actually run, is recorded as a protocol error
    and excluded from the no-progress comparison instead of being treated as
    "no progress" (see `_apply_dod_updates` and the `protocol_error`
    handling in `run_autonomous_loop`).
  - a commit is only ever attempted when every Definition of Done item is
    marked done AND the test command (if any) passed on that same iteration.
  - Definition-of-Done items are only ever parsed from explicit checklist
    lines (`- [ ] ...` / `- [x] ...`); once an item is verified done by the
    orchestrator's own merge, a later agent claim can never un-mark it (see
    `parse_definition_of_done` and `_apply_dod_updates`).
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


class AutonomousStatus(str, Enum):
    RUNNING = "running"
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


def _extract_json(text: str) -> Optional[dict]:
    text = (text or "").strip()
    if not text:
        return None
    fence = _FENCE_RE.search(text)
    candidate = fence.group(1).strip() if fence else text
    try:
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _apply_dod_updates(dod_items: list[DoDItem], parsed: Optional[dict]) -> tuple[str, bool]:
    """Merge the agent's self-reported JSON into dod_items in place.

    The orchestrator has no independent way to check arbitrary natural-
    language DoD items itself, so it cannot fully "verify" the agent's
    claim - but it does not have to trust it blindly either: the merge is
    monotonic (`done = done or claimed_done`), so a DoD item already
    verified done in an earlier iteration can never be silently flipped
    back to not-done by a later, possibly sloppier agent response. Actual
    completion is still independently gated on the orchestrator's own test
    run in `run_autonomous_loop`, not on the agent's claim.

    Returns (notes, protocol_error). protocol_error is True whenever the
    response is missing, not JSON, has no "items" list, has a different
    number of entries than there are DoD items, or contains any entry with
    a malformed/out-of-range index - i.e. whenever the contract described in
    `_build_iteration_prompt` was not honestly followed. Callers must not
    fold a protocol_error iteration into no-progress tracking (see
    AGENTS.md rule 9 / ARCHITECTURE.md): an agent that garbles its output
    format is not the same as an agent that is stuck.
    """
    if not parsed or not isinstance(parsed.get("items"), list):
        return "Agent nevrátil platný JSON stav Definition of Done, ponechávám předchozí stav.", True

    entries = parsed["items"]
    protocol_error = len(entries) != len(dod_items)
    for entry in entries:
        if not isinstance(entry, dict):
            protocol_error = True
            continue
        idx = entry.get("index")
        if not (isinstance(idx, int) and 0 <= idx < len(dod_items)):
            protocol_error = True
            continue
        dod_items[idx].done = dod_items[idx].done or bool(entry.get("done"))

    notes = parsed.get("notes")
    notes = notes if isinstance(notes, str) else ""
    if protocol_error:
        hint = (
            f"[protokol] JSON odpověď neodpovídala zadanému formátu (počet položek musí být "
            f"přesně {len(dod_items)}, každá s platným celočíselným indexem 0..{len(dod_items) - 1}) "
            "- zkus to příští iteraci přesně podle zadaného tvaru."
        )
        notes = f"{notes} {hint}".strip()
    return notes, protocol_error


def _project_status_text(project_path: Path) -> str:
    if not is_git_repo(project_path):
        return "(projekt není Git repozitář, stav nelze zobrazit)"
    status = status_porcelain(project_path)
    return status.strip() or "(žádné neuložené změny)"


def _build_iteration_prompt(
    goal: str,
    dod_items: list[DoDItem],
    iteration: int,
    max_iterations: int,
    project_status: str,
    test_command: Optional[str],
    tests_passed: Optional[bool],
    test_output: Optional[str],
    previous_notes: str,
) -> str:
    lines = [
        f"Autonomní vývojová iterace {iteration}/{max_iterations}.",
        "",
        f"Cíl projektu: {goal}",
        "",
        "Definition of Done (podle poslední znalosti - over si to sám, agent se může mýlit):",
    ]
    for idx, item in enumerate(dod_items):
        mark = "SPLNĚNO" if item.done else "NESPLNĚNO"
        lines.append(f"{idx}. [{mark}] {item.text}")

    lines += ["", "Aktuální stav projektu (git status --porcelain):", project_status]

    if test_command:
        if tests_passed is None:
            lines += ["", f"Testovací příkaz: {test_command} (v této iteraci zatím nespuštěn)"]
        else:
            lines += [
                "",
                f"Poslední výsledek testů ({test_command}): {'PROŠLY' if tests_passed else 'SELHALY'}",
            ]
            if not tests_passed and test_output:
                lines += ["Výstup testů (může být zkrácený):", tail_text(test_output, 2000)]

    if previous_notes:
        lines += ["", "Poznámka z předchozí iterace:", previous_notes]

    lines += [
        "",
        "Uprav projekt tak, aby splnil co nejvíce NESPLNĚNÝCH bodů Definition of Done výše. "
        "Pokud testy z minulé iterace selhaly, nejdřív oprav příčinu selhání. Neměň nic, co "
        "s cílem a Definition of Done nesouvisí.",
        "",
        "Až skončíš, tvá úplně poslední odpověď musí být výhradně jeden JSON objekt (žádný "
        "markdown blok, žádný text před ani za ním) přesně v tomto tvaru:",
        '{"items": [{"index": 0, "done": true}, {"index": 1, "done": false}], '
        '"notes": "strucne shrnuti pro pristi iteraci"}',
        f"Pole items musí mít přesně {len(dod_items)} prvků, jeden pro každý bod výše (indexy "
        f"0..{len(dod_items) - 1}), s upřímným vyhodnocením podle reálného stavu souborů a "
        "testů - ne podle úmyslu.",
    ]
    return "\n".join(lines)


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
) -> tuple[bool, Optional[str], Optional[str]]:
    """Mirrors runner.py's `maybe_commit` guard rules for the autonomous run:
    only commit when auto_commit is requested AND enabled in config, tests did
    not fail, the target is a Git repo, and there is something to commit."""
    if not (auto_commit_requested and config.git.auto_commit):
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
) -> AutonomousResult:
    max_iterations = max(1, min(max_iterations, ABSOLUTE_MAX_ITERATIONS))
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

    def snapshot(status: AutonomousStatus, **extra) -> AutonomousResult:
        return AutonomousResult(status=status, iterations=list(iterations), dod_items=dod_items, **extra)

    logger.info(
        "Autonomní běh %s: start (projekt=%s, max_iterations=%s, DoD položek=%s)",
        run_id, project_path, max_iterations, len(dod_items),
    )

    for i in range(1, max_iterations + 1):
        tests_passed: Optional[bool] = None
        test_output: Optional[str] = None
        project_status = _project_status_text(project_path)

        prompt = _build_iteration_prompt(
            goal, dod_items, i, max_iterations, project_status,
            test_command, tests_passed, test_output, previous_notes,
        )
        logger.info("Autonomní běh %s: iterace %s/%s - spouštím agenta", run_id, i, max_iterations)
        result = agent.run(
            AgentRunRequest(project_path=project_path, prompt=prompt, session_id=session_id)
        )
        session_id = result.session_id or session_id

        if not result.success:
            logger.error("Autonomní běh %s: agent v iteraci %s selhal: %s", run_id, i, result.error)
            iterations.append(
                IterationLog(
                    index=i, prompt=prompt, agent_output=result.output_text, agent_error=result.error,
                    tests_passed=None, test_output=None,
                    dod_snapshot=[{"text": d.text, "done": d.done} for d in dod_items],
                )
            )
            final = snapshot(AutonomousStatus.ERROR, error=result.error)
            if on_iteration:
                on_iteration(final)
            return final

        if test_command:
            tests_passed, test_output = run_test_command(project_path, test_command, logger)

        parsed = _extract_json(result.output_text)
        notes, protocol_error = _apply_dod_updates(dod_items, parsed)
        previous_notes = notes

        iterations.append(
            IterationLog(
                index=i, prompt=prompt, agent_output=result.output_text, agent_error=None,
                tests_passed=tests_passed, test_output=test_output,
                dod_snapshot=[{"text": d.text, "done": d.done} for d in dod_items],
                note=notes, protocol_error=protocol_error,
            )
        )
        logger.info(
            "Autonomní běh %s: iterace %s/%s dokončena (testy prošly=%s, nesplněných bodů=%s, "
            "protokolová chyba=%s)",
            run_id, i, max_iterations, tests_passed,
            sum(1 for d in dod_items if not d.done), protocol_error,
        )
        if on_iteration:
            on_iteration(snapshot(AutonomousStatus.RUNNING))

        all_done = all(item.done for item in dod_items)
        tests_ok = tests_passed is not False
        if all_done and tests_ok:
            committed, commit_hash, commit_error = _commit_if_ready(
                project_path, config, auto_commit_requested, tests_passed, run_id, goal, logger,
            )
            final = snapshot(
                AutonomousStatus.COMPLETED, committed=committed, commit_hash=commit_hash, error=commit_error,
            )
            logger.info("Autonomní běh %s: dokončeno (completed), committed=%s", run_id, committed)
            if on_iteration:
                on_iteration(final)
            return final

        # Only a verified, comparable iteration counts towards no-progress:
        # a protocol error (unparsable/incomplete agent JSON) or a missing
        # test result despite a configured test command carries no signal
        # about whether the project itself is stuck, so it must not move
        # (or reset) the no-progress counter either way - see AGENTS.md
        # rule 9 and _apply_dod_updates' docstring.
        test_result_missing = bool(test_command) and tests_passed is None
        if not protocol_error and not test_result_missing:
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
                "chybí výsledek testů=%s) - nepočítá se do detekce bez pokroku",
                run_id, i, max_iterations, protocol_error, test_result_missing,
            )

    logger.warning("Autonomní běh %s: dosažen limit max_iterations=%s bez splnění DoD", run_id, max_iterations)
    final = snapshot(AutonomousStatus.MAX_ITERATIONS)
    if on_iteration:
        on_iteration(final)
    return final

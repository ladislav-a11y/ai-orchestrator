"""PreToolUse hook: circuit breaker for repeated test-invocation attempts.

Registered via `orchestrator/claude_settings.py` into every project's
`.claude/settings.local.json` (`hooks.PreToolUse`, matcher `"Bash"`). Claude
Code invokes this script once per Bash tool call, before the call runs,
piping a JSON payload (`tool_name`, `tool_input.command`, `session_id`,
`cwd`, ...) on stdin.

Why this exists: real runs showed Claude retrying the same or an equivalent
test-invocation command (pytest / `python -m pytest` / `python -m unittest`
/ a `cmd`-wrapped variant of any of those - the wrapping is what usually
defeats the static `Bash(python:*)`/`Bash(pytest:*)` prefix-match allow
rules in claude_settings.py, since Claude Code's Bash tool on Windows often
executes through `cmd.exe /c "..."`) up to 10-20 times in a single session,
each one burning a permission round-trip and tokens for nothing: the
orchestrator always runs `test_command` itself after the agent finishes
(see runner.py `run_task` / autonomous.py `run_autonomous_loop`) and never
trusts the agent's own test run either way, so the agent running tests buys
nothing.

Design: this hook - not the static allow/deny engine in claude_settings.py -
is the sole decision maker for any Bash command that looks like a test
invocation, and it always denies (see MAX_ATTEMPTS_PER_CATEGORY): the
*first* such command gets a real, explained denial (that is the one
permission-denial attempt the category is allowed). Every further
equivalent attempt in the same Claude Code session is short-circuited
immediately with a terser message, without re-consulting the permission
engine at all, and counted into `saved_attempts` (see `evaluate`). That
count is picked back up by `ClaudeCodeAgent.run()`
(orchestrator/agents/claude_code.py, via `read_saved_attempts`) after the
CLI process exits and logged by runner.py/autonomous.py, so it is visible
in the task's own log, not just buried in this state file.

Fails open by design: any error reading stdin/state (bad JSON, unreadable
state file, ...) falls through to `return 0` (allow) rather than blocking a
tool call over our own bug - the existing static allow/deny rules in
claude_settings.py remain the safety net either way, and normal Read/Edit/
Write/Glob/Grep/git-status/git-diff calls never reach this module at all
(only `tool_name == "Bash"` is inspected, and only Bash commands that match
a test-invocation pattern are ever blocked).

Contract with the Claude Code CLI (stable since hooks were introduced):
exit code 0 = allow, exit code 2 = block and feed stderr back to the agent
as the reason. Any other exit code is a non-blocking hook error (the tool
proceeds as if the hook had not run) - deliberately not relied upon here.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

STATE_DIR_NAME = ".test_guard_state"
TEST_RUN_CATEGORY = "test-run"

# How many times a command class may be denied before every further
# equivalent attempt is short-circuited without re-evaluating anything.
# Deliberately 1, not configurable elsewhere - see module docstring.
MAX_ATTEMPTS_PER_CATEGORY = 1

# Matched against the raw command string with a plain `.search()` (not a
# prefix match), so a `cmd /c "..."`/`powershell -Command "..."` wrapper
# around an equivalent command is still caught - that wrapping is exactly
# what lets these calls slip past claude_settings.py's prefix-based
# `Bash(python:*)`/`Bash(pytest:*)` allow rules in practice on Windows.
TEST_COMMAND_MARKERS: tuple[re.Pattern, ...] = (
    # `(?!\.\w)` avoids false positives like `cat pytest.ini` or
    # `git diff pytest.ini`, where "pytest" is part of a filename, not an
    # invocation.
    re.compile(r"\bpytest\b(?!\.\w)", re.IGNORECASE),
    re.compile(r"\bpy\.test\b", re.IGNORECASE),
    re.compile(r"-m\s+pytest\b", re.IGNORECASE),
    re.compile(r"-m\s+unittest\b", re.IGNORECASE),
    re.compile(r"\bunittest\b(?!\.\w)\s+discover\b", re.IGNORECASE),
    re.compile(r"\bnosetests\b", re.IGNORECASE),
)

FIRST_DENIAL_REASON = (
    "Spouštění testů provádí výhradně orchestrátor, a to až po dokončení úkolu - nikdy sám "
    "agent. Tento pokus o spuštění testovacího příkazu se proto zamítá. Pokračuj v editaci kódu "
    "podle zadání a na úplný závěr jen uveď, že ověření testů necháváš orchestrátorovi. Další "
    "ekvivalentní pokusy (jiná varianta pytest/python/unittest/cmd) budou od teď automaticky "
    "blokovány bez dalšího zdůvodnění."
)
REPEATED_DENIAL_REASON = (
    "Tento typ příkazu (spuštění testů) byl v tomto úkolu už jednou zamítnut - opakované pokusy "
    "se dál neopakují. Pokračuj v editaci kódu; testy ověří orchestrátor sám."
)


def classify_test_command(command: str) -> Optional[str]:
    """Return TEST_RUN_CATEGORY if `command` looks like a test-suite
    invocation in any of its equivalent forms, else None so unrelated Bash
    calls pass through untouched."""
    if not command:
        return None
    for pattern in TEST_COMMAND_MARKERS:
        if pattern.search(command):
            return TEST_RUN_CATEGORY
    return None


def _state_path(project_path: Path, session_id: str) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", session_id or "unknown-session")
    return Path(project_path) / ".claude" / STATE_DIR_NAME / f"{safe_id}.json"


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"categories": {}, "saved_attempts": 0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {"categories": {}, "saved_attempts": 0}
    if not isinstance(data, dict):
        return {"categories": {}, "saved_attempts": 0}
    data.setdefault("categories", {})
    data.setdefault("saved_attempts", 0)
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def evaluate(state: dict[str, Any], category: str) -> tuple[dict[str, Any], bool, str]:
    """Decide whether to block this attempt, mutating and returning `state`.

    The very first attempt for `category` in this session is a genuine,
    explained denial (MAX_ATTEMPTS_PER_CATEGORY == 1) - every attempt after
    that is short-circuited immediately and counted into
    `state["saved_attempts"]`. Always returns should_block=True: this is
    only ever consulted for commands `classify_test_command` already
    identified as a test invocation, and running tests is never the agent's
    job (see module docstring) - there is no case where letting one through
    is correct.
    """
    counts = state["categories"]
    seen = counts.get(category, 0)
    counts[category] = seen + 1
    if seen < MAX_ATTEMPTS_PER_CATEGORY:
        return state, True, FIRST_DENIAL_REASON
    state["saved_attempts"] = state.get("saved_attempts", 0) + 1
    return state, True, REPEATED_DENIAL_REASON


def read_saved_attempts(project_path: Path, session_id: Optional[str]) -> int:
    """Read back how many repeated test-invocation attempts this hook
    short-circuited for `session_id`, for the orchestrator's own task log
    (see ClaudeCodeAgent.run()). Returns 0 if no state file exists yet (the
    common case: nothing to report until a second attempt in the same
    category actually happens) or on any read error - never raises."""
    if not session_id:
        return 0
    try:
        path = _state_path(project_path, session_id)
        if not path.exists():
            return 0
        return int(load_state(path).get("saved_attempts", 0))
    except OSError:
        return 0


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return 0

    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    category = classify_test_command(command or "")
    if category is None:
        return 0

    cwd = payload.get("cwd") or "."
    session_id = payload.get("session_id") or "unknown-session"

    try:
        path = _state_path(Path(cwd), session_id)
        state = load_state(path)
        state, should_block, reason = evaluate(state, category)
        save_state(path, state)
    except OSError:
        return 0  # fail open - never block a tool call over a state-file I/O error

    if should_block:
        sys.stderr.write(reason)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

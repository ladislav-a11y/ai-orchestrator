"""Safe default Claude Code project permissions ("varianta 2").

`ClaudeCodeAgent` runs non-interactively (`--permission-mode acceptEdits`,
see `orchestrator/agents/claude_code.py`), so nobody is around to answer a
permission prompt. This module writes an explicit allow/deny rule set into
every project's own `.claude/settings.local.json`, so Claude Code's permission
engine itself enforces the same boundaries the orchestrator already relies on
- a second, independent layer next to the FORBIDDEN_* guards in
`orchestrator/config.py` / `orchestrator/agents/claude_code.py` and the
`workspace_root` check (see AGENTS.md).

Deliberately narrow: allow exactly what a normal edit-test task needs
(read/create/edit project files, run local Python/pytest/unittest, use the
dedicated bounded Windows runtime launcher for live GUI audits, and the
read-only/staging half of Git), and explicitly deny the Git operations that
lose work (push, hard reset, clean, deleting .git, history rewrites - see
AGENTS.md rule 2) *and* `git commit` itself - committing is the
orchestrator's own job (runner.py/_maybe_commit, autonomous.py/
_commit_if_ready), done only after tests are verified; the agent may
inspect the tree (`git status`, `git diff`, `git add`) but must never create
the commit itself (see AGENTS.md, and NO_COMMIT_INSTRUCTION in
orchestrator/agents/claude_code.py for the matching prompt-level rule).
Anything not listed simply is not auto-approved, which is the safe default
when nobody can click "yes". No blanket command or shell allow is granted
here on purpose.

Also wires up one `PreToolUse` hook (`orchestrator/hooks/test_command_guard.py`)
that stops the agent from repeatedly retrying test-invocation commands after
a permission denial - see that module's docstring for why.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SETTINGS_FILENAME = "settings.local.json"

# Absolute path so the hook still resolves correctly once Claude Code spawns
# it with cwd = the *project's* directory, not this repo.
_TEST_GUARD_HOOK_PATH = Path(__file__).resolve().parent / "hooks" / "test_command_guard.py"

# Read/create/edit project files, run local Python, use the dedicated bounded
# Windows runtime launcher for an independent live audit, and run the
# non-destructive half of Git. acceptEdits already auto-approves file edits;
# listing them here too keeps behavior unchanged if permission_mode is ever
# tightened.
_RUNTIME_GUI_PROBE_PATH = Path(__file__).resolve().parent / "runtime_gui_probe.ps1"
RUNTIME_GUI_RULES: list[str] = [
    f"Bash(powershell -NoProfile -File {_RUNTIME_GUI_PROBE_PATH}:*)",
    f"Bash(pwsh -NoProfile -File {_RUNTIME_GUI_PROBE_PATH}:*)",
]
ALLOWED_RULES: list[str] = [
    "Read",
    "Edit",
    "Write",
    "Glob",
    "Grep",
    # Local Python / virtualenv / test runners. Note: this necessarily also
    # allows "python -c ...", i.e. arbitrary Python code - that is the
    # explicit trade-off of allowing local Python execution at all.
    "Bash(python:*)",
    "Bash(python3:*)",
    "Bash(.venv/bin/python:*)",
    "Bash(.venv/Scripts/python.exe:*)",
    "Bash(.venv\\Scripts\\python.exe:*)",
    "Bash(pytest:*)",
    *RUNTIME_GUI_RULES,
    # Non-destructive Git - deliberately excludes `git commit`: see
    # DENIED_RULES below and the module docstring.
    "Bash(git init:*)",
    "Bash(git status:*)",
    "Bash(git diff:*)",
    "Bash(git add:*)",
    "Bash(git log:*)",
]

# Destructive/irreversible Git operations - denied even if a future edit
# widens ALLOWED_RULES above or a project's own .claude/settings.json tries
# to grant broader Bash access. Mirrors AGENTS.md rule 2 ("never rewrite or
# delete Git history", "never push").
DENIED_RULES: list[str] = [
    "Bash(git push:*)",  # covers --force / --force-with-lease / --delete too
    "Bash(git reset --hard:*)",
    "Bash(git clean -fd:*)",
    "Bash(git clean -fdx:*)",
    "Bash(git rebase:*)",
    "Bash(git filter-branch:*)",
    "Bash(git filter-repo:*)",
    # Committing is exclusively the orchestrator's job, never the agent's -
    # see module docstring and AGENTS.md. This also covers `--amend`, kept
    # listed explicitly too since it doubles as a history-rewrite guard
    # (AGENTS.md rule 2).
    "Bash(git commit:*)",
    "Bash(git commit --amend:*)",
    "Bash(git reflog expire:*)",
    "Bash(git branch -D:*)",
    "Bash(git tag -d:*)",
    "Bash(rm -rf .git:*)",
    "Bash(rm -rf .git/*)",
    "Bash(Remove-Item .git:*)",
    "Bash(Remove-Item -Recurse -Force .git:*)",
    "Bash(rmdir /s .git:*)",
]


def _test_guard_hook_command() -> str:
    """Command string Claude Code runs for the PreToolUse hook below. Uses
    `sys.executable` (the interpreter running the orchestrator itself, an
    absolute path) rather than relying on a bare "python" being on the
    hook subprocess's PATH - the hook script only needs the stdlib, so any
    interpreter works, but this one is guaranteed to exist."""
    return f'"{sys.executable}" "{_TEST_GUARD_HOOK_PATH}"'


def build_settings() -> dict:
    """Return the JSON-serializable content written to settings.local.json."""
    return {
        "permissions": {
            "allow": list(ALLOWED_RULES),
            "deny": list(DENIED_RULES),
        },
        # Circuit breaker against repeated test-invocation attempts (pytest/
        # python/unittest/cmd variants) after the first permission denial -
        # see orchestrator/hooks/test_command_guard.py for the full
        # rationale. Runs before every Bash tool call; only ever blocks
        # calls that look like running the test suite, everything else
        # (Read/Edit/Write/Glob/Grep, non-Bash tools, and Bash calls that
        # are not test invocations) passes through untouched.
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": _test_guard_hook_command()}],
                }
            ]
        },
    }


def ensure_project_claude_settings(project_dir: Path) -> Path:
    """Create or minimally reconcile project Claude settings.

    A hand-tuned project file is never replaced. For valid JSON, only missing
    managed runtime-launch rules are appended so older generated projects do
    not remain unable to perform a live audit; other custom keys and rules are
    preserved.
    """
    claude_dir = project_dir / ".claude"
    settings_path = claude_dir / SETTINGS_FILENAME
    if settings_path.exists():
        try:
            with settings_path.open("r", encoding="utf-8") as handle:
                current = json.load(handle)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return settings_path
        if not isinstance(current, dict):
            return settings_path
        permissions = current.get("permissions")
        if not isinstance(permissions, dict):
            permissions = {}
            current["permissions"] = permissions
        allow = permissions.get("allow")
        if not isinstance(allow, list):
            allow = []
            permissions["allow"] = allow
        changed = False
        for rule in RUNTIME_GUI_RULES:
            if rule not in allow:
                allow.append(rule)
                changed = True
        if changed:
            try:
                with settings_path.open("w", encoding="utf-8", newline="\n") as handle:
                    handle.write(json.dumps(current, ensure_ascii=False, indent=2) + "\n")
            except OSError:
                # ACL/read-only protection is an explicit operator boundary;
                # do not make doctor or a normal submit fail because an old
                # project settings file cannot be reconciled automatically.
                return settings_path
        return settings_path
    claude_dir.mkdir(parents=True, exist_ok=True)
    # Keep generated repository files compliant with AGENTS.md on Windows as
    # well: Path.write_text() otherwise translates ``\n`` to CRLF, which Git
    # reports as trailing whitespace during controller finalization.
    with settings_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(build_settings(), ensure_ascii=False, indent=2) + "\n")
    return settings_path

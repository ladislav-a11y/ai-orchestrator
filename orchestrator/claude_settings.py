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
(read/create/edit project files, run local Python/pytest/unittest, and the
read-only/staging half of Git), and explicitly deny the Git operations that
lose work (push, hard reset, clean, deleting .git, history rewrites - see
AGENTS.md rule 2) *and* `git commit` itself - committing is the
orchestrator's own job (runner.py/_maybe_commit, autonomous.py/
_commit_if_ready), done only after tests are verified; the agent may
inspect the tree (`git status`, `git diff`, `git add`) but must never create
the commit itself (see AGENTS.md, and NO_COMMIT_INSTRUCTION in
orchestrator/agents/claude_code.py for the matching prompt-level rule).
Anything not listed simply is not auto-approved, which is the safe default
when nobody can click "yes". No blanket "Bash" allow is granted here on
purpose.
"""

from __future__ import annotations

import json
from pathlib import Path

SETTINGS_FILENAME = "settings.local.json"

# Read/create/edit project files, run local Python, run the non-destructive
# half of Git. acceptEdits already auto-approves file edits; listing them
# here too keeps behavior unchanged if permission_mode is ever tightened.
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


def build_settings() -> dict:
    """Return the JSON-serializable content written to settings.local.json."""
    return {
        "permissions": {
            "allow": list(ALLOWED_RULES),
            "deny": list(DENIED_RULES),
        }
    }


def ensure_project_claude_settings(project_dir: Path) -> Path:
    """Create `<project_dir>/.claude/settings.local.json` if it's missing.

    Never overwrites a file that's already there - a project someone has
    hand-tuned locally is left alone. This only fills in the safe default
    for projects the orchestrator sets up itself, so every project under
    `workspace_root` ends up with the same rules without manual setup.
    """
    claude_dir = project_dir / ".claude"
    settings_path = claude_dir / SETTINGS_FILENAME
    if settings_path.exists():
        return settings_path
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(build_settings(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return settings_path

"""Security boundary checks evaluated for the Hermes Agent PoC (DoD point 5).

Self-contained re-implementation of the same *category* of checks the
production orchestrator enforces (see AGENTS.md rules 1, 2, 5, 8) - kept
independent on purpose so this PoC never has to import or modify production
code to prove the boundary is enforceable. These functions never execute a
command; they only classify prompts/commands/paths as safe or not.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

# Mirrors AGENTS.md rule 1 (skip-permissions flags) and rule 2 (history
# rewrite / destructive Git operations) - a Hermes-style local agent must
# reject these the same way the existing providers do.
FORBIDDEN_COMMAND_SUBSTRINGS: tuple[str, ...] = (
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "--dangerously-bypass-approvals-and-sandbox",
    "--yolo",
    "--dangerously-bypass-hook-trust",
    "bypassPermissions",
    "git push",
    "git push --force",
    "git push -f",
    "--force-with-lease",
    "git reset --hard",
    "git rebase",
    "git filter-branch",
    "git clean -fd",
    "git clean -fdx",
    "git branch -D",
    "git commit",
    "git commit --amend",
    "rm -rf",
    "del /f /s /q",
    # Windows PowerShell/CMD deletion and destructive volume commands. Keep
    # these conservative: Hermes receives arbitrary provider prompts, so a
    # destructive command must be rejected before transport invocation even
    # when it uses a PowerShell alias rather than `rm`/`del`.
    "remove-item",
    "remove-itemproperty",
    "rm -recurse",
    "rm -force",
    "ri -recurse",
    "ri -force",
    "rmdir",
    "rd /s",
    "erase /s",
    "format-volume",
    "clear-content",
    "directory]::delete",
    "file]::delete",
    # Common programmatic deletion and shell-evaluation primitives. Blocking
    # the execution bridge as well as individual aliases prevents a caller
    # from trivially bypassing the boundary with Python/PowerShell code.
    "shutil.rmtree",
    "shutil.move",
    "os.remove",
    "os.unlink",
    "os.rmdir",
    "path.unlink(",
    "path.rmdir(",
    "subprocess.",
    "os.system(",
    "os.popen(",
    "invoke-expression",
    "invoke-command",
    "start-process",
    "shell=true",
    "python -c",
    "powershell -command",
    "pwsh -command",
    "cmd /c",
)

# The substring list above is retained for the explicit policy names, but it
# is not the enforcement mechanism on its own.  Providers receive arbitrary
# prompt text, so the boundary also rejects *syntax categories*: inline code
# execution bridges, module loading, and filesystem/process mutation APIs.
# This catches spelling/casing/whitespace variants such as
# ``require('fs').rmSync(...)`` without trying to maintain one entry per
# destructive command spelling.
_INLINE_EXECUTION_RE = re.compile(
    r"(?:^|[\s;&|`(\[])"
    r"(?:python(?:\d+(?:\.\d+)*)?|py|node(?:js)?|powershell(?:\.exe)?|"
    r"pwsh|cmd(?:\.exe)?|bash|sh|zsh)"
    r"\s+(?:-[ce]\b|--eval\b|/c\b|/k\b|-command\b|/command\b)",
    re.IGNORECASE,
)
_ENCODED_EXECUTION_RE = re.compile(
    r"(?:^|[\s;&|`(\[])"
    r"(?:powershell(?:\.exe)?|pwsh|cmd(?:\.exe)?)"
    r"\s+-(?:encodedcommand|encoded-command)\b",
    re.IGNORECASE,
)
_GENERIC_INLINE_CODE_RE = re.compile(
    r"(?:^|[\s;&|`(\[])[a-z0-9_.:/\\-]+\s+"
    r"(?:-[ce]\b|--(?:eval|execute)\b|/c\b|/e\b)",
    re.IGNORECASE,
)
_GENERIC_MUTATION_RE = re.compile(
    r"(?:^|[\s;&|`(\[])"
    r"(?:rm|rmdir|del|erase|unlink|delete|remove|rmtree|truncate|shred|wipe|"
    r"mkfs|format|destroy|commit|rebase|reset|clean|kill|shutdown|reboot)"
    r"(?:\s|$|[-:/\\])",
    re.IGNORECASE,
)
_FILE_MUTATOR_COMMAND_RE = re.compile(
    r"^\s*(?:cp|mv|copy|move|robocopy|xcopy|rsync|dd|install|tee|touch|mkdir|"
    r"rmdir|tar|unzip|7z|chmod|chown|attrib)\b",
    re.IGNORECASE,
)
_SHELL_REDIRECTION_RE = re.compile(
    r"(?:>>?|<)(?:\s*[^\s;&|]+)|(?:\|\s*tee)\b",
    re.IGNORECASE,
)
_READ_ONLY_COMMANDS = frozenset(
    {
        "git status",
        "git diff",
        "git diff --check",
        "git log --oneline -5",
        "git --no-pager status --short",
        "git --no-pager diff --check",
        "git --no-pager log --oneline -5",
        "python -m pytest -q",
        "python -m pytest -q -p no:cacheprovider",
    }
)
_MUTATING_COMMAND_RE = re.compile(
    r"(?:^|[\s;&|`(\[])"
    r"(?:cp|mv|copy|move|robocopy|xcopy|rsync|dd|install|tee|touch|mkdir|"
    r"rmdir|tar|unzip|7z|chmod|chown|attrib|set-content|add-content|out-file|"
    r"new-item|copy-item|move-item|rename-item|remove-item|clear-content)\b",
    re.IGNORECASE,
)
_POWERSHELL_MUTATOR_RE = re.compile(
    r"\b(?:set|add|out|new|copy|move|rename|save|export|clear|remove)-"
    r"(?:content|item|itemproperty|file|acl|variable|alias|location)\b",
    re.IGNORECASE,
)
_DOTNET_MUTATOR_RE = re.compile(
    r"\[\s*system\.io\.(?:file|directory)\s*\]\s*::\s*"
    r"(?:write|append|move|copy|delete|replace|create|open|truncate)[a-z]*\s*\(",
    re.IGNORECASE,
)
_POWERSHELL_SC_ALIAS_RE = re.compile(
    r"^\s*sc\s+[^\s]+\s+[^\s]+",
    re.IGNORECASE,
)
_POWERSHELL_MUTATION_ALIAS_RE = re.compile(
    r"(?:^|[\s;&|`(])"
    r"(?:ac|add-content|ni|cpi|ci|clc|si|sp|rp|ri|rni|mi|rm|del|erase|rd|rmdir|remove-item)\s+"
    r"[^\s;&|]+",
    re.IGNORECASE,
)
_COMMAND_INVOCATION_RE = re.compile(
    r"^\s*(?:python(?:\d+(?:\.\d+)*)?|py|node(?:js)?|powershell(?:\.exe)?|"
    r"pwsh|cmd(?:\.exe)?|bash|sh|zsh|git)\b",
    re.IGNORECASE,
)
_READ_ONLY_COMMAND_RE = re.compile(
    r"^\s*(?:git\s+(?:status|diff(?:\s+.*)?|log(?:\s+.*)?)|"
    r"python(?:\d+(?:\.\d+)*)?\s+-m\s+pytest(?:\s+.*)?)\s*$",
    re.IGNORECASE,
)
_COMMAND_OPTION_SHAPE_RE = re.compile(
    r"^\s*[^\s;&|`()\[\]]+\s+(?:--?[a-z][a-z0-9-]*|/[a-z])\b",
    re.IGNORECASE,
)
_COMMAND_LIKE_PAYLOAD_RE = re.compile(
    r"^\s*[^\s;&|`()\[\]]+\s+.*(?:"
    r"(?:^|\s)--?[a-z][a-z0-9-]*\b|"
    r"(?:^|\s)/[a-z][a-z0-9-]*\b|"
    r"[a-z]:[\\/]|"
    r"\b[a-z][a-z0-9_.-]*=\S+)",
    re.IGNORECASE,
)
_CODE_EXECUTION_RE = re.compile(
    r"(?:\brequire\s*\(|\b__import__\s*\(|\b(?:eval|exec|compile|Function)\s*\()",
    re.IGNORECASE,
)
_NODE_MUTATION_RE = re.compile(
    r"(?:rmSync|rmdirSync|unlinkSync|writeFileSync|appendFileSync|"
    r"renameSync|copyFileSync|truncateSync|execSync|spawnSync)\s*\(",
    re.IGNORECASE,
)
_PYTHON_MUTATION_RE = re.compile(
    r"(?:shutil\s*\.\s*(?:rmtree|move|copy|copytree)|"
    r"os\s*\.\s*(?:remove|unlink|rmdir|system|popen)|"
    r"subprocess\s*\.\s*(?:run|popen|call|check_call|check_output)|"
    r"(?:pathlib\s*\.\s*)?Path\s*\([^)]*\)\s*\.\s*(?:unlink|rmdir)\s*\()",
    re.IGNORECASE,
)
_NODE_FS_IMPORT_RE = re.compile(
    r"(?:require\s*\(\s*['\"](?:node:)?fs['\"]\s*\)|"
    r"from\s+['\"](?:node:)?fs['\"])",
    re.IGNORECASE,
)


class SecurityBoundaryError(ValueError):
    """Raised when a command or path violates the PoC's safety boundary."""


def _assert_command_syntax_is_safe(command: str, *, strict_command: bool) -> None:
    """Fail closed for destructive commands and executable payloads.

    This is deliberately a syntax-category guard, not a claim that a finite
    string list can model every shell.  Ordinary prose and the small set of
    explicitly read-only commands remain usable.  Inline interpreters,
    dynamic module loading, and mutation/process APIs are rejected before a
    transport is called; an unknown executable payload is therefore never
    silently treated as safe merely because it uses a new spelling.
    """
    if not isinstance(command, str) or not command.strip():
        raise SecurityBoundaryError("Command rejected: empty or non-text request.")

    lowered = unicodedata.normalize("NFKC", command).casefold()
    collapsed = re.sub(r"\s+", " ", lowered).strip()
    if collapsed in _READ_ONLY_COMMANDS:
        return
    for forbidden in FORBIDDEN_COMMAND_SUBSTRINGS:
        if forbidden.casefold() in lowered or forbidden.casefold() in collapsed:
            raise SecurityBoundaryError(
                f"Command rejected: contains forbidden pattern '{forbidden}'."
            )

    syntax_patterns = (
        _INLINE_EXECUTION_RE,
        _ENCODED_EXECUTION_RE,
        _GENERIC_INLINE_CODE_RE,
        _GENERIC_MUTATION_RE,
        _FILE_MUTATOR_COMMAND_RE,
        _SHELL_REDIRECTION_RE,
        _POWERSHELL_MUTATOR_RE,
        _MUTATING_COMMAND_RE,
        _DOTNET_MUTATOR_RE,
        _CODE_EXECUTION_RE,
        _NODE_MUTATION_RE,
        _PYTHON_MUTATION_RE,
        _NODE_FS_IMPORT_RE,
        _POWERSHELL_MUTATION_ALIAS_RE,
    )
    for pattern in syntax_patterns:
        match = pattern.search(lowered)
        if match:
            raise SecurityBoundaryError(
                "Command rejected: executable or mutating code syntax is not allowed "
                f"('{match.group(0).strip()}')."
            )

    if _POWERSHELL_SC_ALIAS_RE.search(lowered):
        raise SecurityBoundaryError(
            "Command rejected: ambiguous PowerShell mutation alias is not allowed."
        )
    if not strict_command:
        return

    # An input that is explicitly supplied as a shell command is fail-closed:
    # only the read-only inspection commands used by this PoC are allowed.
    # This is deliberately an allowlist, not a growing list of aliases.
    if not _READ_ONLY_COMMAND_RE.fullmatch(collapsed):
        raise SecurityBoundaryError(
            "Command rejected: command is outside the explicit read-only allowlist."
        )


def assert_command_is_safe(command: str) -> None:
    """Fail closed for an explicit shell command.

    Only the explicit read-only allowlist is accepted.  This makes unknown
    commands and future shell aliases unsafe by default, without requiring
    the boundary to know every alias in advance.
    """
    _assert_command_syntax_is_safe(command, strict_command=True)


def assert_prompt_is_safe(prompt: str) -> None:
    """Check a natural-language agent prompt without treating it as a shell command.

    The prompt may describe work in ordinary language, but embedded
    interpreters, mutating APIs, redirection, and known destructive commands
    remain blocked.  Explicit terminal commands are validated separately by
    ``assert_command_is_safe`` at the command-execution boundary.
    """
    _assert_command_syntax_is_safe(prompt, strict_command=False)


def ensure_within_workspace(path: Path, workspace_root: Path) -> Path:
    """Mirror of `Config._ensure_within_workspace()`'s guarantee (AGENTS.md
    rule 8): resolve both paths and reject if `path` does not resolve inside
    `workspace_root`."""
    resolved_path = path.resolve()
    resolved_root = workspace_root.resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise SecurityBoundaryError(
            f"Path '{resolved_path}' is outside workspace root '{resolved_root}'."
        ) from exc
    return resolved_path


def is_localhost_host(host: str) -> bool:
    """Mirror of `config.py`'s API-host restriction (AGENTS.md rule 5)."""
    return host in {"127.0.0.1", "localhost", "::1"}

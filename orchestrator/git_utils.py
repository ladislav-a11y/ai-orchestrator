"""Minimal, deliberately conservative Git wrapper.

Hard invariants (per project safety rules, see AGENTS.md):
  - never rewrites or deletes history (no reset --hard, no rebase, no filter-branch)
  - never force-pushes
  - never pushes anywhere in this phase (push is simply not implemented)
  - never commits when the caller says tests failed
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


class GitError(RuntimeError):
    pass


@dataclass
class GitResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int


def _run(args: list[str], cwd: Path, timeout: int = 60) -> GitResult:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as e:
        raise GitError("Git není nainstalovaný nebo není v PATH.") from e
    except subprocess.TimeoutExpired as e:
        raise GitError(f"Git příkaz vypršel po {timeout}s: git {' '.join(args)}") from e
    return GitResult(
        ok=proc.returncode == 0,
        stdout=proc.stdout.strip(),
        stderr=proc.stderr.strip(),
        returncode=proc.returncode,
    )


def is_git_repo(project_path: Path) -> bool:
    if not project_path.exists():
        return False
    result = _run(["rev-parse", "--show-toplevel"], cwd=project_path)
    if not result.ok or not result.stdout.strip():
        return False
    # ``--is-inside-work-tree`` also returns true for any ordinary
    # subdirectory of a parent repository.  A registered project must be the
    # worktree root itself; otherwise Git operations could accidentally act
    # on the enclosing project instead of the requested directory.
    return Path(result.stdout).resolve() == project_path.resolve()


def has_uncommitted_changes(project_path: Path) -> bool:
    result = _run(["status", "--porcelain"], cwd=project_path)
    return result.ok and bool(result.stdout.strip())


def status_porcelain(project_path: Path) -> str:
    result = _run(["status", "--porcelain"], cwd=project_path)
    return result.stdout


def diff(project_path: Path, staged: bool = False) -> str:
    args = ["diff", "--staged"] if staged else ["diff"]
    result = _run(args, cwd=project_path, timeout=120)
    return result.stdout


def diff_stat(project_path: Path) -> str:
    result = _run(["diff", "--stat"], cwd=project_path, timeout=120)
    return result.stdout


def current_branch(project_path: Path) -> Optional[str]:
    result = _run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=project_path)
    return result.stdout.strip() if result.ok else None


def current_head(project_path: Path) -> Optional[str]:
    result = _run(["rev-parse", "HEAD"], cwd=project_path)
    return result.stdout.strip() if result.ok else None


def origin_url(project_path: Path) -> Optional[str]:
    result = _run(["remote", "get-url", "origin"], cwd=project_path)
    return result.stdout.strip() if result.ok else None


def remote_branch_head(project_path: Path, branch: str) -> Optional[str]:
    result = _run(
        ["ls-remote", "origin", f"refs/heads/{branch}"],
        cwd=project_path,
        timeout=120,
    )
    if not result.ok or not result.stdout:
        return None
    return result.stdout.split()[0]


def commit(project_path: Path, message: str) -> str:
    """Stage all changes and commit. Returns the new commit hash.

    Raises GitError if there is nothing to commit or the commit fails.
    Never uses --amend, --force, reset, or any history-rewriting flag.
    """
    if not has_uncommitted_changes(project_path):
        raise GitError("Není co commitnout - žádné změny v pracovním stromu.")

    add_result = _run(["add", "-A"], cwd=project_path)
    if not add_result.ok:
        raise GitError(f"'git add -A' selhalo: {add_result.stderr}")

    commit_result = _run(["commit", "-m", message], cwd=project_path)
    if not commit_result.ok:
        raise GitError(f"'git commit' selhalo: {commit_result.stderr or commit_result.stdout}")

    hash_result = _run(["rev-parse", "HEAD"], cwd=project_path)
    return hash_result.stdout.strip() if hash_result.ok else ""

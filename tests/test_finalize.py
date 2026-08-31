from __future__ import annotations

import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

from orchestrator.config import Config
from orchestrator.finalize import _safe_paths, finalize_repository


def _git(path, *args):
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    )


@contextmanager
def _temporary_repo():
    with tempfile.TemporaryDirectory(prefix="orchestrator-finalize-") as directory:
        path = Path(directory)
        _repo(path)
        yield path


def _repo(path: Path):
    _git(path, "init", "--initial-branch=main")
    _git(path, "config", "user.email", "test@example.invalid")
    _git(path, "config", "user.name", "finalize-test")
    (path / "tracked.txt").write_text("before\n", encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-m", "initial")


def test_safe_paths_rejects_metadata_and_traversal():
    for value in (".git", ".git/config", "../outside", "C:/outside"):
        try:
            _safe_paths([value])
        except ValueError:
            continue
        raise AssertionError(f"unsafe path accepted: {value}")


def test_finalize_commits_explicit_scope_and_leaves_clean_worktree():
    with _temporary_repo() as path:
        (path / "tracked.txt").write_text("after", encoding="utf-8")
        result = finalize_repository(
            path, Config(), "run-1", goal="test finalization", paths=["tracked.txt"]
        )

        assert result["status"] == "completed", result.get("error")
        assert result["committed"] is True
        assert result["clean"] is True
        assert _git(path, "status", "--porcelain").stdout == ""


def test_finalize_blocks_when_worktree_contains_path_outside_scope():
    with _temporary_repo() as path:
        (path / "tracked.txt").write_text("after", encoding="utf-8")
        (path / "unapproved.txt").write_text("must not be staged", encoding="utf-8")
        result = finalize_repository(
            path, Config(), "run-2", paths=["tracked.txt"]
        )

        assert result["status"] == "blocked", result.get("error")
        assert result["committed"] is False
        assert "unapproved.txt" in result["outside_scope"]
        assert _git(path, "log", "-1", "--pretty=%s").stdout.strip() == "initial"

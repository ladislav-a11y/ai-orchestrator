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


def test_finalize_auto_scopes_to_dirty_status_when_no_paths_configured():
    """Most projects never get a curated finalize-paths allowlist (see
    scripts/run-ai-project-manager.ps1's $finalizePaths - historically only
    ever set up for the self-modifying "AI Project Manager" project itself).
    An empty ``paths`` must not mean "block forever" for every other,
    ordinary target project (incident: Station Agent - oprava P5,
    2026-09-03) - it must fall back to exactly what is dirty right now,
    still staged path-by-path, never ``git add -A``."""
    with _temporary_repo() as path:
        (path / "tracked.txt").write_text("after", encoding="utf-8")
        (path / "new_file.txt").write_text("brand new", encoding="utf-8")
        result = finalize_repository(path, Config(), "run-3", goal="auto scope")

        assert result["status"] == "completed", result.get("error")
        assert result["committed"] is True
        assert result["clean"] is True
        assert _git(path, "status", "--porcelain").stdout == ""
        committed_files = _git(
            path, "show", "--name-only", "--pretty=", "HEAD"
        ).stdout.split()
        assert sorted(committed_files) == ["new_file.txt", "tracked.txt"]


def test_finalize_reports_committed_when_blocked_on_push_allowlist():
    """Regression: _blocked() used to hardcode committed=False/clean=False,
    so a caller reporting a real commit that then got blocked on push
    authorization (committed=True passed explicitly) crashed with
    ``TypeError: _result() got multiple values for keyword argument
    'committed'`` instead of returning a clean, parseable result. This is
    not a corner case - it is the first block a REAL commit ever hits, so
    the exception must never reach the caller as a raw traceback (see
    incident: Station Agent - oprava P5, 2026-09-03, where this exact crash
    was the caller-visible "controller finalization" error)."""
    with _temporary_repo() as path:
        (path / "tracked.txt").write_text("after", encoding="utf-8")
        _git(path, "remote", "add", "origin", "https://example.invalid/not-allowed.git")
        result = finalize_repository(
            path, Config(), "run-4", goal="push scope",
            paths=["tracked.txt"], push_requested=True,
            allowed_remote="https://example.invalid/actually-allowed.git",
        )

        assert result["status"] == "blocked"
        assert result["committed"] is True
        assert result["clean"] is True
        assert "push allowlist" in result["error"]
        assert _git(path, "log", "-1", "--pretty=%s").stdout.strip() != "initial"


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


def test_finalize_accepts_explicit_directory_scope_and_stages_dirty_paths():
    with _temporary_repo() as path:
        _git(path, "config", "core.autocrlf", "false")
        source = path / "src"
        source.mkdir()
        (source / "tracked.py").write_text("before\n", encoding="utf-8", newline="\n")
        _git(path, "add", "src/tracked.py")
        _git(path, "commit", "-m", "add source")

        (source / "tracked.py").write_text("after\n", encoding="utf-8", newline="\n")
        (source / "new.py").write_text("new\n", encoding="utf-8", newline="\n")
        result = finalize_repository(
            path, Config(), "run-directory-scope", paths=["src/"]
        )

        assert result["status"] == "completed", result.get("error")
        committed_files = _git(
            path, "show", "--name-only", "--pretty=", "HEAD"
        ).stdout.split()
        assert sorted(committed_files) == ["src/new.py", "src/tracked.py"]

"""Controller-owned, explicit-scope repository finalization."""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
from pathlib import Path
from typing import Optional, Sequence

from orchestrator.autonomous import _detect_test_command, run_test_command
from orchestrator.config import Config


logger = logging.getLogger("orchestrator.finalize")


def _git(
    project_path: Path,
    args: Sequence[str],
    timeout: int = 120,
    input_text: Optional[str] = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(project_path), capture_output=True,
        input=input_text, text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )


def _result(status: str, **fields) -> dict:
    return {"status": status, "done": status == "completed", **fields}


def _blocked(reason: str, **fields) -> dict:
    # committed/clean default to False (the common case: blocked before
    # anything happened), but several call sites block AFTER a real commit
    # already landed (e.g. push authorization/failure) and must report that
    # truthfully - an explicit field always overrides the default instead of
    # colliding with it as a duplicate keyword argument.
    merged = {"committed": False, "clean": False, **fields}
    return _result("blocked", error=reason, **merged)


def _safe_paths(paths: Sequence[str]) -> list[str]:
    safe = []
    for raw in paths:
        path = str(raw).strip().replace("\\", "/")
        candidate = Path(path)
        if not path or candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError(f"unsafe finalize path: {raw!r}")
        if path == ".git" or path.startswith(".git/"):
            raise ValueError(f"Git metadata cannot be finalized: {raw!r}")
        if path not in safe:
            safe.append(path)
    return safe


def _status_paths(status_output: str) -> list[str]:
    paths = []
    # ``--porcelain=v1 -z`` preserves arbitrary Windows filenames without
    # C-style quoting and also avoids newline ambiguity.  Keep accepting the
    # line-oriented form for callers/tests that provide legacy output.
    records = status_output.split("\0") if "\0" in status_output else status_output.splitlines()
    for line in records:
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[1]
        if path:
            paths.append(path.replace("\\", "/"))
    return paths


def _status_records(status_output: str) -> list[tuple[str, str, str]]:
    """Parse porcelain status into index status, worktree status and path."""
    records = status_output.split("\0") if "\0" in status_output else status_output.splitlines()
    parsed = []
    for line in records:
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[1]
        if path:
            parsed.append((line[0], line[1], path.replace("\\", "/")))
    return parsed


def _git_add_paths(project_path: Path, paths: Sequence[str]) -> subprocess.CompletedProcess:
    """Stage explicit paths without putting the whole path list in argv.

    A live GUI verification can create hundreds of browser-profile files.
    Passing those paths as individual Windows command-line arguments hits the
    OS command-length limit before Git can apply the finalizer's scope rules.
    Git's NUL-delimited pathspec input keeps the operation explicit while
    making its size independent of argv limits.
    """
    payload = "".join(f"{path}\0" for path in paths)
    return _git(
        project_path,
        ["add", "--pathspec-from-file=-", "--pathspec-file-nul"],
        input_text=payload,
    )


_TRANSIENT_PATH_COMPONENTS = frozenset({
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".nox",
    "bin", "obj",
})
_BUILD_ARTIFACT_NAMES = frozenset({
    "project.assets.json",
    ".nuget.g.props",
    ".nuget.g.targets",
})
_BUILD_ARTIFACT_SUFFIXES = (
    ".g.cs", ".g.i.cs", ".filelistabsolute.txt", ".dll", ".exe", ".pdb",
    ".deps.json", ".runtimeconfig.json", ".assets.cache",
)
_TRANSIENT_FILE_SUFFIXES = (".pyc", ".pyo", ".tmp", ".temp", ".bak", ".swp")
_TRANSIENT_ROOT_PREFIXES = (
    "_iter_layout",
    "_iter1_",
    "_debug_hang",
    "_gui_verify",
)


def _transient_paths(paths: Sequence[str]) -> list[str]:
    """Return generated/cache paths that must be cleaned, never committed.

    This is deliberately project-agnostic. It rejects artifact categories,
    not application filenames, so dynamic self-update scope remains able to
    finalize any legitimate new source or documentation file.
    """
    transient = []
    for raw in paths:
        normalized = str(raw).replace("\\", "/")
        lowered = normalized.casefold()
        components = {part.casefold() for part in normalized.split("/")}
        name = normalized.rsplit("/", 1)[-1]
        root_name = normalized.split("/", 1)[0].casefold()
        if (
            components.intersection(_TRANSIENT_PATH_COMPONENTS)
            or lowered.endswith(_TRANSIENT_FILE_SUFFIXES)
            or name.endswith("~")
            or name == ".coverage"
            or name.startswith(".coverage.")
            or any(root_name.startswith(prefix) for prefix in _TRANSIENT_ROOT_PREFIXES)
        ):
            transient.append(normalized)
    return transient


def _safe_build_artifact_path(relative: str) -> bool:
    """Return whether a path is a conventional Debug/Release build artifact."""
    normalized = str(relative).replace("\\", "/")
    components = [part.casefold() for part in normalized.split("/")]
    if not {"bin", "obj"}.intersection(components):
        return False
    if not {"debug", "release"}.intersection(components):
        return False
    name = components[-1]
    return name in _BUILD_ARTIFACT_NAMES or name.endswith(_BUILD_ARTIFACT_SUFFIXES)


def _cleanup_transient_paths(
    project_path: Path,
    status_output: str,
) -> tuple[list[str], Optional[str]]:
    """Remove only safe, generated build paths reported by Git.

    Agents can accidentally stage build output before the controller sees it.
    Known Debug/Release output is disposable controller state. Other
    transient paths remain protected by the normal fail-closed check.
    """
    records = _status_records(status_output)
    transient_records = [
        (index_status, path)
        for index_status, _worktree_status, path in records
        if _safe_build_artifact_path(path)
    ]
    if not transient_records:
        return [], None

    cleaned: list[str] = []
    for index_status, relative in transient_records:
        candidate = (project_path / relative).resolve()
        if candidate == project_path or project_path not in candidate.parents:
            return cleaned, f"unsafe build cleanup path: {relative}"
        if index_status not in {" ", "?"}:
            unstaged = _git(project_path, ["restore", "--staged", "--", relative])
            if unstaged.returncode != 0:
                return cleaned, f"could not unstage build artifact {relative}: {unstaged.stderr}"
        try:
            if candidate.is_file() or candidate.is_symlink():
                candidate.unlink()
            cleaned.append(relative)
        except OSError as exc:
            return cleaned, f"could not remove build artifact {relative}: {exc}"

    return cleaned, None


def _path_in_scope(path: str, scopes: Sequence[str]) -> bool:
    """Return whether a dirty path matches an exact path or directory scope."""
    normalized = str(path).replace("\\", "/")
    for scope in scopes:
        candidate = str(scope).replace("\\", "/")
        if candidate.endswith("/"):
            root = candidate.rstrip("/")
            if normalized == root or normalized.startswith(f"{root}/"):
                return True
        elif normalized == candidate:
            return True
    return False


def finalize_repository(
    project_path: Path,
    config: Config,
    run_id: str,
    goal: str = "",
    paths: Sequence[str] = (),
    preexisting_paths: Sequence[str] = (),
    test_command: Optional[str] = None,
    push_requested: bool = False,
    allowed_remote: Optional[str] = None,
) -> dict:
    """Finalize current-task paths while preserving pre-existing user work.

    This is intentionally separate from the agent loop.  When the caller
    supplies ``paths`` (a curated, per-project allowlist - see
    ``AI_ORCHESTRATOR_FINALIZE_PATHS``), that list is a hard scope: any
    except for paths explicitly identified by the caller as pre-existing
    before the task started. Most projects never get a curated task-path
    list, so an empty ``paths`` still derives the current-task scope from
    ``git status --porcelain`` - but it never absorbs the supplied baseline.
    Baseline paths remain dirty and are reported in the finalization proof;
    they are not staged, committed, or pushed by this task. This function
    never uses ``git add -A`` and never offers force-push or history rewrite.
    """
    project_path = Path(project_path).resolve()
    try:
        safe_paths = _safe_paths(paths)
        safe_preexisting_paths = _safe_paths(preexisting_paths)
    except ValueError as exc:
        return _blocked(str(exc))
    explicit_scope = bool(safe_paths)

    repo = _git(project_path, ["rev-parse", "--show-toplevel"])
    if repo.returncode != 0 or Path(repo.stdout.strip()).resolve() != project_path:
        return _blocked(f"not a Git worktree root: {project_path}")
    branch = _git(project_path, ["branch", "--show-current"]).stdout.strip()
    if not branch:
        return _blocked("detached HEAD cannot be finalized safely")

    selected_test = test_command or _detect_test_command(project_path)
    tests_passed = True
    test_output = ""
    if selected_test:
        tests_passed, test_output = run_test_command(project_path, selected_test, logger)
    if not tests_passed:
        return _blocked(
            "controller test command failed", tests_passed=False,
            test_command=selected_test, test_output=test_output[-4000:], branch=branch,
        )

    check = _git(project_path, ["diff", "--check"])
    if check.returncode != 0:
        return _blocked("git diff --check failed", diff_check=check.stderr or check.stdout, branch=branch)

    before = _git(
        project_path,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
    )
    if before.returncode != 0:
        return _blocked("could not read Git status", branch=branch)
    dirty = bool(before.stdout.strip())
    committed = False
    head = _git(project_path, ["rev-parse", "HEAD"]).stdout.strip()
    baseline_remaining: list[str] = []
    task_paths_for_proof: list[str] = []
    cleaned_transient_paths: list[str] = []
    if dirty:
        cleaned_transient_paths, cleanup_error = _cleanup_transient_paths(
            project_path, before.stdout
        )
        if cleanup_error:
            return _blocked(
                "controller cleanup of generated build artifacts failed",
                cleanup_error=cleanup_error, cleaned_transient_paths=cleaned_transient_paths,
                branch=branch,
            )
        if cleaned_transient_paths:
            before = _git(
                project_path,
                ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            )
            if before.returncode != 0:
                return _blocked(
                    "could not read Git status after transient cleanup",
                    cleaned_transient_paths=cleaned_transient_paths, branch=branch,
                )
            dirty = bool(before.stdout.strip())
        if not dirty:
            task_paths_for_proof = []
        else:
            dirty_paths = _status_paths(before.stdout)
            transient_paths = _transient_paths(dirty_paths)
            if transient_paths:
                return _blocked(
                    "temporary/cache artifacts must be cleaned before controller finalization",
                    dirty_paths=dirty_paths, transient_paths=transient_paths,
                    cleaned_transient_paths=cleaned_transient_paths, branch=branch,
                )
            baseline_paths = [
                path for path in dirty_paths
                if _path_in_scope(path, safe_preexisting_paths)
            ]
            staged_baseline = [
                path for index_status, _worktree_status, path in _status_records(before.stdout)
                if path in baseline_paths and index_status not in {" ", "?"}
            ]
            if staged_baseline:
                return _blocked(
                    "pre-existing user changes are already staged; refusing to alter their index state",
                    dirty_paths=dirty_paths, preexisting_paths=baseline_paths,
                    staged_preexisting_paths=staged_baseline, branch=branch,
                )
            task_dirty_paths = [path for path in dirty_paths if path not in baseline_paths]
            if explicit_scope:
                outside_scope = [
                    path for path in task_dirty_paths if not _path_in_scope(path, safe_paths)
                ]
                if outside_scope:
                    return _blocked(
                        "dirty worktree contains paths outside the explicit finalize scope",
                        dirty_paths=dirty_paths, outside_scope=outside_scope, branch=branch,
                    )
            else:
                try:
                    safe_paths = _safe_paths(task_dirty_paths)
                except ValueError as exc:
                    return _blocked(str(exc), branch=branch)
            stage_paths = [path for path in task_dirty_paths if _path_in_scope(path, safe_paths)]
            if not stage_paths:
                return _blocked(
                    "no current-task changes are available for controller finalization",
                    dirty_paths=dirty_paths, preexisting_paths=baseline_paths,
                    cleaned_transient_paths=cleaned_transient_paths, branch=branch,
                )
            task_paths_for_proof = list(stage_paths)
            staged = _git_add_paths(project_path, stage_paths)
            if staged.returncode != 0:
                return _blocked(
                    f"git add of explicit finalize paths failed: {staged.stderr}",
                    cleaned_transient_paths=cleaned_transient_paths, branch=branch,
                )
            staged_check = _git(project_path, ["diff", "--cached", "--check"])
            if staged_check.returncode != 0:
                return _blocked(
                    "git diff --cached --check failed",
                    staged_diff_check=staged_check.stderr or staged_check.stdout,
                    cleaned_transient_paths=cleaned_transient_paths, branch=branch,
                )
            summary = goal.strip().splitlines()[0][:72] if goal.strip() else "finalize repository"
            message = f"{config.git.commit_message_prefix}{summary}\n\nController finalization {run_id}"
            commit = _git(project_path, ["commit", "-m", message])
            if commit.returncode != 0:
                return _blocked(
                    f"controller commit failed: {commit.stderr or commit.stdout}",
                    cleaned_transient_paths=cleaned_transient_paths, branch=branch,
                )
            committed = True
            head = _git(project_path, ["rev-parse", "HEAD"]).stdout.strip()

    after = _git(
        project_path,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
    )
    if after.returncode != 0:
        return _blocked(
            "post-commit Git status is not clean", committed=committed,
            commit_hash=head, clean=False, status=after.stdout.strip(), branch=branch,
        )
    remaining_paths = _status_paths(after.stdout)
    unexpected_remaining = [
        path for path in remaining_paths
        if not _path_in_scope(path, safe_preexisting_paths)
    ]
    if unexpected_remaining:
        return _blocked(
            "post-commit Git status contains unapproved current-task paths",
            committed=committed, commit_hash=head, clean=False,
            status=after.stdout.strip(), remaining_paths=remaining_paths,
            unexpected_remaining=unexpected_remaining, branch=branch,
        )
    baseline_remaining = [
        path for path in remaining_paths
        if _path_in_scope(path, safe_preexisting_paths)
    ]

    origin = _git(project_path, ["remote", "get-url", "origin"]).stdout.strip()
    remote_head = None
    if push_requested:
        if not origin:
            return _blocked("origin remote is missing", committed=committed, commit_hash=head, clean=True)
        if not allowed_remote or origin != allowed_remote:
            return _blocked(
                "origin remote is not on the explicit push allowlist",
                committed=committed, commit_hash=head, clean=True, remote=origin,
            )
        pushed = _git(project_path, ["push", "origin", branch], timeout=120)
        if pushed.returncode != 0:
            return _blocked(
                f"git push failed: {pushed.stderr or pushed.stdout}",
                committed=committed, commit_hash=head, clean=True, remote=origin,
            )
        lookup = _git(project_path, ["ls-remote", "origin", f"refs/heads/{branch}"])
        if lookup.returncode == 0 and lookup.stdout.strip():
            remote_head = lookup.stdout.split()[0]
        if remote_head != head:
            return _blocked(
                "remote HEAD does not match local HEAD after push",
                committed=committed, commit_hash=head, clean=True,
                remote=origin, remote_commit=remote_head,
            )

    return _result(
        "completed", committed=committed, commit_hash=head, clean=True,
        tests_passed=True, test_command=selected_test, branch=branch,
        remote=origin or None, pushed=push_requested, remote_commit=remote_head,
        preexisting_paths=baseline_remaining,
        task_paths=task_paths_for_proof, cleaned_transient_paths=cleaned_transient_paths,
        scope_policy="preexisting paths preserved outside current-task commit",
        run_id=run_id,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Controller-owned explicit-scope commit and verified push"
    )
    parser.add_argument("--project", required=True, help="Registered project name or checkout path")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--goal", default="")
    parser.add_argument("--test-command")
    parser.add_argument("--path", action="append", default=[])
    parser.add_argument("--preexisting-path", action="append", default=[])
    parser.add_argument("--push", action="store_true")
    parser.add_argument("--allowed-remote")
    args = parser.parse_args(argv)
    try:
        from orchestrator.config import load_config

        config = load_config()
        entry = config.resolve_project(args.project)
        result = finalize_repository(
            Path(entry.path), config, args.run_id, goal=args.goal,
            paths=args.path, preexisting_paths=args.preexisting_path,
            test_command=args.test_command,
            push_requested=args.push, allowed_remote=args.allowed_remote,
        )
    except (FileNotFoundError, ValueError, KeyError) as exc:
        result = _blocked(str(exc))
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result.get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

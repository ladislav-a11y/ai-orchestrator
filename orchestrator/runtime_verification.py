"""Bounded, shell-free runtime checks for independent audits."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class RuntimeCheckResult:
    """Concrete evidence from one configured runtime invocation."""

    command: tuple[str, ...]
    exit_code: int | None
    output: str
    passed: bool
    timed_out: bool = False

    def evidence(self, expected: str) -> str:
        status = "OK" if self.passed else "FAIL"
        detail = self.output.strip() or "(bez výstupu)"
        return (
            f"runtime:{status} command={list(self.command)!r} exit_code={self.exit_code!r} "
            f"expect={expected!r} output={detail[-2000:]}"
        )


def _terminate(process: subprocess.Popen) -> None:
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
                timeout=10,
            )
            return
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        process.kill()
    except OSError:
        pass


def run_runtime_check(
    project_path: Path,
    command: Sequence[str],
    expected: str,
    *,
    timeout_seconds: float = 60.0,
) -> RuntimeCheckResult:
    """Run a configured runtime command without invoking a shell.

    The command is configuration-owned, not read from agent output.  The
    working directory is the already validated project checkout and the
    process tree is terminated on timeout so a desktop app cannot wedge the
    audit worker indefinitely.
    """
    argv = tuple(str(part) for part in command)
    if not argv or not argv[0].strip():
        raise ValueError("runtime command must contain an executable")
    if timeout_seconds <= 0:
        raise ValueError("runtime timeout must be positive")
    if not isinstance(expected, str) or not expected.strip():
        raise ValueError("runtime expected output must be a non-empty string")

    # On Windows, ``subprocess.Popen(..., cwd=...)`` changes the child
    # working directory but does not reliably resolve a relative executable
    # against that directory. A project-owned interpreter such as
    # ``.venv/Scripts/python.exe`` would therefore be looked up from AO/PM's
    # current directory and the runtime check could silently use the wrong
    # Python (or fail with ModuleNotFoundError). Resolve an existing relative
    # executable against the already validated checkout, while preserving
    # PATH-based commands such as ``python`` when no project-local match
    # exists.
    executable = Path(argv[0])
    if not executable.is_absolute():
        project_executable = project_path / executable
        if project_executable.is_file():
            argv = (str(project_executable.resolve()), *argv[1:])

    process = subprocess.Popen(
        list(argv),
        cwd=str(project_path),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    try:
        output, _ = process.communicate(timeout=timeout_seconds)
        exit_code = process.returncode
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        _terminate(process)
        output, _ = process.communicate()
        output = output or (exc.output if isinstance(exc.output, str) else "")
        exit_code = None
        timed_out = True

    output = output or ""
    return RuntimeCheckResult(
        command=argv,
        exit_code=exit_code,
        output=output,
        passed=not timed_out and exit_code == 0 and expected in output,
        timed_out=timed_out,
    )

import sys
import shutil
from pathlib import Path

from orchestrator.runtime_verification import run_runtime_check


def test_runtime_check_runs_without_a_shell_and_captures_expected_output(tmp_path: Path):
    result = run_runtime_check(
        tmp_path,
        [sys.executable, "-c", "print('RUNTIME_OK')"],
        "RUNTIME_OK",
    )

    assert result.passed is True
    assert result.exit_code == 0
    assert "RUNTIME_OK" in result.output
    assert "runtime:OK" in result.evidence("RUNTIME_OK")


def test_runtime_check_fails_on_unexpected_output(tmp_path: Path):
    result = run_runtime_check(
        tmp_path,
        [sys.executable, "-c", "print('OTHER')"],
        "RUNTIME_OK",
    )

    assert result.passed is False
    assert result.exit_code == 0


def test_runtime_check_resolves_project_owned_relative_executable(tmp_path: Path):
    executable = tmp_path / "runtime-python.exe"
    shutil.copy2(sys.executable, executable)
    venv_cfg = Path(sys.executable).parent.parent / "pyvenv.cfg"
    if venv_cfg.exists():
        shutil.copy2(venv_cfg, tmp_path / "pyvenv.cfg")

    result = run_runtime_check(
        tmp_path,
        ["runtime-python.exe", "-c", "print('PROJECT_RUNTIME_OK')"],
        "PROJECT_RUNTIME_OK",
    )

    assert result.passed is True
    assert result.command[0] == str(executable.resolve())


def test_runtime_check_terminates_a_hung_process(tmp_path: Path):
    result = run_runtime_check(
        tmp_path,
        [sys.executable, "-c", "import time; time.sleep(30)"],
        "RUNTIME_OK",
        timeout_seconds=0.05,
    )

    assert result.passed is False
    assert result.timed_out is True
    assert result.exit_code is None

import json
import subprocess
import sys
from pathlib import Path

from orchestrator.hooks.test_command_guard import (
    FIRST_DENIAL_REASON,
    MAX_ATTEMPTS_PER_CATEGORY,
    REPEATED_DENIAL_REASON,
    TEST_RUN_CATEGORY,
    classify_test_command,
    evaluate,
    load_state,
    read_saved_attempts,
    save_state,
)

HOOK_SCRIPT = Path(__file__).resolve().parent.parent / "orchestrator" / "hooks" / "test_command_guard.py"

# 15 different-looking ways an agent might retry "run the tests" after the
# first attempt was denied - the exact repeated-retry pattern seen in real
# runs (pytest / python -m pytest / unittest / cmd-wrapped variants).
EQUIVALENT_TEST_COMMANDS = [
    "pytest",
    "pytest -q",
    "python -m pytest",
    "python3 -m pytest -x",
    "py -m pytest",
    ".venv\\Scripts\\python.exe -m pytest",
    "python -m unittest discover",
    "python -m unittest tests.test_foo",
    "nosetests",
    'cmd /c "pytest"',
    'cmd.exe /c "python -m pytest -q"',
    'powershell -Command "python -m pytest"',
    "pytest tests/ -v",
    "python -m pytest --maxfail=1",
    "cd project && pytest",
]


def test_classify_test_command_detects_equivalent_variants():
    for command in EQUIVALENT_TEST_COMMANDS:
        assert classify_test_command(command) == TEST_RUN_CATEGORY, command


def test_classify_test_command_ignores_unrelated_commands():
    assert classify_test_command("git status") is None
    assert classify_test_command("python script.py") is None
    assert classify_test_command("cat pytest.ini") is None
    assert classify_test_command("") is None
    assert classify_test_command(None) is None  # type: ignore[arg-type]


def test_max_attempts_per_category_is_one():
    assert MAX_ATTEMPTS_PER_CATEGORY == 1


def test_circuit_breaker_blocks_all_but_the_first_of_15_repeated_attempts():
    """Regression test for the real-world failure mode: Claude retrying an
    equivalent pytest/python/unittest/cmd command up to 10-20x after the
    permission system denies the first one. Simulates 15 attempts (using 15
    differently-worded but equivalent commands, exactly like a confused
    agent would try) through the same state that persists across Bash tool
    calls within one Claude Code session, and verifies only the very first
    one is treated as a genuine (explained) denial - every one of the
    other 14 is short-circuited before it would ever reach a real
    permission decision / process."""
    assert len(EQUIVALENT_TEST_COMMANDS) == 15
    state = {"categories": {}, "saved_attempts": 0}

    outcomes = []
    for command in EQUIVALENT_TEST_COMMANDS:
        category = classify_test_command(command)
        assert category == TEST_RUN_CATEGORY
        state, should_block, reason = evaluate(state, category)
        outcomes.append((should_block, reason))

    first_block, first_reason = outcomes[0]
    assert first_block is True
    assert first_reason == FIRST_DENIAL_REASON

    repeats = outcomes[1:]
    assert len(repeats) == 14
    for should_block, reason in repeats:
        assert should_block is True
        assert reason == REPEATED_DENIAL_REASON

    # Only the first attempt was a "real" evaluation; the other 14 were
    # saved (short-circuited) - this is the number the orchestrator's own
    # task log surfaces (see runner.py/_record_permission_denials and
    # autonomous.py/note_breaker_savings).
    assert state["saved_attempts"] == 14
    assert state["categories"][TEST_RUN_CATEGORY] == 15


def test_evaluate_keeps_separate_categories_independent():
    state = {"categories": {}, "saved_attempts": 0}
    state, block_a, reason_a = evaluate(state, "test-run")
    state, block_b, reason_b = evaluate(state, "other-category")
    assert block_a is True and reason_a == FIRST_DENIAL_REASON
    assert block_b is True and reason_b == FIRST_DENIAL_REASON
    assert state["saved_attempts"] == 0


def test_state_round_trips_through_disk(tmp_path: Path):
    path = tmp_path / "state.json"
    state = load_state(path)
    assert state == {"categories": {}, "saved_attempts": 0}
    state, _, _ = evaluate(state, TEST_RUN_CATEGORY)
    save_state(path, state)

    reloaded = load_state(path)
    assert reloaded["categories"][TEST_RUN_CATEGORY] == 1


def test_load_state_recovers_from_corrupt_file(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text("not json{{{", encoding="utf-8")
    assert load_state(path) == {"categories": {}, "saved_attempts": 0}


def test_read_saved_attempts_reads_back_hook_state(tmp_path: Path):
    project_dir = tmp_path / "myproj"
    project_dir.mkdir()
    assert read_saved_attempts(project_dir, "sess-1") == 0

    state = {"categories": {}, "saved_attempts": 0}
    for command in EQUIVALENT_TEST_COMMANDS:
        state, _, _ = evaluate(state, classify_test_command(command))
    state_path = project_dir / ".claude" / ".test_guard_state" / "sess-1.json"
    save_state(state_path, state)

    assert read_saved_attempts(project_dir, "sess-1") == 14
    assert read_saved_attempts(project_dir, "other-session") == 0
    assert read_saved_attempts(project_dir, None) == 0


def _run_hook_process(payload: dict, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(payload),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_hook_process_blocks_first_attempt_with_exit_code_2(tmp_path: Path):
    payload = {
        "session_id": "sess-cli",
        "cwd": str(tmp_path),
        "tool_name": "Bash",
        "tool_input": {"command": "pytest -q"},
    }
    proc = _run_hook_process(payload, tmp_path)
    assert proc.returncode == 2
    assert proc.stderr.strip() == FIRST_DENIAL_REASON


def test_hook_process_short_circuits_repeated_attempts_across_15_invocations(tmp_path: Path):
    """End-to-end version of the 15-repeated-attempts regression test,
    driving the actual hook entry point (the real Claude Code CLI contract:
    JSON on stdin, exit code 2 = block) once per attempt, exactly as Claude
    Code would invoke it for each Bash tool call in the same session -
    verifies only the first of the 15 real subprocess invocations is
    treated as the genuine denial."""
    session_id = "sess-cli-repeat"
    exit_codes = []
    stderrs = []
    for command in EQUIVALENT_TEST_COMMANDS:
        payload = {
            "session_id": session_id,
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": command},
        }
        proc = _run_hook_process(payload, tmp_path)
        exit_codes.append(proc.returncode)
        stderrs.append(proc.stderr.strip())

    assert exit_codes == [2] * 15
    assert stderrs[0] == FIRST_DENIAL_REASON
    assert stderrs[1:] == [REPEATED_DENIAL_REASON] * 14
    assert read_saved_attempts(tmp_path, session_id) == 14


def test_hook_process_allows_non_bash_and_unrelated_bash_commands(tmp_path: Path):
    for payload in (
        {"session_id": "s", "cwd": str(tmp_path), "tool_name": "Read", "tool_input": {"file_path": "a.py"}},
        {"session_id": "s", "cwd": str(tmp_path), "tool_name": "Bash", "tool_input": {"command": "git status"}},
        {"session_id": "s", "cwd": str(tmp_path), "tool_name": "Bash", "tool_input": {"command": "git diff"}},
    ):
        proc = _run_hook_process(payload, tmp_path)
        assert proc.returncode == 0
        assert proc.stderr == ""


def test_hook_process_fails_open_on_garbage_stdin(tmp_path: Path):
    proc = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input="not valid json",
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0

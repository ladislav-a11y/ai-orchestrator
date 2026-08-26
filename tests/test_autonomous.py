import json
import logging

from orchestrator.agents.base import Agent, AgentRunResult
from orchestrator.autonomous import (
    ABSOLUTE_MAX_ITERATIONS,
    AUDIT_MARKER,
    AutonomousStatus,
    DOD_BATCH_SIZE,
    NO_PROGRESS_LIMIT,
    _extract_json,
    parse_definition_of_done,
    run_autonomous_loop,
)
from orchestrator.config import Config, GitConfig

LOGGER = logging.getLogger("test")


class FakeAgent(Agent):
    name = "fake"

    def __init__(self, run_fn):
        self._run_fn = run_fn

    def is_available(self):
        return True, "fake agent always available"

    def run(self, request):
        return self._run_fn(request)


def _confirming_audit_or(executor_run_fn):
    """Wrap an executor run_fn so any independent-audit prompt (see
    AUDIT_MARKER) gets a clean "everything confirmed" response, and every
    other prompt goes to the given executor run_fn. Most tests only care
    about the executor's behaviour; the audit pass is a separate, real
    part of the contract (see AGENTS.md rule 8) so it must not be silently
    skipped by a fake that doesn't know about it."""

    def run_fn(request):
        if AUDIT_MARKER in request.prompt:
            return AgentRunResult(success=True, output_text='{"rejected_indices": [], "notes": "audit ok"}')
        return executor_run_fn(request)

    return run_fn


# -- Definition of Done parsing ----------------------------------------------


def test_parse_definition_of_done_checklist():
    items = parse_definition_of_done("- [x] hotovo\n- [ ] nesplneno\n* [ ] dalsi bod")
    assert [i.text for i in items] == ["hotovo", "nesplneno", "dalsi bod"]
    assert items[0].done is True
    assert items[1].done is False
    assert items[2].done is False


def test_parse_definition_of_done_plain_goal_is_single_item():
    items = parse_definition_of_done("Aplikace umi prihlasit uzivatele")
    assert len(items) == 1
    assert items[0].text == "Aplikace umi prihlasit uzivatele"
    assert items[0].done is False


def test_parse_definition_of_done_ignores_headings_and_prose():
    # Regression for run 9cd54b5bf219 (station-agent): a real spec file
    # mixes "# Heading" / "## Section" markdown with "- [ ] ..." checklist
    # items (see dod-station-agent-v1.md). Headings/prose must never become
    # DoD items - a heading can never be "done", so if it were parsed as an
    # item the checklist could never be fully satisfied.
    spec = (
        "# Definition of Done - Station Agent v1 (prvni plne funkcni verze)\n"
        "\n"
        "## GUI\n"
        "- [ ] Lokalni webove GUI dostupne na 127.0.0.1\n"
        "- [x] Server se vaze vyhradne na loopback\n"
        "\n"
        "## Testy\n"
        "Popisny text, ktery neni checklist a nesmi se stat polozkou.\n"
        "- [ ] Vsechny testy projdou bez chyby\n"
        "* [x] Bezpecnostni testy PTT/TX existuji\n"
    )
    items = parse_definition_of_done(spec)
    assert [i.text for i in items] == [
        "Lokalni webove GUI dostupne na 127.0.0.1",
        "Server se vaze vyhradne na loopback",
        "Vsechny testy projdou bez chyby",
        "Bezpecnostni testy PTT/TX existuji",
    ]
    assert [i.done for i in items] == [False, True, False, True]


# -- success: DoD splněná, testy prošly -> completed + commit -----------------


def test_run_autonomous_completed_with_commit(git_repo):
    dod = parse_definition_of_done("- [ ] Priprav feature.txt")

    def executor(request):
        # Zm?na vznik? a? B?HEM autonomous b?hu, tak?e ji auto-commit
        # sm? bezpe?n? zahrnout.
        (git_repo / "feature.txt").write_text("nova funkce\n", encoding="utf-8")
        return AgentRunResult(
            success=True,
            output_text='{"items": [{"index": 0, "done": true}], "notes": "hotovo"}',
        )

    cfg = Config()
    cfg.git = GitConfig(auto_commit=True)

    result = run_autonomous_loop(
        run_id="test-run",
        project_path=git_repo,
        goal="Priprav feature",
        dod_items=dod,
        config=cfg,
        agent=FakeAgent(_confirming_audit_or(executor)),
        logger=LOGGER,
        test_command=None,
        max_iterations=5,
        auto_commit_requested=True,
    )

    assert result.status == AutonomousStatus.COMPLETED
    assert result.committed is True
    assert result.commit_hash
    assert len(result.iterations) == 1
    assert result.iterations[0].audit_performed is True
    assert all(item.done for item in result.dod_items)


def test_run_autonomous_does_not_commit_preexisting_dirty_tree(git_repo):
    (git_repo / "preexisting.txt").write_text("rozpracovana prace\n", encoding="utf-8")
    dod = parse_definition_of_done("- [ ] Over stav")

    def executor(request):
        return AgentRunResult(
            success=True,
            output_text='{"items": [{"index": 0, "done": true}], "notes": "hotovo"}',
        )

    cfg = Config()
    cfg.git = GitConfig(auto_commit=True)

    result = run_autonomous_loop(
        run_id="dirty-tree-test",
        project_path=git_repo,
        goal="Over stav",
        dod_items=dod,
        config=cfg,
        agent=FakeAgent(_confirming_audit_or(executor)),
        logger=LOGGER,
        test_command=None,
        max_iterations=2,
        auto_commit_requested=True,
    )

    assert result.status == AutonomousStatus.COMPLETED
    assert result.committed is False
    assert result.commit_hash is None
    assert (git_repo / "preexisting.txt").exists()


def test_run_autonomous_accumulates_breaker_saved_attempts_across_calls(git_repo):
    # The PreToolUse circuit breaker (orchestrator/hooks/test_command_guard.py)
    # reports how many repeated test-invocation attempts it short-circuited
    # via AgentRunResult.breaker_saved_attempts - run_autonomous_loop must
    # sum this across every agent.run() call (executor + audit here) onto
    # the final AutonomousResult, same as it does for DoD/test tracking.
    (git_repo / "feature.txt").write_text("nova funkce\n", encoding="utf-8")
    dod = parse_definition_of_done("- [ ] Priprav feature.txt")

    def run_fn(request):
        if AUDIT_MARKER in request.prompt:
            return AgentRunResult(
                success=True,
                output_text='{"rejected_indices": [], "notes": "audit ok"}',
                breaker_saved_attempts=5,
            )
        return AgentRunResult(
            success=True,
            output_text='{"items": [{"index": 0, "done": true}], "notes": "hotovo"}',
            breaker_saved_attempts=14,
        )

    cfg = Config()  # git.auto_commit defaults to False

    result = run_autonomous_loop(
        run_id="test-run",
        project_path=git_repo,
        goal="Priprav feature",
        dod_items=dod,
        config=cfg,
        agent=FakeAgent(run_fn),
        logger=LOGGER,
        test_command=None,
        max_iterations=5,
        auto_commit_requested=False,
    )

    assert result.status == AutonomousStatus.COMPLETED
    assert result.breaker_saved_attempts == 19


def test_run_autonomous_completed_without_commit_when_run_did_not_request_it(git_repo):
    # The run itself did not request a commit (auto_commit_requested=False,
    # e.g. no --commit flag / auto_commit param passed) and the global config
    # default is also off - the ban on unsolicited commits must hold.
    (git_repo / "feature.txt").write_text("nova funkce\n", encoding="utf-8")
    dod = parse_definition_of_done("- [ ] Priprav feature.txt")

    def executor(request):
        return AgentRunResult(
            success=True,
            output_text='{"items": [{"index": 0, "done": true}]}',
        )

    cfg = Config()  # git.auto_commit defaults to False

    result = run_autonomous_loop(
        run_id="test-run",
        project_path=git_repo,
        goal="Priprav feature",
        dod_items=dod,
        config=cfg,
        agent=FakeAgent(_confirming_audit_or(executor)),
        logger=LOGGER,
        test_command=None,
        max_iterations=5,
        auto_commit_requested=False,
    )

    assert result.status == AutonomousStatus.COMPLETED
    assert result.committed is False
    assert result.commit_hash is None


def test_run_autonomous_commits_when_run_explicitly_requests_it_even_if_config_default_is_off(git_repo):
    # Regression: auto_commit_requested (an explicit per-run approval, e.g.
    # via the CLI --commit flag or the API auto_commit param) used to be
    # ANDed with config.git.auto_commit in _commit_if_ready, so an explicitly
    # approved run could never actually commit unless the global config
    # default was ALSO flipped on (which would blanket-approve every future
    # run, not just this explicitly approved one). An explicit per-run
    # approval must be sufficient on its own once the Definition of Done is
    # met, tests pass, and the audit does not reject anything.
    #
    # The change must happen INSIDE the executor callback, not before
    # run_autonomous_loop() is called - preexisting_dirty is computed from
    # the actual working tree at the very start of the loop (see
    # run_autonomous_loop), so writing the file any earlier would make the
    # tree dirty before the run even starts and _commit_if_ready would
    # (correctly) refuse to commit, defeating the point of this test.
    dod = parse_definition_of_done("- [ ] Priprav feature.txt")

    def executor(request):
        (git_repo / "feature.txt").write_text("nova funkce\n", encoding="utf-8")
        return AgentRunResult(
            success=True,
            output_text='{"items": [{"index": 0, "done": true}]}',
        )

    cfg = Config()  # git.auto_commit defaults to False

    result = run_autonomous_loop(
        run_id="test-run",
        project_path=git_repo,
        goal="Priprav feature",
        dod_items=dod,
        config=cfg,
        agent=FakeAgent(_confirming_audit_or(executor)),
        logger=LOGGER,
        test_command=None,
        max_iterations=5,
        auto_commit_requested=True,
    )

    assert result.status == AutonomousStatus.COMPLETED
    assert result.committed is True
    assert result.commit_hash


# -- failed tests: never commits, keeps trying until max_iterations ----------


def test_run_autonomous_never_commits_when_tests_fail(tmp_path):
    dod = parse_definition_of_done("- [ ] Neco co agent tvrdi ze je hotove")
    test_cmd = 'python -c "import sys; sys.exit(1)"'

    def run_fn(request):
        return AgentRunResult(
            success=True,
            output_text='{"items": [{"index": 0, "done": true}], "notes": "tvrdim ze hotovo"}',
        )

    cfg = Config()
    cfg.git = GitConfig(auto_commit=True)

    result = run_autonomous_loop(
        run_id="test-run",
        project_path=tmp_path,
        goal="cil",
        dod_items=dod,
        config=cfg,
        agent=FakeAgent(run_fn),
        logger=LOGGER,
        test_command=test_cmd,
        max_iterations=2,
        auto_commit_requested=True,
    )

    assert result.status == AutonomousStatus.MAX_ITERATIONS
    assert result.committed is False
    assert result.commit_hash is None
    assert len(result.iterations) == 2
    assert all(it.tests_passed is False for it in result.iterations)


# -- max iterations: never fully completes, stops at the requested cap ------


def test_run_autonomous_stops_at_max_iterations(tmp_path):
    # A well-behaved executor that always covers every index it was ever
    # asked about (see DOD_BATCH_SIZE contract in _apply_dod_updates) -
    # marks exactly one additional item done per call, one at a time.
    dod = parse_definition_of_done("\n".join(f"- [ ] bod {i}" for i in range(4)))
    calls = {"n": 0}

    def executor(request):
        calls["n"] += 1
        done_count = calls["n"]
        items = [{"index": idx, "done": idx < done_count} for idx in range(4)]
        return AgentRunResult(success=True, output_text=json.dumps({"items": items, "notes": f"iterace {done_count}"}))

    cfg = Config()

    result = run_autonomous_loop(
        run_id="test-run",
        project_path=tmp_path,
        goal="udelej 4 veci",
        dod_items=dod,
        config=cfg,
        agent=FakeAgent(_confirming_audit_or(executor)),
        logger=LOGGER,
        test_command=None,
        max_iterations=3,
        auto_commit_requested=False,
    )

    assert result.status == AutonomousStatus.MAX_ITERATIONS
    assert len(result.iterations) == 3
    assert sum(1 for item in result.dod_items if item.done) == 3
    assert result.committed is False


def test_run_autonomous_hard_cap_on_max_iterations(tmp_path):
    # even a huge --max-iterations can never exceed ABSOLUTE_MAX_ITERATIONS
    total_items = ABSOLUTE_MAX_ITERATIONS + 5
    dod = parse_definition_of_done("\n".join(f"- [ ] bod {i}" for i in range(total_items)))
    calls = {"n": 0}

    def executor(request):
        calls["n"] += 1
        done_count = calls["n"]
        items = [{"index": idx, "done": idx < done_count} for idx in range(total_items)]
        return AgentRunResult(success=True, output_text=json.dumps({"items": items}))

    cfg = Config()

    result = run_autonomous_loop(
        run_id="test-run",
        project_path=tmp_path,
        goal="udelej hodne veci",
        dod_items=dod,
        config=cfg,
        agent=FakeAgent(_confirming_audit_or(executor)),
        logger=LOGGER,
        test_command=None,
        max_iterations=100_000,
        auto_commit_requested=False,
    )

    assert result.status == AutonomousStatus.MAX_ITERATIONS
    assert len(result.iterations) == ABSOLUTE_MAX_ITERATIONS


# -- no progress: stops early as blocked, before max_iterations --------------


def test_run_autonomous_detects_no_progress_and_blocks(tmp_path):
    dod = parse_definition_of_done("- [ ] nikdy se nesplni")

    def run_fn(request):
        return AgentRunResult(
            success=True,
            output_text='{"items": [{"index": 0, "done": false}], "notes": "porad zaseklo"}',
        )

    cfg = Config()

    result = run_autonomous_loop(
        run_id="test-run",
        project_path=tmp_path,
        goal="nemozny cil",
        dod_items=dod,
        config=cfg,
        agent=FakeAgent(run_fn),
        logger=LOGGER,
        test_command=None,
        max_iterations=10,
        auto_commit_requested=False,
    )

    assert result.status == AutonomousStatus.BLOCKED
    assert len(result.iterations) == NO_PROGRESS_LIMIT
    assert len(result.iterations) < 10


# -- tests_passed must always be a real bool when a test command runs -------


def test_run_autonomous_tests_passed_is_always_bool(tmp_path):
    def run_fn(request):
        return AgentRunResult(
            success=True,
            output_text='{"items": [{"index": 0, "done": false}], "notes": "zkousim"}',
        )

    cfg = Config()
    for test_cmd, expected in [
        ('python -c "import sys; sys.exit(0)"', True),
        ('python -c "import sys; sys.exit(1)"', False),
    ]:
        result = run_autonomous_loop(
            run_id="test-run",
            project_path=tmp_path,
            goal="cil",
            dod_items=parse_definition_of_done("- [ ] neco co se nikdy neoznaci"),
            config=cfg,
            agent=FakeAgent(run_fn),
            logger=LOGGER,
            test_command=test_cmd,
            max_iterations=1,
            auto_commit_requested=False,
        )
        assert len(result.iterations) == 1
        assert isinstance(result.iterations[0].tests_passed, bool)
        assert result.iterations[0].tests_passed is expected


# -- DoD evaluation: monotonic merge, protocol errors don't fake no-progress -


def test_run_autonomous_preserves_verified_done_items_across_iterations(tmp_path):
    """Requirement: the orchestrator must not blindly trust the agent's JSON
    claim each iteration - once an item is verified done, a later (possibly
    sloppier) agent response claiming it is no longer done must not un-mark
    it."""
    dod = parse_definition_of_done("- [ ] bod A\n- [ ] bod B")
    calls = {"n": 0}

    def run_fn(request):
        calls["n"] += 1
        if calls["n"] == 1:
            payload = {"items": [{"index": 0, "done": True}, {"index": 1, "done": False}], "notes": "A hotovo"}
        else:
            # Second response incorrectly claims item 0 regressed to not-done.
            payload = {"items": [{"index": 0, "done": False}, {"index": 1, "done": False}], "notes": "pokus 2"}
        return AgentRunResult(success=True, output_text=json.dumps(payload))

    cfg = Config()
    result = run_autonomous_loop(
        run_id="test-run",
        project_path=tmp_path,
        goal="cil",
        dod_items=dod,
        config=cfg,
        agent=FakeAgent(run_fn),
        logger=LOGGER,
        test_command=None,
        max_iterations=2,
        auto_commit_requested=False,
    )

    assert result.dod_items[0].done is True
    assert result.dod_items[1].done is False


def test_run_autonomous_malformed_response_does_not_cause_false_blocked(tmp_path):
    """Regression for run 9cd54b5bf219: an agent whose response cannot be
    parsed as the expected JSON contract must be recorded as a protocol
    error and given another chance, not silently treated as a repeated
    'no progress' state that trips the blocked detector."""
    dod = parse_definition_of_done("- [ ] bod, ktery se nikdy neoznaci")

    def run_fn(request):
        return AgentRunResult(success=True, output_text="tohle vubec neni JSON")

    cfg = Config()
    result = run_autonomous_loop(
        run_id="test-run",
        project_path=tmp_path,
        goal="cil",
        dod_items=dod,
        config=cfg,
        agent=FakeAgent(run_fn),
        logger=LOGGER,
        test_command=None,
        max_iterations=3,
        auto_commit_requested=False,
    )

    assert result.status == AutonomousStatus.MAX_ITERATIONS
    assert len(result.iterations) == 3
    assert all(it.protocol_error for it in result.iterations)


def test_run_autonomous_incomplete_json_is_protocol_error_not_no_progress(tmp_path):
    dod = parse_definition_of_done("- [ ] bod A\n- [ ] bod B")

    def run_fn(request):
        # Only reports one of the two required items every time.
        return AgentRunResult(
            success=True,
            output_text='{"items": [{"index": 0, "done": false}], "notes": "neuplne"}',
        )

    cfg = Config()
    result = run_autonomous_loop(
        run_id="test-run",
        project_path=tmp_path,
        goal="cil",
        dod_items=dod,
        config=cfg,
        agent=FakeAgent(run_fn),
        logger=LOGGER,
        test_command=None,
        max_iterations=3,
        auto_commit_requested=False,
    )

    assert result.status == AutonomousStatus.MAX_ITERATIONS
    assert all(it.protocol_error for it in result.iterations)


def test_protocol_error_does_not_apply_partial_dod_updates(tmp_path):
    """A protocol-invalid response must not mutate any DoD item."""
    dod = parse_definition_of_done("- [ ] bod A\n- [ ] bod B")

    def run_fn(request):
        return AgentRunResult(
            success=True,
            output_text=json.dumps({
                "items": [
                    {"index": 0, "done": True},
                    {"index": 1, "done": True},
                    {"index": 2, "done": True},
                ],
                "notes": "obsahuje neplatny index",
            }),
        )

    result = run_autonomous_loop(
        run_id="test-run",
        project_path=tmp_path,
        goal="cil",
        dod_items=dod,
        config=Config(),
        agent=FakeAgent(run_fn),
        logger=LOGGER,
        test_command=None,
        max_iterations=1,
        auto_commit_requested=False,
    )

    assert result.status == AutonomousStatus.MAX_ITERATIONS
    assert result.iterations[0].protocol_error is True
    assert all(item.done is False for item in result.dod_items)


# -- test command auto-detection: autonomous mode must not silently skip ----
# -- running tests just because none was explicitly configured -------------


def test_run_autonomous_autodetects_pytest_when_no_test_command_configured(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()

    def run_fn(request):
        return AgentRunResult(
            success=True,
            output_text='{"items": [{"index": 0, "done": false}], "notes": "zkousim"}',
        )

    cfg = Config()
    result = run_autonomous_loop(
        run_id="test-run",
        project_path=tmp_path,
        goal="cil",
        dod_items=parse_definition_of_done("- [ ] neco co se nikdy neoznaci"),
        config=cfg,
        agent=FakeAgent(run_fn),
        logger=LOGGER,
        test_command=None,  # not configured - must be auto-detected, not skipped
        max_iterations=1,
        auto_commit_requested=False,
    )

    assert len(result.iterations) == 1
    assert isinstance(result.iterations[0].tests_passed, bool)
    assert result.iterations[0].test_output is not None


def test_run_autonomous_does_not_autodetect_for_non_python_project(tmp_path):
    def run_fn(request):
        return AgentRunResult(
            success=True,
            output_text='{"items": [{"index": 0, "done": true}], "notes": "hotovo"}',
        )

    cfg = Config()
    result = run_autonomous_loop(
        run_id="test-run",
        project_path=tmp_path,  # no pyproject.toml, no tests/ dir
        goal="cil",
        dod_items=parse_definition_of_done("- [ ] neco"),
        config=cfg,
        agent=FakeAgent(run_fn),
        logger=LOGGER,
        test_command=None,
        max_iterations=1,
        auto_commit_requested=False,
    )

    assert len(result.iterations) == 1
    assert result.iterations[0].tests_passed is None


# -- agent error: stops immediately, does not keep retrying blindly ----------


def test_run_autonomous_agent_error_stops_immediately(tmp_path):
    dod = parse_definition_of_done("- [ ] neco")

    def run_fn(request):
        return AgentRunResult(success=False, output_text="", error="boom")

    cfg = Config()

    result = run_autonomous_loop(
        run_id="test-run",
        project_path=tmp_path,
        goal="cil",
        dod_items=dod,
        config=cfg,
        agent=FakeAgent(run_fn),
        logger=LOGGER,
        test_command=None,
        max_iterations=5,
        auto_commit_requested=False,
    )

    assert result.status == AutonomousStatus.ERROR
    assert result.error == "boom"
    assert len(result.iterations) == 1


# -- _extract_json: robust against trailing/leading text and fences ---------


def test_extract_json_recovers_from_trailing_permission_denial_note():
    """Regression for run 11b4aaae08b4: the Claude Code CLI wrapper used to
    append a plain-text note *after* the agent's own valid JSON payload
    whenever a tool call was permission-denied, e.g.:
        '{"items": [...], "notes": "..."}\\n\\n[orchestrator] Claude odmítl 15 akci(í) kvůli oprávněním.'
    A naive `json.loads` on the whole string fails on the trailing text,
    which is exactly why 8 iterations in that run were misreported as
    protocol_error despite tests_passed=True and a perfectly valid agent
    response. `_extract_json` must recover the JSON object regardless."""
    text = (
        '{"items": [{"index": 0, "done": true}, {"index": 1, "done": false}], "notes": "hotovo"}'
        "\n\n[orchestrator] Claude odmítl 15 akci(í) kvůli oprávněním."
    )
    parsed = _extract_json(text)
    assert parsed is not None
    assert parsed["items"] == [{"index": 0, "done": True}, {"index": 1, "done": False}]
    assert parsed["notes"] == "hotovo"


def test_extract_json_recovers_from_leading_prose_and_fence():
    text = (
        "Shrnutí práce: opravil jsem X a Y.\n\n"
        "```json\n"
        '{"items": [{"index": 0, "done": true}], "notes": "ok"}\n'
        "```\n"
    )
    parsed = _extract_json(text)
    assert parsed == {"items": [{"index": 0, "done": True}], "notes": "ok"}


def test_extract_json_returns_none_for_genuinely_broken_json():
    assert _extract_json("tohle vubec neni JSON") is None
    assert _extract_json('{"items": [{"index": 0, "done": true premature cutoff') is None
    assert _extract_json("") is None


# -- batching: only a small batch of unmet items is requested per iteration -


def test_run_autonomous_requests_only_a_small_batch_not_the_whole_dod(tmp_path):
    """Regression for run 11b4aaae08b4 (66 DoD items): every iteration used
    to list and require a JSON entry for *all* DoD items, however many there
    were. That is both expensive (huge prompts every iteration) and fragile
    (one huge response is much more likely to get truncated/garbled). Each
    iteration must only ask about a small batch."""
    total_items = DOD_BATCH_SIZE * 3
    dod = parse_definition_of_done("\n".join(f"- [ ] bod {i}" for i in range(total_items)))

    def executor(request):
        return AgentRunResult(success=True, output_text='{"items": [], "notes": "zadny pokrok"}')

    cfg = Config()
    result = run_autonomous_loop(
        run_id="test-run",
        project_path=tmp_path,
        goal="hodne bodu",
        dod_items=dod,
        config=cfg,
        agent=FakeAgent(_confirming_audit_or(executor)),
        logger=LOGGER,
        test_command=None,
        max_iterations=1,
        auto_commit_requested=False,
    )

    assert len(result.iterations[0].requested_indices) == DOD_BATCH_SIZE
    assert result.iterations[0].requested_indices == list(range(DOD_BATCH_SIZE))
    # the prompt must not mention every single one of the total_items bodies
    assert result.iterations[0].prompt.count("bod ") <= DOD_BATCH_SIZE + 2


# -- cheap repair: malformed response gets one reprompt, not a fresh iteration


def test_run_autonomous_recovers_from_malformed_response_via_cheap_repair(tmp_path):
    """Regression for run 11b4aaae08b4: tests_passed=True on every iteration
    but the agent's raw response was unparsable, so every iteration was
    marked protocol_error and none of the already-verified DoD progress
    could ever lead to completion. The loop must (a) never reset a
    previously verified DoD item because of a later malformed response, and
    (b) recover via a single cheap repair reprompt rather than repeating a
    full, expensive implementation iteration."""
    dod = parse_definition_of_done("- [ ] bod A\n- [ ] bod B")
    calls = {"n": 0}

    def executor(request):
        calls["n"] += 1
        n = calls["n"]
        if n == 1:
            # iteration 1: clean valid response, marks bod A done
            return AgentRunResult(
                success=True,
                output_text=json.dumps(
                    {"items": [{"index": 0, "done": True}, {"index": 1, "done": False}], "notes": "A hotovo"}
                ),
            )
        if n == 2:
            # iteration 2, main call: genuinely broken/truncated JSON
            return AgentRunResult(success=True, output_text='{"items": [{"index": 1, "done": true premat')
        # iteration 2, cheap repair reprompt: valid JSON this time
        return AgentRunResult(
            success=True,
            output_text=json.dumps({"items": [{"index": 1, "done": True}], "notes": "B hotovo po repair"}),
        )

    cfg = Config()
    result = run_autonomous_loop(
        run_id="test-run",
        project_path=tmp_path,
        goal="cil",
        dod_items=dod,
        config=cfg,
        agent=FakeAgent(_confirming_audit_or(executor)),
        logger=LOGGER,
        test_command='python -c "import sys; sys.exit(0)"',  # always tests_passed True
        max_iterations=4,
        auto_commit_requested=False,
    )

    # bod A (verified in iteration 1) must never be reset, even though
    # iteration 2 started with a malformed response.
    assert result.dod_items[0].done is True
    # bod B was recovered via the cheap repair reprompt in iteration 2.
    assert result.dod_items[1].done is True
    # every iteration's tests were independently verified by the orchestrator
    assert all(it.tests_passed is True for it in result.iterations)
    # iteration 2's malformed main response triggered exactly one repair
    # attempt, which succeeded - it must not be reported as a protocol error
    # (that would have falsely wiped out the no-progress tracking signal).
    assert len(result.iterations) == 2
    iter2 = result.iterations[1]
    assert iter2.repair_attempted is True
    assert iter2.repair_succeeded is True
    assert iter2.protocol_error is False
    # the run completed via the independent audit confirming the claim, not
    # by trusting the executor's self-report alone
    assert result.status == AutonomousStatus.COMPLETED
    assert iter2.audit_performed is True
    # cheap: only one extra repair call was needed, not a fresh full
    # implementation iteration (3 executor-side calls total: main x2 + 1 repair)
    assert calls["n"] == 3


# -- independent audit: rejects a false "done" claim before committing ------


def test_run_autonomous_audit_reopens_falsely_claimed_done_item(tmp_path):
    """The executor's self-report is never sufficient on its own (Manager/
    Executor/Auditor split, see AGENTS.md rule 8): if the independent audit
    pass finds a claimed-done item is not actually done, that item must be
    reopened (not committed), and the run must keep going instead of
    silently trusting the executor."""
    dod = parse_definition_of_done("- [ ] bod A")
    audit_calls = {"n": 0}

    def run_fn(request):
        if AUDIT_MARKER in request.prompt:
            audit_calls["n"] += 1
            if audit_calls["n"] == 1:
                return AgentRunResult(
                    success=True,
                    output_text='{"rejected_indices": [0], "notes": "bod A ve skutecnosti chybi"}',
                )
            return AgentRunResult(success=True, output_text='{"rejected_indices": [], "notes": "ted uz ok"}')
        return AgentRunResult(
            success=True,
            output_text='{"items": [{"index": 0, "done": true}], "notes": "hotovo"}',
        )

    cfg = Config()
    result = run_autonomous_loop(
        run_id="test-run",
        project_path=tmp_path,
        goal="cil",
        dod_items=dod,
        config=cfg,
        agent=FakeAgent(run_fn),
        logger=LOGGER,
        test_command=None,
        max_iterations=3,
        auto_commit_requested=False,
    )

    assert result.status == AutonomousStatus.COMPLETED
    assert result.iterations[0].audit_rejected_indices == [0]
    assert result.iterations[0].audit_performed is True
    # not completed on the first iteration - the audit reopened it
    assert len(result.iterations) == 2
    assert result.dod_items[0].done is True


def test_run_autonomous_waits_when_provider_is_limited(tmp_path):
    dod = parse_definition_of_done("- [ ] bod A")

    def run_fn(request):
        return AgentRunResult(
            success=False,
            output_text="",
            error="All providers LIMITED",
            limited=True,
            retry_after_seconds=600,
        )

    cfg = Config()

    result = run_autonomous_loop(
        run_id="wait-test",
        project_path=tmp_path,
        goal="cekani na providera",
        dod_items=dod,
        config=cfg,
        agent=FakeAgent(run_fn),
        logger=LOGGER,
        test_command=None,
        max_iterations=3,
        auto_commit_requested=False,
    )

    assert result.status == AutonomousStatus.WAITING_FOR_PROVIDER
    assert result.retry_after_seconds == 600
    assert result.error == "All providers LIMITED"
    assert result.dod_items[0].done is False

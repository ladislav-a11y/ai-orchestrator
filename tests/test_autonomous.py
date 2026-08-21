import json
import logging

from orchestrator.agents.base import Agent, AgentRunResult
from orchestrator.autonomous import (
    ABSOLUTE_MAX_ITERATIONS,
    AutonomousStatus,
    NO_PROGRESS_LIMIT,
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


# -- success: DoD splněná, testy prošly -> completed + commit -----------------


def test_run_autonomous_completed_with_commit(git_repo):
    (git_repo / "feature.txt").write_text("nova funkce\n", encoding="utf-8")
    dod = parse_definition_of_done("- [ ] Priprav feature.txt")

    def run_fn(request):
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
        agent=FakeAgent(run_fn),
        logger=LOGGER,
        test_command=None,
        max_iterations=5,
        auto_commit_requested=True,
    )

    assert result.status == AutonomousStatus.COMPLETED
    assert result.committed is True
    assert result.commit_hash
    assert len(result.iterations) == 1
    assert all(item.done for item in result.dod_items)


def test_run_autonomous_completed_without_commit_when_auto_commit_disabled(git_repo):
    (git_repo / "feature.txt").write_text("nova funkce\n", encoding="utf-8")
    dod = parse_definition_of_done("- [ ] Priprav feature.txt")

    def run_fn(request):
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
        agent=FakeAgent(run_fn),
        logger=LOGGER,
        test_command=None,
        max_iterations=5,
        auto_commit_requested=True,
    )

    assert result.status == AutonomousStatus.COMPLETED
    assert result.committed is False
    assert result.commit_hash is None


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
    dod = parse_definition_of_done("\n".join(f"- [ ] bod {i}" for i in range(4)))
    calls = {"n": 0}

    def run_fn(request):
        idx = calls["n"]
        calls["n"] += 1
        return AgentRunResult(
            success=True,
            output_text=json.dumps({"items": [{"index": idx, "done": True}], "notes": f"iterace {idx}"}),
        )

    cfg = Config()

    result = run_autonomous_loop(
        run_id="test-run",
        project_path=tmp_path,
        goal="udelej 4 veci",
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
    assert sum(1 for item in result.dod_items if item.done) == 3
    assert result.committed is False


def test_run_autonomous_hard_cap_on_max_iterations(tmp_path):
    # even a huge --max-iterations can never exceed ABSOLUTE_MAX_ITERATIONS
    total_items = ABSOLUTE_MAX_ITERATIONS + 5
    dod = parse_definition_of_done("\n".join(f"- [ ] bod {i}" for i in range(total_items)))
    calls = {"n": 0}

    def run_fn(request):
        idx = calls["n"]
        calls["n"] += 1
        return AgentRunResult(
            success=True,
            output_text=json.dumps({"items": [{"index": idx, "done": True}]}),
        )

    cfg = Config()

    result = run_autonomous_loop(
        run_id="test-run",
        project_path=tmp_path,
        goal="udelej hodne veci",
        dod_items=dod,
        config=cfg,
        agent=FakeAgent(run_fn),
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

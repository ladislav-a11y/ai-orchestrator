import json
from pathlib import Path

from orchestrator import service as service_module
from orchestrator.agents.base import Agent, AgentRunResult
from orchestrator.autonomous import AUDIT_MARKER, AutonomousStatus
from orchestrator.config import ApiConfig, Config, GitConfig, PathsConfig, ProjectEntry, TestingConfig
from orchestrator.service import OrchestratorService


class FakeAgent(Agent):
    name = "fake"

    def is_available(self):
        return True, "fake agent always available"

    def run(self, request):
        # The autonomous loop's independent audit pass (see AGENTS.md rule
        # 8/10) sends a separate, distinctly-marked prompt before trusting
        # this agent's own "done" claim - it must be answered too, or the
        # loop never completes and instead exhausts max_iterations.
        if AUDIT_MARKER in request.prompt:
            return AgentRunResult(success=True, output_text='{"rejected_indices": [], "notes": "audit ok"}')
        return AgentRunResult(
            success=True,
            output_text='{"items": [{"index": 0, "done": true}], "notes": "hotovo"}',
        )


def make_cfg(tmp_path: Path) -> Config:
    return Config(
        projects={
            "station-agent": ProjectEntry(name="station-agent", path=str(tmp_path / "workspace" / "station-agent")),
        },
        git=GitConfig(auto_commit=False),
        testing=TestingConfig(),
        api=ApiConfig(),
        paths=PathsConfig(
            inbox=str(tmp_path / "inbox"),
            outbox=str(tmp_path / "outbox"),
            logs=str(tmp_path / "logs"),
            data=str(tmp_path / "data"),
        ),
        workspace_root=str(tmp_path / "workspace"),
    )


def test_submit_creates_missing_registered_project_dir(tmp_path):
    cfg = make_cfg(tmp_path)
    project_dir = tmp_path / "workspace" / "station-agent"
    assert not project_dir.exists()

    service = OrchestratorService(cfg)
    task = service.submit(project_ref="station-agent", prompt="zaloz projekt")

    assert project_dir.exists() and project_dir.is_dir()
    assert task.project_path == str(project_dir)


def test_submit_leaves_existing_project_dir_untouched(tmp_path):
    cfg = make_cfg(tmp_path)
    project_dir = tmp_path / "workspace" / "station-agent"
    project_dir.mkdir(parents=True)
    marker = project_dir / "keep.txt"
    marker.write_text("existing content", encoding="utf-8")

    service = OrchestratorService(cfg)
    service.submit(project_ref="station-agent", prompt="pokracuj")

    assert marker.read_text(encoding="utf-8") == "existing content"


def test_submit_writes_safe_claude_settings_for_new_project(tmp_path):
    cfg = make_cfg(tmp_path)
    project_dir = tmp_path / "workspace" / "station-agent"

    service = OrchestratorService(cfg)
    service.submit(project_ref="station-agent", prompt="zaloz projekt")

    settings_path = project_dir / ".claude" / "settings.local.json"
    assert settings_path.exists()
    content = json.loads(settings_path.read_text(encoding="utf-8"))
    assert "Bash(git push:*)" in content["permissions"]["deny"]
    assert "Bash(git status:*)" in content["permissions"]["allow"]


def test_submit_does_not_overwrite_existing_claude_settings(tmp_path):
    cfg = make_cfg(tmp_path)
    project_dir = tmp_path / "workspace" / "station-agent"
    claude_dir = project_dir / ".claude"
    claude_dir.mkdir(parents=True)
    custom_settings = claude_dir / "settings.local.json"
    custom_settings.write_text('{"permissions": {"allow": ["Bash(npm test:*)"]}}', encoding="utf-8")

    service = OrchestratorService(cfg)
    service.submit(project_ref="station-agent", prompt="pokracuj")

    content = json.loads(custom_settings.read_text(encoding="utf-8"))
    assert content == {"permissions": {"allow": ["Bash(npm test:*)"]}}


def test_run_task_permission_denial_details_reach_outbox(tmp_path, monkeypatch):
    denials = [{"tool_name": "Bash", "tool_input": {"command": "git push --force"}}]

    class DenyingAgent(Agent):
        name = "fake"

        def is_available(self):
            return True, "fake agent always available"

        def run(self, request):
            return AgentRunResult(
                success=True,
                output_text="hotovo",
                permission_denials=len(denials),
                permission_denial_details=denials,
            )

    monkeypatch.setattr(service_module, "build_agent", lambda name, config: DenyingAgent())
    cfg = make_cfg(tmp_path)

    service = OrchestratorService(cfg)
    task = service.submit(project_ref="station-agent", prompt="zkus neco zakazaneho")
    result_task = service.run_sync(task)

    assert result_task.permission_denials == len(denials)
    assert result_task.permission_denial_details == denials

    outbox_path = cfg.outbox_dir / f"{task.id}.json"
    assert outbox_path.exists()
    payload = json.loads(outbox_path.read_text(encoding="utf-8"))
    assert payload["permission_denials"] == len(denials)
    assert payload["permission_denial_details"] == denials


def test_run_autonomous_completed_writes_log_and_outbox(tmp_path, monkeypatch):
    monkeypatch.setattr(service_module, "build_agent", lambda name, config: FakeAgent())
    cfg = make_cfg(tmp_path)
    cfg.git = GitConfig(auto_commit=False)

    service = OrchestratorService(cfg)
    run_id, result = service.run_autonomous(
        project_ref="station-agent",
        goal="Priprav zakladni projekt",
        spec_text="- [ ] Zaloz projekt",
        max_iterations=3,
    )

    assert result.status == AutonomousStatus.COMPLETED
    assert all(item.done for item in result.dod_items)

    log_path = cfg.logs_dir / "autonomous" / f"{run_id}.log"
    assert log_path.exists()
    assert "Priprav zakladni projekt" in log_path.read_text(encoding="utf-8")

    outbox_path = cfg.outbox_dir / f"autonomous-{run_id}.json"
    assert outbox_path.exists()
    payload = json.loads(outbox_path.read_text(encoding="utf-8"))
    assert payload["status"] == "completed"
    assert payload["run_id"] == run_id
    assert payload["retry_after_seconds"] is None
    assert payload["done"] is True
    assert payload["checkpoint"]["run_id"] == run_id
    assert payload["checkpoint"]["completed_dod_indices"] == [0]
    assert payload["next_step"] == ""


def test_resumed_autonomous_run_reuses_original_run_id(tmp_path, monkeypatch):
    """A run that hits WAITING_FOR_PROVIDER and is later resumed must finish
    under the SAME run_id it started with (e.g. the Trello card id AI
    Project Manager passed via --run-id). Otherwise the completed result
    lands in a different outbox/autonomous-<run_id>.json file than the one
    the external caller is watching, and it never sees the outcome.
    """

    class OnceLimitedAgent(Agent):
        name = "fake"

        def __init__(self):
            self.calls = 0

        def is_available(self):
            return True, "fake agent always available"

        def run(self, request):
            if AUDIT_MARKER in request.prompt:
                return AgentRunResult(success=True, output_text='{"rejected_indices": [], "notes": "audit ok"}')
            self.calls += 1
            if self.calls == 1:
                return AgentRunResult(
                    success=False,
                    output_text="",
                    error="rate limit reached",
                    limited=True,
                    retry_after_seconds=0.0,
                )
            return AgentRunResult(
                success=True,
                output_text='{"items": [{"index": 0, "done": true}], "notes": "hotovo"}',
            )

    agent = OnceLimitedAgent()
    monkeypatch.setattr(service_module, "build_agent", lambda name, config: agent)
    cfg = make_cfg(tmp_path)
    service = OrchestratorService(cfg)
    try:
        # agent_name must be explicit here: leaving it unset routes through
        # build_failover_agent(), which builds one provider slot per name in
        # the fallback order ("claude-code", "antigravity", "codex") - since
        # build_agent is monkeypatched to hand back this SAME stateful fake
        # for every name, FailoverAgent.run() would call it once per slot
        # inside a single outer call (limited on the 1st call, success on
        # the 2nd), reaching COMPLETED before ever surfacing
        # WAITING_FOR_PROVIDER. An explicit agent_name calls build_agent()
        # directly instead, one call per outer run_autonomous() invocation.
        run_id, result = service.run_autonomous(
            project_ref="station-agent",
            goal="Priprav zakladni projekt",
            spec_text="- [ ] Zaloz projekt",
            agent_name="fake",
            max_iterations=3,
            run_id="trello-card-77",
        )

        assert result.status == AutonomousStatus.WAITING_FOR_PROVIDER
        assert run_id == "trello-card-77"

        waiting_outbox = cfg.outbox_dir / "autonomous-trello-card-77.json"
        assert waiting_outbox.exists()
        assert json.loads(waiting_outbox.read_text(encoding="utf-8"))["status"] == "waiting_for_provider"

        waiting_task = service.queue.find_active_autonomous("station-agent", "- [ ] Zaloz projekt")
        assert waiting_task is not None
        assert waiting_task.run_id == "trello-card-77"
        # A 0-second retry_after_seconds ("retry immediately") is falsy in
        # Python - must still produce a real retry_at, or the waiting worker
        # would never consider this task due and it would wait forever.
        assert waiting_task.retry_at is not None

        # Mirror exactly what the waiting worker does: atomically claim the
        # due task (flips it to RUNNING) and hand that claimed task to resume.
        claimed_task = service.queue.claim_next_due_waiting()
        assert claimed_task is not None
        assert claimed_task.id == waiting_task.id

        service._resume_autonomous_task(claimed_task)

        payload = json.loads(waiting_outbox.read_text(encoding="utf-8"))
        assert payload["run_id"] == "trello-card-77"
        assert payload["status"] == "completed"
        assert payload["done"] is True
        assert not (cfg.outbox_dir / f"autonomous-{waiting_task.id}.json").exists()

        resumed_task = service.queue.get(waiting_task.id)
        assert resumed_task.status == service_module.TaskStatus.DONE
    finally:
        service.shutdown()


def test_run_autonomous_requires_goal_or_spec(tmp_path):
    cfg = make_cfg(tmp_path)
    service = OrchestratorService(cfg)
    try:
        service.run_autonomous(project_ref="station-agent")
        assert False, "expected ValueError"
    except ValueError as e:
        assert "goal" in str(e) or "spec" in str(e)


def test_inbox_task_round_trip_writes_outbox_result(tmp_path, monkeypatch):
    """Exercises the ai-orchestrator side of the Trello -> AI Project Manager
    -> ai-orchestrator -> provider -> result chain: AI Project Manager drops a
    task derived from a Trello card into inbox/, ai-orchestrator picks it up,
    runs it, and writes the outcome to outbox/ for AI Project Manager to read
    and post back to the card. Posting to Trello itself happens in AI Project
    Manager, outside this repository, so it is not exercised here.
    """
    monkeypatch.setattr(service_module, "build_agent", lambda name, config: FakeAgent())
    cfg = make_cfg(tmp_path)
    service = OrchestratorService(cfg)
    try:
        cfg.inbox_dir.mkdir(parents=True, exist_ok=True)
        inbox_file = cfg.inbox_dir / "trello-card-42.json"
        inbox_file.write_text(
            json.dumps({"project": "station-agent", "prompt": "Trello karta #42: over health endpoint"}),
            encoding="utf-8",
        )

        created = service.import_inbox()
        assert len(created) == 1
        assert created[0].source == "inbox"
        assert (cfg.inbox_dir / "processed" / "trello-card-42.json").exists()
        assert not inbox_file.exists()

        result_task = service.run_sync(created[0])
        assert result_task.status == service_module.TaskStatus.DONE

        outbox_path = cfg.outbox_dir / f"{result_task.id}.json"
        assert outbox_path.exists()
        payload = json.loads(outbox_path.read_text(encoding="utf-8"))
        assert payload["status"] == "done"
        assert payload["source"] == "inbox"
        assert payload["result"]
    finally:
        service.shutdown()


def test_second_service_instance_cannot_recover_live_task(tmp_path):
    import pytest
    from orchestrator.queue import make_task

    cfg = make_cfg(tmp_path)
    service1 = OrchestratorService(cfg)
    try:
        live = make_task(
            "station-agent",
            str(tmp_path / "workspace" / "station-agent"),
            "goal",
            "claude-code",
            None,
            0,
            False,
            source="autonomous",
        )
        live.status = service_module.TaskStatus.RUNNING
        live.is_autonomous = True
        service1.queue.add(live)

        with pytest.raises(RuntimeError, match="Jiná instance ai-orchestrator"):
            OrchestratorService(cfg)

        assert service1.queue.get(live.id).status == service_module.TaskStatus.RUNNING
    finally:
        service1.shutdown()

def test_startup_recovers_orphaned_running_task_and_dedupes_waiting(tmp_path):
    """A process that died mid-run leaves rows behind that a fresh process
    never touches on its own (nothing resumes a bare RUNNING row, and
    find_active_autonomous would treat it as still active forever) - and,
    separately, historical duplicate WAITING_FOR_PROVIDER rows for the same
    project+spec that predate find_active_autonomous's dedup-on-insert.
    Both must be reconciled automatically the next time the service starts,
    without requiring an external run to notice and clean them up by hand.
    """
    cfg = make_cfg(tmp_path)
    # Seed the queue file directly (as a previous, now-dead process would
    # have left it) before OrchestratorService.__init__ ever runs.
    from orchestrator.queue import TaskQueue, make_task

    seed_queue = TaskQueue(cfg.data_dir / "tasks.db")
    orphan = make_task("station-agent", str(tmp_path / "workspace" / "station-agent"), "goal", "claude-code", None, 0, False, source="autonomous")
    orphan.status = service_module.TaskStatus.RUNNING
    orphan.is_autonomous = True
    seed_queue.add(orphan)

    dup1 = make_task("station-agent", str(tmp_path / "workspace" / "station-agent"), "goal", "claude-code", None, 0, False, source="autonomous")
    dup1.created_at = "2026-01-01T00:00:00+00:00"
    dup1.status = service_module.TaskStatus.WAITING_FOR_PROVIDER
    dup1.is_autonomous = True
    dup1.spec_text = "- [ ] a"
    dup1.retry_at = "2999-01-01T00:00:00+00:00"
    seed_queue.add(dup1)

    dup2 = make_task("station-agent", str(tmp_path / "workspace" / "station-agent"), "goal", "claude-code", None, 0, False, source="autonomous")
    dup2.created_at = "2026-01-01T00:05:00+00:00"
    dup2.status = service_module.TaskStatus.WAITING_FOR_PROVIDER
    dup2.is_autonomous = True
    dup2.spec_text = "- [ ] a"
    dup2.retry_at = "2999-01-01T00:00:00+00:00"
    seed_queue.add(dup2)

    service = OrchestratorService(cfg)
    try:
        assert service.queue.get(orphan.id).status == service_module.TaskStatus.ERROR
        assert service.queue.get(dup1.id).status == service_module.TaskStatus.WAITING_FOR_PROVIDER
        assert service.queue.get(dup2.id).status == service_module.TaskStatus.ERROR
    finally:
        service.shutdown()


def test_waiting_worker_finds_due_provider_task(tmp_path):
    cfg = make_cfg(tmp_path)
    service = OrchestratorService(cfg)

    task = service.submit(
        project_ref="station-agent",
        prompt="pokracuj po limite",
    )

    task.status = service_module.TaskStatus.WAITING_FOR_PROVIDER
    task.is_autonomous = True
    task.retry_at = "2000-01-01T00:00:00+00:00"
    service.queue.update(task)

    found = service.queue.next_due_waiting()

    assert found is not None
    assert found.id == task.id



def test_waiting_worker_submits_due_task(tmp_path):
    cfg = make_cfg(tmp_path)
    service = OrchestratorService(cfg)

    submitted = []

    original_submit = service._executor.submit

    def fake_submit(fn, task):
        submitted.append((fn, task))
        return None

    service._executor.submit = fake_submit

    task = service.submit(
        project_ref="station-agent",
        prompt="obnov po limite",
    )

    task.status = service_module.TaskStatus.WAITING_FOR_PROVIDER
    task.is_autonomous = True
    task.retry_at = "2000-01-01T00:00:00+00:00"
    service.queue.update(task)

    service._waiting_worker_stop = True
    service._waiting_worker_interval = 0

    service._waiting_worker_loop()

    assert len(submitted) == 1
    assert submitted[0][0] == service._resume_autonomous_task
    assert submitted[0][1].id == task.id
    assert submitted[0][1].status == service_module.TaskStatus.RUNNING

    service._executor.submit = original_submit


def test_claim_due_waiting_is_atomic_and_cannot_be_claimed_twice(tmp_path):
    cfg = make_cfg(tmp_path)
    service = OrchestratorService(cfg)
    task = service.submit(project_ref="station-agent", prompt="resume once")
    task.status = service_module.TaskStatus.WAITING_FOR_PROVIDER
    task.is_autonomous = True
    task.retry_at = "2000-01-01T00:00:00+00:00"
    service.queue.update(task)

    first = service.queue.claim_next_due_waiting()
    second = service.queue.claim_next_due_waiting()

    assert first is not None
    assert first.id == task.id
    assert second is None
    service.shutdown()


def test_shutdown_stops_workers(tmp_path):
    cfg = make_cfg(tmp_path)
    service = OrchestratorService(cfg)

    assert service._waiting_worker.is_alive()

    service.shutdown()

    assert service._waiting_worker_stop is True
    assert not service._waiting_worker.is_alive()


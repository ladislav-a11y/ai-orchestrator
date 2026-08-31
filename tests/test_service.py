import json
import re
from pathlib import Path

from orchestrator import service as service_module
from orchestrator.agents.base import Agent, AgentRunResult
from orchestrator.autonomous import AUDIT_MARKER, AutonomousStatus
from orchestrator.config import ApiConfig, Config, GitConfig, PathsConfig, ProjectEntry, TestingConfig
from orchestrator.service import OrchestratorService


def _audit_response(request):
    indices = [int(value) for value in re.findall(r"(?m)^(\d+)\. ", request.prompt)]
    return json.dumps({
        "items": [{"index": value, "accepted": True, "evidence": "audit evidence"} for value in indices],
        "notes": "audit ok",
    })


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
            return AgentRunResult(success=True, output_text=_audit_response(request))
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


def test_run_autonomous_protocol_error_preserves_checkpoint_and_reports_waste_in_outbox(tmp_path, monkeypatch):
    """DoD points: a run that stops as PROTOCOL_ERROR must (1) keep the
    checkpoint/DoD progress already verified before the protocol errors
    started (never regress already-done items back to not-done), and (2)
    surface a clear stop reason plus a waste metric in the outbox JSON, not
    just a bare status - see the production incident referenced in
    autonomous.py's module docstring."""
    from orchestrator.autonomous import AUDIT_MARKER, PROTOCOL_ERROR_STREAK_LIMIT
    from orchestrator.autonomous_checkpoint import load_checkpoint

    spec = "- [ ] prvni bod\n- [ ] druhy bod, ktery se nikdy neoznaci"

    class FirstDoneThenProtocolErrorAgent(Agent):
        name = "fake"

        def __init__(self):
            self.calls = 0

        def is_available(self):
            return True, "ok"

        def run(self, request):
            if AUDIT_MARKER in request.prompt:
                return AgentRunResult(success=True, output_text=_audit_response(request))
            self.calls += 1
            if self.calls == 1:
                return AgentRunResult(
                    success=True,
                    output_text=(
                        '{"items": [{"index": 0, "done": true}, {"index": 1, "done": false}], '
                        '"notes": "prvni hotovo"}'
                    ),
                )
            return AgentRunResult(success=True, output_text="porad neplatny JSON, ne kontrakt")

    monkeypatch.setattr(service_module, "build_agent", lambda name, config: FirstDoneThenProtocolErrorAgent())
    sent = []
    monkeypatch.setattr("orchestrator.autonomous.notify", lambda msg: sent.append(msg))
    cfg = make_cfg(tmp_path)

    service = OrchestratorService(cfg)
    run_id, result = service.run_autonomous(
        project_ref="station-agent",
        goal="Priprav projekt",
        spec_text=spec,
        agent_name="fake",
        max_iterations=10,
        run_id="protocol-error-run",
    )

    assert result.status == AutonomousStatus.PROTOCOL_ERROR
    assert result.dod_items[0].done is True
    assert result.dod_items[1].done is False

    outbox_path = cfg.outbox_dir / f"autonomous-{run_id}.json"
    payload = json.loads(outbox_path.read_text(encoding="utf-8"))
    assert payload["status"] == "protocol_error"
    assert payload["done"] is False
    assert payload["checkpoint"]["completed_dod_indices"] == [0]
    assert payload["protocol_error_total"] >= PROTOCOL_ERROR_STREAK_LIMIT
    assert payload["protocol_error_wasted_prompt_tokens_estimate"] > 0
    assert payload["stop_reason"] and "protokol" in payload["stop_reason"].lower()

    checkpoint = load_checkpoint(cfg.data_dir, Path(cfg.projects["station-agent"].path), spec)
    assert checkpoint is not None
    done_indices = {i for i, item in enumerate(checkpoint.items) if item.get("done")}
    assert done_indices == {0}

    assert len(sent) == 1
    assert "protokol" in sent[0].lower()
    assert f"{result.protocol_error_total} protokolově chybných iterací" in sent[0]


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
                return AgentRunResult(success=True, output_text=_audit_response(request))
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


class _OnceLimitedFakeAgent(Agent):
    name = "fake"

    def __init__(self):
        self.calls = 0

    def is_available(self):
        return True, "fake agent always available"

    def run(self, request):
        if AUDIT_MARKER in request.prompt:
                return AgentRunResult(success=True, output_text=_audit_response(request))
        self.calls += 1
        if self.calls == 1:
            return AgentRunResult(
                success=False, output_text="", error="rate limit reached",
                limited=True, retry_after_seconds=90.0,
            )
        return AgentRunResult(
            success=True,
            output_text='{"items": [{"index": 0, "done": true}], "notes": "hotovo"}',
        )


def test_waiting_for_provider_reports_auto_resume_inactive_by_default(tmp_path, monkeypatch):
    """DoD (produkční incident cb501524e47e, 26.8.2026): a one-shot service
    (the CLI's default - see OrchestratorService.__init__'s `persistent`)
    must say plainly, both in the outbox handoff AND in the Slack
    notification, that automatic continuation is NOT active - there is no
    persistent worker/scheduler behind this particular invocation."""
    agent = _OnceLimitedFakeAgent()
    monkeypatch.setattr(service_module, "build_agent", lambda name, config: agent)
    sent = []
    monkeypatch.setattr(service_module, "notify", lambda msg: sent.append(msg))

    cfg = make_cfg(tmp_path)
    service = OrchestratorService(cfg)
    try:
        assert service.auto_resume_active is False
        run_id, result = service.run_autonomous(
            project_ref="station-agent",
            goal="Priprav projekt",
            spec_text="- [ ] Zaloz projekt",
            agent_name="fake",
            max_iterations=3,
            run_id="trello-card-inactive",
        )
        assert result.status == AutonomousStatus.WAITING_FOR_PROVIDER

        payload = json.loads(
            (cfg.outbox_dir / "autonomous-trello-card-inactive.json").read_text(encoding="utf-8")
        )
        assert payload["auto_resume_active"] is False
        assert payload["done"] is False

        assert len(sent) == 1
        assert "NENÍ aktivní" in sent[0]
        assert "trello-card-inactive" in sent[0]
        assert "90" in sent[0]
    finally:
        service.shutdown()


def test_waiting_for_provider_reports_auto_resume_active_when_persistent(tmp_path, monkeypatch):
    """The counterpart to the test above: a service explicitly constructed
    as persistent (e.g. the long-running `orchestrator.py api` process, see
    api.py's get_service()) must report auto-resume as active instead."""
    agent = _OnceLimitedFakeAgent()
    monkeypatch.setattr(service_module, "build_agent", lambda name, config: agent)
    sent = []
    monkeypatch.setattr(service_module, "notify", lambda msg: sent.append(msg))

    cfg = make_cfg(tmp_path)
    service = OrchestratorService(cfg, persistent=True)
    try:
        assert service.auto_resume_active is True
        run_id, result = service.run_autonomous(
            project_ref="station-agent",
            goal="Priprav projekt",
            spec_text="- [ ] Zaloz projekt",
            agent_name="fake",
            max_iterations=3,
            run_id="trello-card-active",
        )
        assert result.status == AutonomousStatus.WAITING_FOR_PROVIDER

        payload = json.loads(
            (cfg.outbox_dir / "autonomous-trello-card-active.json").read_text(encoding="utf-8")
        )
        assert payload["auto_resume_active"] is True

        assert len(sent) == 1
        assert "JE aktivní" in sent[0]
    finally:
        service.shutdown()


def test_fresh_service_instance_resumes_from_checkpoint_and_closes_orphaned_waiting_row(tmp_path, monkeypatch):
    """Mirrors the real AI Project Manager flow (README ch.9): every
    autonomous run is a brand-new one-shot process/OrchestratorService
    instance, NOT the same instance's internal waiting worker resuming a
    claimed task. After a WAITING_FOR_PROVIDER outcome, a second,
    completely independent OrchestratorService instance invoked with the
    same project+spec+run_id must:
    - resume from the orchestrator-verified checkpoint (skip the
      already-done item, never redo it);
    - never report a false 'done' in the interim (waiting) outbox state;
    - leave no orphaned WAITING_FOR_PROVIDER row behind in the queue once
      the second invocation actually finishes the run.

    This covers the actual production continuation path, not just the
    internal-worker-resume path already covered by
    test_resumed_autonomous_run_reuses_original_run_id.
    """
    cfg = make_cfg(tmp_path)
    spec = "- [ ] prvni bod\n- [ ] druhy bod"

    class PartialThenLimitedAgent(Agent):
        name = "fake"

        def __init__(self):
            self.calls = 0

        def is_available(self):
            return True, "ok"

        def run(self, request):
            if AUDIT_MARKER in request.prompt:
                return AgentRunResult(success=True, output_text=_audit_response(request))
            self.calls += 1
            if self.calls == 1:
                # DOD_BATCH_SIZE (8) covers both DoD items in one request, so
                # the response must address BOTH requested indices (0 and 1)
                # - naming only index 0 would leave index 1 "missing" and
                # trip a protocol_error/repair detour instead of cleanly
                # finishing this iteration with item 0 done, item 1 open.
                return AgentRunResult(
                    success=True,
                    output_text=(
                        '{"items": [{"index": 0, "done": true}, {"index": 1, "done": false}], '
                        '"notes": "prvni hotovo"}'
                    ),
                )
            return AgentRunResult(
                success=False, output_text="", error="rate limit", limited=True, retry_after_seconds=5.0,
            )

    first_agent = PartialThenLimitedAgent()
    monkeypatch.setattr(service_module, "build_agent", lambda name, config: first_agent)
    first_service = OrchestratorService(cfg)
    try:
        run_id, result = first_service.run_autonomous(
            project_ref="station-agent",
            goal="Priprav projekt",
            spec_text=spec,
            agent_name="fake",
            max_iterations=5,
            run_id="trello-card-resume",
        )
        assert result.status == AutonomousStatus.WAITING_FOR_PROVIDER
        assert [item.done for item in result.dod_items] == [True, False]
    finally:
        first_service.shutdown()

    outbox_path = cfg.outbox_dir / "autonomous-trello-card-resume.json"
    interim_payload = json.loads(outbox_path.read_text(encoding="utf-8"))
    assert interim_payload["done"] is False
    assert interim_payload["status"] == "waiting_for_provider"

    class FinishingAgent(Agent):
        name = "fake"

        def __init__(self):
            self.calls = 0

        def is_available(self):
            return True, "ok"

        def run(self, request):
            if AUDIT_MARKER in request.prompt:
                return AgentRunResult(success=True, output_text=_audit_response(request))
            self.calls += 1
            # Only the still-unmet item (index 1) should ever be asked about
            # - index 0 was already verified done by the checkpoint.
            assert "druhy bod" in request.prompt
            assert "prvni bod" not in request.prompt
            return AgentRunResult(
                success=True,
                output_text='{"items": [{"index": 1, "done": true}], "notes": "hotovo"}',
            )

    second_agent = FinishingAgent()
    monkeypatch.setattr(service_module, "build_agent", lambda name, config: second_agent)
    second_service = OrchestratorService(cfg)
    try:
        run_id2, result2 = second_service.run_autonomous(
            project_ref="station-agent",
            goal="Priprav projekt",
            spec_text=spec,
            agent_name="fake",
            max_iterations=5,
            run_id="trello-card-resume",
        )
        assert run_id2 == "trello-card-resume"
        assert result2.status == AutonomousStatus.COMPLETED
        assert result2.restored_from_checkpoint == 1
        assert all(item.done for item in result2.dod_items)
        assert second_agent.calls == 1

        waiting_rows = second_service.queue.list(status=service_module.TaskStatus.WAITING_FOR_PROVIDER)
        assert waiting_rows == []

        final_payload = json.loads(outbox_path.read_text(encoding="utf-8"))
        assert final_payload["status"] == "completed"
        assert final_payload["done"] is True
    finally:
        second_service.shutdown()


def test_pm_handoff_with_stale_out_of_range_checkpoint_indices_stays_on_real_five_item_dod(
    tmp_path, monkeypatch
):
    """Regression for the production incident (2026-08-26, P5 cleanup run):
    AI Project Manager echoed back a PM-CHECKPOINT whose
    "completed_dod_indices" (64, 65) referred to an unrelated, much larger
    Trello backlog instead of this card's actual 5-item Definition of Done.
    apply_pm_checkpoint() used to trust those indices positionally; here
    they are simply out of range for a 5-item list.

    A real end-to-end run_autonomous() call (not just the isolated
    autonomous_checkpoint unit tests) must:
    - never raise/IndexError on the out-of-range PM-CHECKPOINT;
    - keep the two items already marked "[x]" directly in the DoD text
      (the orchestrator's own prior verified progress, restated by AI
      Project Manager) done, instead of the bogus checkpoint wiping them;
    - only ask the agent about the real remaining items (2, 3, 4);
    - only ever emit in-range indices (0-4) in the outbox handoff back to
      AI Project Manager.
    """
    cfg = make_cfg(tmp_path)
    spec = (
        "- [x] bod jedna\n"
        "- [x] bod dva\n"
        "- [ ] bod tri\n"
        "- [ ] bod ctyri\n"
        "- [ ] bod pet\n"
        '\n<!-- PM-CHECKPOINT\n{"run_id":"trello-card-64-65",'
        '"checkpoint":{"completed_dod_indices":[64,65]}}\n-->\n'
    )

    class RemainingItemsAgent(Agent):
        name = "fake"

        def __init__(self):
            self.calls = 0

        def is_available(self):
            return True, "ok"

        def run(self, request):
            if AUDIT_MARKER in request.prompt:
                return AgentRunResult(success=True, output_text=_audit_response(request))
            self.calls += 1
            # Only the three genuinely unmet items should ever be asked
            # about - the checkpoint's bogus indices 64/65 must never
            # surface here, and the already-"[x]" items must not either.
            assert "bod jedna" not in request.prompt
            assert "bod dva" not in request.prompt
            return AgentRunResult(
                success=True,
                output_text=(
                    '{"items": ['
                    '{"index": 2, "done": true}, '
                    '{"index": 3, "done": true}, '
                    '{"index": 4, "done": true}'
                    '], "notes": "zbytek hotovo"}'
                ),
            )

    agent = RemainingItemsAgent()
    monkeypatch.setattr(service_module, "build_agent", lambda name, config: agent)

    service = OrchestratorService(cfg)
    try:
        run_id, result = service.run_autonomous(
            project_ref="station-agent",
            goal="Priprav projekt",
            spec_text=spec,
            agent_name="fake",
            max_iterations=5,
            run_id="trello-card-64-65",
        )
    finally:
        service.shutdown()

    assert len(result.dod_items) == 5
    assert result.status == AutonomousStatus.COMPLETED
    assert [item.done for item in result.dod_items] == [True, True, True, True, True]
    assert agent.calls == 1

    outbox_path = cfg.outbox_dir / "autonomous-trello-card-64-65.json"
    payload = json.loads(outbox_path.read_text(encoding="utf-8"))
    assert payload["checkpoint"]["completed_dod_indices"] == [0, 1, 2, 3, 4]
    assert all(0 <= i < 5 for i in payload["checkpoint"]["completed_dod_indices"])


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
    service = OrchestratorService(cfg, persistent=True)

    assert service._waiting_worker is not None
    assert service._waiting_worker.is_alive()

    service.shutdown()

    assert service._waiting_worker_stop is True
    assert not service._waiting_worker.is_alive()


def test_nonpersistent_service_does_not_start_waiting_worker(tmp_path):
    cfg = make_cfg(tmp_path)
    service = OrchestratorService(cfg)

    assert service.auto_resume_active is False
    assert service._waiting_worker is None

    service.shutdown()

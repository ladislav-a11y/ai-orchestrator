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


def test_run_autonomous_requires_goal_or_spec(tmp_path):
    cfg = make_cfg(tmp_path)
    service = OrchestratorService(cfg)
    try:
        service.run_autonomous(project_ref="station-agent")
        assert False, "expected ValueError"
    except ValueError as e:
        assert "goal" in str(e) or "spec" in str(e)
